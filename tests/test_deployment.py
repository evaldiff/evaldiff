"""Candidate provenance and failure paths using the real script and inert Docker."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest
from starlette.testclient import TestClient

from api.build_info import write_build_revision
from api.main import create_app
from api.secrets import generate_secret_key
from api.settings import Settings

REVISION = "a" * 40
IMAGE = "sha256:" + "b" * 64


def checker():
    spec = importlib.util.spec_from_file_location(
        "check_deployment", Path("deploy/check_deployment.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOCKER_STUB = """
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["TEST_TRACE"], "a") as trace:
    trace.write(" ".join(args) + "\\n")
failure = os.environ.get("TEST_FAILURE", "")
image = "sha256:" + "b" * 64
if args[:2] == ["compose", "build"]:
    context = Path(os.environ["EVALDIFF_BUILD_CONTEXT"])
    assert (context / "deploy/deploy.sh").exists()
    assert not (context / "deploy/.env").exists()
    assert not (context / "private.local").exists()
    with open(os.environ["TEST_TRACE"], "a") as trace:
        trace.write("build-context " + str(context) + "\\n")
    if failure == "candidate_changed":
        with open("deploy.sh", "a") as file:
            file.write("\\n# unreviewed concurrent edit\\n")
if "--validate-config" in args:
    os.execv(os.environ["TEST_PYTHON"], [os.environ["TEST_PYTHON"], "-m", "api.migrate_secrets", "--validate-config"])
if args[:3] == ["compose", "up", "--help"]:
    print("--wait --wait-timeout" if failure != "old_compose" else "old help")
elif args[:2] == ["image", "inspect"]:
    print(json.dumps({"id": image, "version": os.environ["EVALDIFF_BUILD_VERSION"],
        "revision": "wrong" if failure == "image_labels" else os.environ["EVALDIFF_BUILD_REVISION"]}))
elif args[:3] == ["compose", "ps", "-q"]:
    print(args[3])
elif args[:1] == ["inspect"]:
    service = args[1]
    print(json.dumps({"status": "running", "health": "unhealthy" if failure == service + "_health" else "healthy",
        "image": "sha256:" + "c" * 64 if failure == "running_image" and service == "api" else image}))
elif args[:2] == ["compose", "up"] and "--help" not in args:
    if failure == "dependencies" and "db" in args:
        sys.exit(1)
    if failure == "api_wait" and "api" in args:
        sys.exit(1)
elif args[:2] == ["compose", "exec"]:
    if failure == "container_smoke":
        sys.exit(1)
"""

PYTHON_STUB = """
import importlib.util
import io
import json
import os
import sys
from urllib.error import HTTPError
args = sys.argv[1:]
if len(args) > 1 and args[0].endswith("check_deployment.py") and args[1] == "http":
    spec = importlib.util.spec_from_file_location("deployment", args[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failure = os.environ.get("TEST_FAILURE", "")
    with open(os.environ["TEST_TRACE"], "a") as trace:
        trace.write("https-smoke\\n")
    class Opener:
        def open(self, url, **kwargs):
            if url.endswith("/ops"):
                raise HTTPError(url, 404, "private", {}, None)
            if url.endswith("/ready"):
                return io.BytesIO(json.dumps({"status": "ready", "checks": {
                    "database": failure != "https_readiness", "storage": True, "worker": True, "probe_fresh": True
                }}).encode())
            return io.BytesIO(json.dumps({"status": "ok", "version": os.environ["EVALDIFF_BUILD_VERSION"],
                "revision": "wrong" if failure == "https_revision" else os.environ["EVALDIFF_BUILD_REVISION"]}).encode())
    module.build_opener = lambda *a: Opener()
    sys.argv = args
    raise SystemExit(module.main())
os.execv(os.environ["TEST_PYTHON"], [os.environ["TEST_PYTHON"], *args])
"""


@pytest.fixture
def deployment_host(tmp_path):
    if sys.version_info < (3, 11):
        pytest.skip("deployment host requires Python 3.11+; image uses Python 3.12")
    repo = tmp_path / "repo"
    deploy = repo / "deploy"
    deploy.mkdir(parents=True)
    for filename in ("deploy.sh", "check_deployment.py"):
        (deploy / filename).write_text(Path("deploy", filename).read_text())
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.0.18"\n')
    (repo / ".gitignore").write_text(".env\nprivate.local\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "candidate"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (deploy / ".env").write_text("ARCHIVE_TEST_SECRET=sensitive-ignored-secret\n")
    (repo / "private.local").write_text("ignored data")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, code in (("docker", DOCKER_STUB), ("python3", PYTHON_STUB)):
        path = commands / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)
    sleep = commands / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o700)
    trace = tmp_path / "trace"
    env = {key: value for key, value in os.environ.items() if not key.startswith("EVALDIFF_")}
    env.update(
        PATH=str(commands) + os.pathsep + os.environ["PATH"],
        PYTHONPATH=str(Path.cwd()),
        TEST_PYTHON=sys.executable,
        TEST_TRACE=str(trace),
        POSTGRES_PASSWORD="sensitive-db-password",
        SEAWEEFS_SECRET="sensitive-s3-password",
        EVALDIFF_SECRET_KEY=generate_secret_key(),
    )

    def run(failure="", key=None):
        configured = {**env, "TEST_FAILURE": failure}
        if key is not None:
            configured["EVALDIFF_SECRET_KEY"] = key
        result = subprocess.run(
            ["bash", str(deploy / "deploy.sh")],
            env=configured,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        calls = trace.read_text() if trace.exists() else ""
        for secret in (
            configured["POSTGRES_PASSWORD"],
            configured["SEAWEEFS_SECRET"],
            configured["EVALDIFF_SECRET_KEY"],
            "sensitive-ignored-secret",
        ):
            if secret:
                assert secret not in result.stdout + result.stderr + calls
        return result, calls

    return repo, run


@pytest.mark.parametrize("key_kind", ["missing", "invalid", "valid"])
def test_deploy_preflight_before_changing_services(deployment_host, key_kind):
    _, run = deployment_host
    key = {"missing": "", "invalid": "sensitive-invalid-key", "valid": None}[key_kind]
    result, calls = run(key=key)
    if key_kind == "valid":
        assert result.returncode == 0, result.stderr
        assert (
            calls.index("--validate-config")
            < calls.index("compose stop")
            < calls.index("compose up -d")
        )
        assert "--wait --wait-timeout 120 db seaweed" in calls
        assert "--wait --wait-timeout 120 api" in calls
        assert "exec -T api python - http http://localhost:8000" in calls
        assert "https-smoke" in calls
        assert "Deployment verified:" in result.stdout
        context = next(
            line.removeprefix("build-context ")
            for line in calls.splitlines()
            if line.startswith("build-context ")
        )
        assert not Path(context).exists()
    else:
        assert result.returncode != 0
        assert "compose stop" not in calls and "compose up -d" not in calls


@pytest.mark.parametrize(
    "failure, diagnostic",
    [
        ("old_compose", "must support"),
        ("image_labels", "labels do not match"),
        ("candidate_changed", "Candidate changed during build"),
        ("dependencies", "dependencies"),
        ("api_wait", "api-readiness"),
        ("db_health", "db: container is not healthy"),
        ("seaweed_health", "seaweed: container is not healthy"),
        ("running_image", "running image differs"),
        ("container_smoke", "container-smoke"),
        ("https_readiness", "dependencies or worker are not ready"),
        ("https_revision", "deployed version/revision differs"),
    ],
)
def test_deploy_fails_with_clear_stage_and_no_secret_output(deployment_host, failure, diagnostic):
    _, run = deployment_host
    result, calls = run(failure=failure)
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert "Deployment verified:" not in result.stdout
    if failure in {"old_compose", "image_labels", "candidate_changed"}:
        assert "compose stop" not in calls
    if failure == "dependencies":
        assert "--wait-timeout 120 api" not in calls


def test_dirty_checkout_rejected_before_build(deployment_host):
    repo, run = deployment_host
    (repo / "unreviewed.txt").write_text("uncommitted")
    result, calls = run()
    assert result.returncode != 0
    assert "checkout is dirty" in result.stderr
    assert "compose build" not in calls


@pytest.mark.parametrize(
    "failure", ["readiness", "version", "revision", "ops", "huge", "malformed"]
)
def test_http_smoke_rejects_invalid_candidate(failure, monkeypatch):
    module = checker()

    class Opener:
        def open(self, url, **kwargs):
            assert kwargs["timeout"] == 2
            if url.endswith("/ops"):
                if failure == "ops":
                    return io.BytesIO(b"private metrics")
                raise HTTPError(url, 404, "private", {}, None)
            if failure == "huge":
                return io.BytesIO(b"x" * 65537)
            if failure == "malformed":
                return io.BytesIO(b"secret-not-json")
            if url.endswith("/ready"):
                return io.BytesIO(
                    json.dumps(
                        {
                            "status": "ready",
                            "checks": {
                                "database": failure != "readiness",
                                "storage": True,
                                "worker": True,
                                "probe_fresh": True,
                            },
                        }
                    ).encode()
                )
            return io.BytesIO(
                json.dumps(
                    {
                        "status": "ok",
                        "version": "wrong" if failure == "version" else "0.0.18",
                        "revision": "wrong" if failure == "revision" else REVISION,
                    }
                ).encode()
            )

    monkeypatch.setattr(module, "build_opener", lambda *a: Opener())
    with pytest.raises(module.CheckFailed) as exc:
        module.http_checks("https://api.example.com", "0.0.18", REVISION, public=True)
    assert "secret-not-json" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
    ],
)
def test_public_origin_validation(url):
    module = checker()
    with pytest.raises(module.CheckFailed):
        module.validate_origin(url, public=True)
    with pytest.raises(module.CheckFailed):
        module.NoRedirects().redirect_request(
            None, None, 302, "redirect", {}, "https://elsewhere.com"
        )


def test_baked_revision_matches_api_health_and_is_not_runtime_env(tmp_path, monkeypatch):
    monkeypatch.setattr("api.main.BUILD_REVISION", REVISION)
    monkeypatch.setenv("EVALDIFF_BUILD_REVISION", "spoofed")
    settings = Settings(database_url=f"sqlite:///{tmp_path}/api.db", enable_worker=False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["revision"] == REVISION


def test_build_marker_validates_revision_and_source_version(tmp_path):
    pytest.importorskip("tomllib")
    (tmp_path / "api").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.0.18"\n')
    write_build_revision(REVISION, "0.0.18", tmp_path)
    assert (tmp_path / "api/_build_revision.py").read_text() == f"REVISION = {REVISION!r}\n"
    with pytest.raises(ValueError, match="version does not match"):
        write_build_revision(REVISION, "different", tmp_path)
    with pytest.raises(ValueError, match="full Git"):
        write_build_revision("unreviewed", "0.0.18", tmp_path)
