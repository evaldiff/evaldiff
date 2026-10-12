"""Readiness, total probe deadlines, redaction, and operational alert gates."""

import asyncio
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from api.db import make_engine, state
from api.main import create_app
from api.models import Account, Dataset, Job, Run, RunCase
from api.operations import Operations, dependency_probe
from api.operations_probe import probe
from api.settings import Settings
from api.startup import initialize_database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/ops.db",
        enable_worker=False,
        readiness_timeout=2,
        readiness_interval=0.1,
        rate_limit_rpm=0,
        signup_rate_per_min=0,
    )


def test_readiness_fails_closed_and_expires(settings, monkeypatch):
    operations = Operations(settings)
    assert not operations.snapshot()["ready"]
    operations.sample = {"database": True, "storage": True, "metrics": {"queue_depth": 3}}
    operations.sample_at = time.monotonic()
    assert operations.snapshot()["ready"]
    operations.sample["storage"] = False
    assert not operations.snapshot()["ready"]
    operations.sample["storage"] = True
    monkeypatch.setattr("api.operations.time.monotonic", lambda: operations.sample_at + 10)
    snapshot = operations.snapshot()
    assert not snapshot["ready"]
    assert snapshot["metrics"] == {}


async def test_worker_health_requires_live_task_and_tick(settings):
    settings.enable_worker = True
    operations = Operations(settings)
    operations.sample = {"database": True, "storage": True, "metrics": {}}
    operations.sample_at = time.monotonic()
    operations.worker_task = asyncio.create_task(asyncio.sleep(10))
    try:
        assert not operations.snapshot()["ready"]
        operations.worker.last_tick = time.monotonic()
        assert operations.snapshot()["ready"]
        operations.worker.last_tick -= 31
        assert not operations.snapshot()["ready"]
        operations.worker.last_tick = time.monotonic()
    finally:
        operations.worker_task.cancel()
        await asyncio.gather(operations.worker_task, return_exceptions=True)
    assert not operations.snapshot()["ready"]


async def test_real_probe_aggregates_without_payloads(settings):
    engine = make_engine(settings.database_url)
    initialize_database(engine)
    from sqlalchemy.orm import Session

    secret = "sensitive-dataset-model-key"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as session:
        account = Account(email=secret + "@example.com")
        dataset = Dataset(account=account, name=secret, case_count=1, cases_json=secret)
        run = Run(
            account=account,
            dataset=dataset,
            model=secret,
            endpoint="https://example.com",
            api_key_ref=secret,
            status="failed",
            finished_at=now,
            error="RunDeadlineExceeded: " + secret,
        )
        session.add(run)
        session.flush()
        session.add(RunCase(run_id=run.id, seq=0, error=secret))
        session.add(
            Job(
                payload_json=json.dumps({"run_id": run.id}),
                created_at=now - timedelta(seconds=70),
                status="pending",
            )
        )
        session.commit()
    result = await dependency_probe(settings)
    assert result["database"] and result["storage"]
    assert result["metrics"]["queue_depth"] == 1
    assert result["metrics"]["queue_oldest_seconds"] >= 70
    assert result["metrics"]["failed_runs_5m"] == 1
    assert result["metrics"]["timed_out_runs_5m"] == 1
    assert result["metrics"]["case_errors_5m"] == 1
    assert secret not in json.dumps(result)
    engine.dispose()


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "shutdown"])
async def test_stalled_probe_is_killed_and_reaped(settings, monkeypatch, cancel):
    original = asyncio.create_subprocess_exec
    processes = []

    async def stalled(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("api.operations.asyncio.create_subprocess_exec", stalled)
    settings.readiness_timeout = 0.1
    started = time.monotonic()
    task = asyncio.create_task(dependency_probe(settings))
    if cancel:
        while not processes:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(asyncio.TimeoutError):
            await task
    assert time.monotonic() - started < 2
    assert processes[0].returncode is not None


def test_readiness_route_is_cached_and_payload_free(settings, monkeypatch):
    settings.readiness_interval = 60

    async def sample(_):
        return {"database": True, "storage": True, "metrics": {"queue_depth": 0}}

    monkeypatch.setattr("api.operations.dependency_probe", sample)
    with TestClient(create_app(settings)) as client:
        until = time.monotonic() + 2
        while client.get("/ready").status_code != 200:
            assert time.monotonic() < until
            time.sleep(0.01)
        body = client.get("/ready").json()
        assert set(body) == {"status", "checks"}
        assert client.get("/ready").headers["cache-control"] == "no-store"
        operations = client.app.state.operations
        operations.sample["database"] = False
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 200
        # Database and storage exceptions count without recording their strings.
        with state.session_factory() as session, pytest.raises(OperationalError):
            session.execute(text("SELECT * FROM nonexistent_ops_table"))
        with pytest.raises(KeyError):
            state.storage.get_json("secret-object-key")
        snapshot = client.get("/ops").json()
        assert snapshot["database_errors_total"] == 1
        assert snapshot["storage_errors_total"] == 1
        assert "secret-object-key" not in json.dumps(snapshot)


def test_storage_failure_probe_redacts_exception(settings, monkeypatch, capsys):
    class FakeClient:
        def head_bucket(self, **kwargs):
            raise RuntimeError("secret-credential-payload")

        def close(self):
            pass

    import types

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: FakeClient())
    )
    monkeypatch.setitem(
        sys.modules, "botocore.config", types.SimpleNamespace(Config=lambda **k: None)
    )
    config = {
        "database_url": "sqlite://",
        "s3_endpoint": "https://example.com",
        "s3_access_key": "secret",
        "s3_secret_key": "secret",
        "s3_region": "x",
        "s3_bucket": "x",
    }
    result = probe(config)
    assert not result["storage"]
    assert "secret" not in json.dumps(result) + capsys.readouterr().out


def load_monitor():
    spec = importlib.util.spec_from_file_location(
        "monitor", Path(__file__).parents[1] / "deploy/monitor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alert_thresholds_and_error_counter_deltas():
    monitor = load_monitor()
    snapshot = {"ready": True, "metrics": {"queue_depth": 0}, "database_errors_total": 0}
    resources = {
        "memory": dict.fromkeys(monitor.MEMORY_LIMITS, 1),
        "disk_used_bytes": 1,
        "disk_free_bytes": 20 * monitor.GIB,
        "host_available_bytes": 2 * monitor.GIB,
    }
    assert monitor.alerts(snapshot, resources, snapshot) == []
    previous = dict(snapshot)
    snapshot.update(
        database_errors_total=1, storage_errors_total=1, worker_heartbeat_errors_total=1
    )
    snapshot["metrics"] = {
        "queue_oldest_seconds": 180,
        "queue_depth": 40,
        "expired_leases": 1,
        "failed_runs_5m": 5,
        "timed_out_runs_5m": 3,
    }
    resources["memory"]["api"] = monitor.MEMORY_LIMITS["api"]
    resources["disk_free_bytes"] = 9 * monitor.GIB
    events = {
        event["code"]: event["severity"] for event in monitor.alerts(snapshot, resources, previous)
    }
    assert events["queue_depth"] == "warning"
    for code in (
        "queue_oldest_seconds",
        "expired_leases",
        "failed_runs_5m",
        "timed_out_runs_5m",
        "api_memory",
        "disk_budget",
    ):
        assert events[code] == "critical"
    assert events["database_errors"] == "warning"
    assert events["storage_errors"] == "warning"
    assert events["worker_heartbeat_errors"] == "warning"
    assert monitor.byte_size("1.5GiB") == 1536 * 1024**2
    snapshot["database_errors_total"] = 0  # process restart must not emit a negative delta
    assert "database_errors" not in {
        a["code"] for a in monitor.alerts(snapshot, resources, previous)
    }


def test_monitor_collection_failure_is_redacted(monkeypatch, capsys):
    monitor = load_monitor()
    monkeypatch.setattr(sys, "argv", ["monitor", "--volume-path", "/tmp"])

    def fail(_):
        raise RuntimeError("secret-connection-string")

    monkeypatch.setattr(monitor, "collect", fail)
    assert monitor.main() == 1
    output = capsys.readouterr().out
    assert "monitor_collection_failed" in output
    assert "secret" not in output


async def test_slow_drip_s3_headers_obey_total_probe_deadline(settings):
    pytest.importorskip("boto3")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    entered = threading.Event()
    release = threading.Event()

    class SlowS3(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            self.wfile.flush()
            entered.set()
            try:
                while not release.wait(0.1):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowS3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = make_engine(settings.database_url)
    initialize_database(engine)
    settings.s3_endpoint = f"http://127.0.0.1:{server.server_port}"
    settings.s3_access_key = "synthetic"
    settings.s3_secret_key = "synthetic"
    settings.readiness_timeout = 2
    started = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await dependency_probe(settings)
        assert entered.is_set()  # reached real S3 I/O rather than timing out at import
        assert time.monotonic() - started < 3
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        engine.dispose()
