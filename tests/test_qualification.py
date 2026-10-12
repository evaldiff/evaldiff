import asyncio
import base64
import hashlib
import itertools
import json
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

from deploy.backup import BackupError, write_json
from deploy.check_deployment import CheckFailed
from deploy.qualification.fixture import CONTENT, app
from deploy.qualification.restore import check_archive, check_key_ids, checked_snapshot
from deploy.qualification.run import Harness, QualificationError, load_config, mix, percentile


def settings(tmp_path):
    environment = tmp_path / "candidate.env"
    environment.write_text("POSTGRES_PASSWORD=private\n")
    environment.chmod(0o600)
    return {
        "purpose": "isolated-staging",
        "project": "evaldiff-staging-test",
        "base_url": "https://staging.example.com",
        "fixture_url": "https://fixture.example.com",
        "fixture_key": "private-fixture",
        "fixture_version": 1,
        "image": "registry.example.com/api:commit",
        "image_id": "sha256:" + "a" * 64,
        "revision": "b" * 40,
        "version": "0.0.18",
        "seed": 7,
        "volume_path": "/tmp",
        "environment_file": str(environment),
        "evidence_directory": str(tmp_path / "evidence"),
    }


def test_configuration_requires_private_isolated_project(tmp_path):
    config = settings(tmp_path)
    path = tmp_path / "config.json"
    write_json(path, config)
    assert load_config(path) == config
    for change in (
        {"project": "evaldiff"},
        {"purpose": "production"},
        {"revision": "unknown"},
        {"base_url": "http://staging.example.com"},
    ):
        write_json(path, {**config, **change})
        with pytest.raises((QualificationError, CheckFailed, BackupError)):
            load_config(path)
    path.chmod(0o644)
    with pytest.raises(BackupError):
        load_config(path)


def test_percentiles_and_reproducible_exact_mix():
    assert percentile([], 0.95) is None
    assert percentile(range(1, 101), 0.95) == 95
    assert percentile(range(1, 101), 0.99) == 99
    left = list(itertools.islice(mix(7), 40))
    assert left == list(itertools.islice(mix(7), 40))
    assert left != list(itertools.islice(mix(8), 40))
    assert {name: left.count(name) for name in set(left)} == {
        "healthy": 32,
        "slow": 4,
        "error": 2,
        "stall": 2,
    }


@pytest.mark.asyncio
async def test_http_isolation_and_secret_free_evidence(tmp_path):
    config = settings(tmp_path)
    harness = Harness(config)
    await harness.client.aclose()
    requests = []

    def respond(request):
        requests.append(request)
        headers = {"X-Evaldiff-Staging-Project": config["project"]}
        if request.url.path == "/ops":
            return httpx.Response(404, text="Not Found", headers=headers)
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"version": config["version"], "revision": config["revision"]},
                headers=headers,
            )
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "checks": {k: True for k in ("database", "storage", "worker", "probe_fresh")},
                },
                headers=headers,
            )
        return httpx.Response(200, json={"payload": "secret-body"}, headers=headers)

    harness.client = httpx.AsyncClient(
        base_url=config["base_url"], transport=httpx.MockTransport(respond)
    )
    try:
        await harness.identity()
        await harness.request("GET", "/v1/usage", {"key": "secret-bearer"})
        evidence = (harness.directory / "events.jsonl").read_text()
        assert "secret-body" not in evidence and "secret-bearer" not in evidence
        assert requests[-1].headers["authorization"] == "Bearer secret-bearer"
        await harness.client.aclose()
        harness.client = httpx.AsyncClient(
            base_url=config["base_url"],
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )
        with pytest.raises(QualificationError):
            await harness.request("GET", "/health")
    finally:
        await harness.client.aclose()


@pytest.mark.asyncio
async def test_candidate_freeze_and_scoped_compose(tmp_path, monkeypatch):
    config = settings(tmp_path)
    harness = Harness(config)
    try:
        harness.freeze()
        assert "fixture_key" not in (harness.directory / "manifest.json").read_text()
        harness.config = {**config, "seed": 8}
        with pytest.raises(QualificationError, match="evidence_candidate_changed"):
            harness.freeze()
        harness.config = config
        captured = []
        harness.command = lambda args, **kwargs: captured.append((args, kwargs)) or b""
        monkeypatch.setenv("EVALDIFF_SECRET_KEY", "production-key")
        harness.compose("stop", "db")
        args, kwargs = captured[0]
        assert args[args.index("-p") + 1] == config["project"]
        assert "EVALDIFF_SECRET_KEY" not in kwargs["env"]
        assert kwargs["env"]["EVALDIFF_STAGING_IMAGE"] == config["image"]
    finally:
        await harness.client.aclose()


def test_restore_rejects_bad_keys_and_unsafe_archives(tmp_path):
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    values = {"EVALDIFF_SECRET_KEY": key, "EVALDIFF_PREVIOUS_SECRET_KEYS": ""}
    check_key_ids(values, [hashlib.sha256(b"k" * 32).hexdigest()])
    with pytest.raises(QualificationError):
        check_key_ids(values, ["wrong"])
    for name in ("../escape", "/absolute", "data/../escape"):
        archive = tmp_path / "objects.tar"
        with tarfile.open(archive, "w") as output:
            output.addfile(tarfile.TarInfo(name))
        with pytest.raises(QualificationError, match="unsafe_object_archive"):
            check_archive(archive)
    with tarfile.open(archive, "w") as output:
        info = tarfile.TarInfo("data/symlink")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
        output.addfile(info)
    with pytest.raises(QualificationError):
        check_archive(archive)


def test_restore_requires_verified_snapshot():
    class Fake:
        def __init__(self):
            self.config = {"host": "candidate"}

        def restic(self, *args):
            return json.dumps([{"id": "a" * 64, "tags": ["data", "pending"]}])

    with pytest.raises(QualificationError, match="verified_snapshot_required"):
        checked_snapshot(Fake(), "data", "a" * 64)
    with pytest.raises(QualificationError, match="full_snapshot_id_required"):
        checked_snapshot(Fake(), "data", "short")


@pytest.mark.asyncio
async def test_fixture_authentication_and_contract(monkeypatch):
    monkeypatch.setenv("EVALDIFF_FIXTURE_KEY", "fixture-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.post("/healthy/v1/chat/completions")).status_code == 401
        response = await client.post(
            "/healthy/v1/chat/completions", headers={"Authorization": "Bearer fixture-secret"}
        )
        assert response.json()["choices"][0]["message"]["content"] == CONTENT
        assert len(CONTENT.encode()) == 1024
        assert (
            await client.post(
                "/error/v1/chat/completions", headers={"Authorization": "Bearer fixture-secret"}
            )
        ).status_code == 503
        assert (await client.get("/health")).json()["fixture_version"] == 1


def test_staging_topology_preserves_resource_limits():
    root = Path(__file__).resolve().parents[1]
    production = yaml.safe_load((root / "deploy/docker-compose.yml").read_text())
    staging = yaml.safe_load((root / "deploy/staging-compose.yml").read_text())
    assert staging["name"] != production["name"]
    for service in production["services"]:
        for field in ("cpus", "mem_limit", "memswap_limit", "logging"):
            assert staging["services"][service][field] == production["services"][service][field]
    api = staging["services"]["api"]
    assert "build" not in api
    assert api["environment"]["EVALDIFF_DEFAULT_QUOTA"] == "100000"
    assert api["environment"]["EVALDIFF_PRODUCTION"] == "true"
    assert "EVALDIFF_ALLOW_LOCAL_ENDPOINTS" not in api["environment"]


@pytest.mark.asyncio
async def test_summary_never_passes_from_latency_alone(tmp_path):
    harness = Harness(settings(tmp_path))
    try:
        harness.records = [
            {
                "kind": "request",
                "measured": True,
                "category": "control",
                "status": 200,
                "expected": 200,
                "seconds": 0.1,
            }
        ]
        result = harness.summary()
        assert result["outcome"] == "incomplete"
        assert result["latencies"]["control"]["p99"] == 0.1
        assert result["latencies"]["upload"]["p95"] is None
    finally:
        await harness.client.aclose()


@pytest.mark.asyncio
async def test_full_baseline_schedule_without_wall_clock_wait(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from deploy.qualification import run as module

    config = settings(tmp_path)
    harness = Harness(config)
    await harness.client.aclose()
    clock = [0.0]
    actual_sleep = asyncio.sleep
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 100000 + clock[0])
    )

    async def sleep(seconds):
        clock[0] += seconds
        await actual_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    harness.accounts = [{"key": f"private-{i}", "dataset": i + 1} for i in range(10)]
    harness.identity = lambda: actual_sleep(0)
    harness.provision = lambda: actual_sleep(0)
    harness.persisted_runs = list

    async def sample(stop):
        await stop.wait()

    harness.sample = sample
    next_run = [0]

    def respond(request):
        headers = {"X-Evaldiff-Staging-Project": config["project"]}
        if request.method == "POST" and request.url.path == "/v1/runs":
            next_run[0] += 1
            return httpx.Response(202, json={"id": next_run[0]}, headers=headers)
        if request.url.path.endswith("report.md"):
            return httpx.Response(200, text="# Synthetic report", headers=headers)
        if request.url.path.endswith("/diff"):
            return httpx.Response(200, json={"regressions": []}, headers=headers)
        if request.url.path.startswith("/v1/runs/"):
            return httpx.Response(
                200,
                json={"status": "done", "total_cases": 20, "passed_cases": 20, "avg_score": 1},
                headers=headers,
            )
        if request.method == "POST":
            return httpx.Response(201, json={"id": 1}, headers=headers)
        return httpx.Response(200, json={"used": 0}, headers=headers)

    harness.client = httpx.AsyncClient(
        base_url=config["base_url"], transport=httpx.MockTransport(respond)
    )
    try:
        await harness.load("baseline")
        measured = [row for row in harness.records if row["kind"] == "request" and row["measured"]]
        assert len(measured) == 7200
        assert len(harness.runs) == 75
        assert all(item["terminal"] for item in harness.runs)
        assert all(
            sum(row["category"] == category for row in measured) == 100
            for category in ("upload", "report", "diff")
        )
        assert (
            json.loads((harness.directory / "baseline-summary.json").read_text())["outcome"]
            == "incomplete"
        )
    finally:
        await harness.client.aclose()


@pytest.mark.asyncio
async def test_e2e_records_markers_and_checks_account_isolation(tmp_path):
    config = settings(tmp_path)
    harness = Harness(config)
    await harness.client.aclose()
    accounts = [{"key": f"private-{i}", "dataset": i + 1} for i in range(10)]
    write_json(harness.directory / "accounts.json", accounts)
    run_number = [0]

    def respond(request):
        headers = {"X-Evaldiff-Staging-Project": config["project"]}
        if request.url.path == "/health":
            body = {"version": config["version"], "revision": config["revision"]}
        elif request.url.path == "/ready":
            body = {
                "status": "ready",
                "checks": {k: True for k in ("database", "storage", "worker", "probe_fresh")},
            }
        elif request.url.path == "/ops":
            return httpx.Response(404, text="Not Found", headers=headers)
        elif request.url.path == "/v1/usage":
            body = {"quota": 100000}
        elif (
            request.url.path.startswith("/v1/datasets/")
            or request.headers.get("authorization") == "Bearer private-1"
        ):
            return httpx.Response(404, json={"detail": "not found"}, headers=headers)
        elif request.method == "POST":
            run_number[0] += 1
            return httpx.Response(202, json={"id": run_number[0]}, headers=headers)
        elif request.url.path.endswith("/cases"):
            body = [{"seq": i} for i in range(20)]
        elif request.url.path.endswith("/diff"):
            body = {"regressions": []}
        elif request.url.path.endswith("report.md"):
            return httpx.Response(200, text="# Report", headers=headers)
        else:
            body = {"status": "done", "total_cases": 20, "passed_cases": 20, "avg_score": 1}
        return httpx.Response(200, json=body, headers=headers)

    harness.client = httpx.AsyncClient(
        base_url=config["base_url"], transport=httpx.MockTransport(respond)
    )
    try:
        await harness.e2e()
        markers = json.loads((harness.directory / "markers.json").read_text())
        assert markers["runs"] == [1, 2]
        assert markers["account"]["dataset"] == 1
        assert (harness.directory / "markers.json").stat().st_mode & 0o077 == 0
        assert "private-" not in (harness.directory / "events.jsonl").read_text()
    finally:
        await harness.client.aclose()


@pytest.mark.asyncio
async def test_restore_rejects_existing_target_before_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from deploy.qualification import restore as module

    config = settings(tmp_path)
    config["project"] = "evaldiff-staging-restore-test"
    config_path = tmp_path / "config.json"
    write_json(config_path, config)
    backup_path = tmp_path / "backup.json"
    write_json(backup_path, {"host": "evaldiff-staging-source"})
    monkeypatch.setattr(module, "validate", lambda value: None)
    commands = []

    class Existing(Harness):
        def check_image(self):
            pass

        def compose(self, *args, **kwargs):
            commands.append(args)
            return b"existing-container"

    monkeypatch.setattr(module, "Harness", Existing)
    with pytest.raises(QualificationError, match="restore_target_not_empty"):
        await module.restore(
            SimpleNamespace(
                config=str(config_path),
                backup_config=str(backup_path),
                snapshot="a" * 64,
                incident_timestamp=100,
                markers="unused",
            )
        )
    assert commands == [("ps", "-aq")]


def test_staging_backup_scopes_compose_without_changing_docker_commands():
    from deploy.qualification.backup import StagingBackup

    calls = []

    class FakeHarness:
        def compose(self, *args, **kwargs):
            calls.append((args, kwargs))
            return b"isolated-api"

    backup = StagingBackup({}, FakeHarness())
    assert backup.docker("compose", "ps", "-q", "api") == b"isolated-api"
    assert calls[0][0] == ("ps", "-q", "api")
    backup.command = lambda args, **kwargs: calls.append((args, kwargs)) or b"original"
    assert backup.docker("start", "original-container") == b"original"
    assert calls[-1][0] == ["docker", "start", "original-container"]


@pytest.mark.asyncio
async def test_request_total_budget_bounds_slow_drip(tmp_path, monkeypatch):
    from deploy.qualification import run as module

    config = settings(tmp_path)
    harness = Harness(config)
    await harness.client.aclose()
    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.03)
    closed = []

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b" "

        async def aclose(self):
            closed.append(True)

    harness.client = httpx.AsyncClient(
        base_url=config["base_url"],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=Drip(), headers={"X-Evaldiff-Staging-Project": config["project"]}
            )
        ),
    )
    try:
        with pytest.raises(QualificationError):
            await asyncio.wait_for(harness.request("GET", "/health"), timeout=0.2)
        assert closed
        assert harness.records[-1]["status"] == 0
    finally:
        await harness.client.aclose()
