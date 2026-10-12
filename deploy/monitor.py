"""Host monitor: aggregate-only JSON alerts to stdout/journald, no payloads."""

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

GIB = 1024**3
MEMORY_LIMITS = {
    "api": 1536 * 1024**2,
    "db": 768 * 1024**2,
    "seaweed": 768 * 1024**2,
    "caddy": 192 * 1024**2,
}


def alerts(snapshot, resources, previous=None):
    result = []

    def add(code, severity="warning"):
        result.append({"code": code, "severity": severity})

    if not snapshot.get("ready"):
        add("readiness_failed", "critical")
    metrics = snapshot.get("metrics", {})
    for name, warning, critical in (
        ("queue_depth", 40, 80),
        ("queue_oldest_seconds", 60, 180),
        ("failed_runs_5m", 1, 5),
        ("timed_out_runs_5m", 1, 3),
        ("case_errors_5m", 1, 10),
        ("expired_leases", 1, 1),
    ):
        value = metrics.get(name, 0)
        if value >= warning:
            add(name, "critical" if value >= critical else "warning")
    for name in (
        "database_errors_total",
        "storage_errors_total",
        "database_probe_errors_total",
        "storage_probe_errors_total",
        "probe_failures_total",
        "worker_claim_errors_total",
        "worker_heartbeat_errors_total",
    ):
        if previous is not None and snapshot.get(name, 0) > previous.get(name, 0):
            add(name.removesuffix("_total"))
    for service, limit in MEMORY_LIMITS.items():
        used = resources.get("memory", {}).get(service)
        if used is None:
            add(f"{service}_memory_unavailable", "critical")
        elif used >= limit:
            add(f"{service}_memory", "critical")
    if (
        resources.get("disk_used_bytes", 0) >= 30 * GIB
        or resources.get("disk_free_bytes", 0) <= 10 * GIB
    ):
        add("disk_budget", "critical")
    elif resources.get("disk_free_bytes", 0) <= 12 * GIB:
        add("disk_free_low")
    if resources.get("host_available_bytes", 0) < GIB:
        add("host_memory", "critical")
    return result


def command(arguments):
    # Never include stderr in output: Docker failures can contain deployment data.
    return subprocess.run(
        arguments, cwd=Path(__file__).parent, check=True, capture_output=True, text=True, timeout=8
    ).stdout


def byte_size(value):
    match = re.fullmatch(r"([0-9.]+)\s*(B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)", value.strip())
    if not match:
        raise ValueError("invalid memory size")
    units = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": GIB,
        "TiB": 1024**4,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
    }
    return int(float(match[1]) * units[match[2]])


def collect(volume_path):
    snapshot = json.loads(
        command(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/ops', timeout=2).read().decode())",
            ]
        )
    )
    output = command(["docker", "compose", "ps", "--format", "json"])
    entries = (
        json.loads(output)
        if output.lstrip().startswith("[")
        else [json.loads(line) for line in output.splitlines()]
    )
    names = {entry["Name"]: entry["Service"] for entry in entries}
    if not names:
        raise ValueError("no containers")
    rows = command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names])
    memory = {}
    for line in rows.splitlines():
        row = json.loads(line)
        service = names[row["Name"]]
        memory[service] = memory.get(service, 0) + byte_size(row["MemUsage"].split("/")[0])
    disk = shutil.disk_usage(volume_path)
    available = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    return snapshot, {
        "memory": memory,
        "disk_used_bytes": disk.used,
        "disk_free_bytes": disk.free,
        "host_available_bytes": available,
    }


def backup_alerts(path, now=None):
    try:
        status = json.loads(Path(path).read_text())
        now = time.time() if now is None else now
        result = []
        if status.get("outcome") == "failed":
            result.append({"code": "backup_failed", "severity": "critical"})
        if now - float(status.get("last_success", 0)) >= 6 * 3600:
            result.append({"code": "backup_stale", "severity": "critical"})
        if status.get("outcome") == "running" and now - float(status["attempt_started"]) > 55 * 60:
            result.append({"code": "backup_stuck", "severity": "critical"})
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return [{"code": "backup_status_unavailable", "severity": "critical"}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume-path", required=True, help="dedicated Docker volume filesystem")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--backup-status")
    args = parser.parse_args()
    previous = None
    while True:
        started = time.monotonic()
        try:
            snapshot, resources = collect(args.volume_path)
            events = alerts(snapshot, resources, previous)
            previous = snapshot
            record = {
                "timestamp": int(time.time()),
                "alerts": events,
                "operations": snapshot,
                "resources": resources,
            }
        except Exception:  # noqa: BLE001 - diagnostics must not expose deployment secrets
            events = [{"code": "monitor_collection_failed", "severity": "critical"}]
            record = {"timestamp": int(time.time()), "alerts": events}
        if args.backup_status:
            events.extend(backup_alerts(args.backup_status))
        print(json.dumps(record, separators=(",", ":")), flush=True)
        if not args.loop:
            return 1 if events else 0
        time.sleep(max(0, 15 - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
