"""Cached readiness and aggregate operations data, without customer payloads."""

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("evaldiff.operations")


@dataclass
class WorkerHealth:
    last_tick: float = 0
    claim_errors: int = 0
    heartbeat_errors: int = 0


def memory_bytes() -> int | None:
    # Container-wide memory on cgroup v2/v1, with Linux process RSS fallback.
    for path in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            return int(Path(path).read_text().strip())
        except (OSError, ValueError):
            pass
    try:
        import os

        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


async def dependency_probe(settings) -> dict:
    config = {
        name: getattr(settings, name)
        for name in (
            "database_url",
            "s3_endpoint",
            "s3_access_key",
            "s3_secret_key",
            "s3_bucket",
            "s3_region",
        )
    }
    config["probe_key"] = f".evaldiff-readiness/{socket.gethostname()}-{os.getpid()}"
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "api.operations_probe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        process = await spawning
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps(config).encode()), timeout=settings.readiness_timeout
        )
        if process.returncode != 0:
            raise ValueError("probe failed")
        return json.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


class Operations:
    def __init__(self, settings):
        self.settings = settings
        self.worker = WorkerHealth()
        self.worker_task = None
        self.sample = {"database": False, "storage": False, "metrics": {}}
        self.sample_at = None
        self.database_errors = 0
        self.storage_errors = 0
        self.probe_timeouts = 0
        self._error_lock = threading.Lock()
        self.runtime_database_errors = 0
        self.runtime_storage_errors = 0

    def record_database_error(self):
        with self._error_lock:
            self.runtime_database_errors += 1

    def record_storage_error(self):
        with self._error_lock:
            self.runtime_storage_errors += 1

    async def monitor(self):
        while True:
            try:
                sample = await dependency_probe(self.settings)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - probe errors must be redacted
                self.probe_timeouts += 1
                sample = {"database": False, "storage": False, "metrics": {}}
            self.database_errors += not sample["database"]
            self.storage_errors += not sample["storage"]
            if (
                sample["database"] != self.sample["database"]
                or sample["storage"] != self.sample["storage"]
            ):
                log.info(
                    "dependency state database=%s storage=%s", sample["database"], sample["storage"]
                )
            self.sample, self.sample_at = sample, time.monotonic()
            await asyncio.sleep(self.settings.readiness_interval)

    def snapshot(self) -> dict:
        now = time.monotonic()
        age = now - self.sample_at if self.sample_at is not None else None
        fresh = (
            age is not None
            and age <= self.settings.readiness_interval + self.settings.readiness_timeout + 2
        )
        worker_age = now - self.worker.last_tick if self.worker.last_tick else None
        worker_ok = not self.settings.enable_worker or (
            self.worker_task is not None
            and not self.worker_task.done()
            and worker_age is not None
            and worker_age <= self.settings.worker_health_timeout
        )
        checks = {
            "database": fresh and self.sample["database"],
            "storage": fresh and self.sample["storage"],
            "worker": worker_ok,
            "probe_fresh": fresh,
        }
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "sample_age_seconds": age,
            "worker_tick_age_seconds": worker_age,
            "metrics": self.sample["metrics"] if fresh else {},
            "memory_bytes": memory_bytes(),
            "database_probe_errors_total": self.database_errors,
            "storage_probe_errors_total": self.storage_errors,
            "probe_failures_total": self.probe_timeouts,
            "database_errors_total": self.runtime_database_errors,
            "storage_errors_total": self.runtime_storage_errors,
            "worker_claim_errors_total": self.worker.claim_errors,
            "worker_heartbeat_errors_total": self.worker.heartbeat_errors,
        }
