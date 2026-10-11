"""Production encryption, offline upgrades, key rotation, and recovery."""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import Text, inspect
from starlette.testclient import TestClient

from api.db import state
from api.main import create_app
from api.migrate_secrets import migrate_credentials, verify_credentials
from api.models import Run
from api.secrets import KeyCipher, SecretError, generate_secret_key
from api.settings import Settings
from tests.test_recovery_quota import seed
from tests.test_recovery_quota import sessions as sessions  # noqa: PLC0414 -- export pytest fixture


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch):
    for name in ("EVALDIFF_SECRET_KEY", "EVALDIFF_PREVIOUS_SECRET_KEYS", "EVALDIFF_PRODUCTION"):
        monkeypatch.delenv(name, raising=False)
    for name in ("settings", "session_factory", "storage", "limiter"):
        monkeypatch.setattr(state, name, None)


def config(tmp_path, **kwargs):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/secrets.db",
        enable_worker=False,
        rate_limit_rpm=0,
        signup_rate_per_min=0,
        **kwargs,
    )


@pytest.mark.parametrize("key", [None, "", "not-a-key", "é" * 44])
def test_production_rejects_missing_invalid_key_before_database(tmp_path, key):
    app = create_app(config(tmp_path, production=True, secret_key=key))
    with pytest.raises(SecretError, match="EVALDIFF_SECRET_KEY") as error, TestClient(app):
        pytest.fail("startup accepted an invalid production key")
    if key:
        assert key not in str(error.value)
    assert not (tmp_path / "secrets.db").exists()


def test_settings_redact_keys_and_validate_fallbacks(tmp_path):
    key = generate_secret_key()
    settings = config(tmp_path, secret_key=key, previous_secret_keys="bad-fallback")
    assert key not in repr(settings)
    assert "bad-fallback" not in repr(settings)
    with pytest.raises(SecretError, match="EVALDIFF_PREVIOUS_SECRET_KEYS") as error:
        KeyCipher(settings)
    assert "bad-fallback" not in str(error.value)
    with pytest.raises(SecretError):
        KeyCipher(config(tmp_path, previous_secret_keys=key))


def submit(client, credential):
    signup = client.post("/v1/auth/signup", json={"email": "secret-test@example.com"})
    assert signup.status_code == 201
    headers = {"Authorization": f"Bearer {signup.json()['key']}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "d", "cases": [{"input": "hi", "expected": "hi"}]},
    )
    run = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds.json()["id"],
            "model": "m",
            "endpoint": "https://model.example",
            "api_key": credential,
        },
    )
    assert run.status_code == 202
    return headers, run.json()["id"]


def test_development_plaintext_requires_explicit_migration_for_production(tmp_path, caplog):
    key = generate_secret_key()
    with TestClient(create_app(config(tmp_path))) as client:
        _, run_id = submit(client, "legacy-provider-key")
        with state.session_factory() as session:
            assert session.get(Run, run_id).api_key_ref == "legacy-provider-key"
    assert "Development mode" in caplog.text
    settings = config(tmp_path, production=True, secret_key=key)
    with pytest.raises(SecretError, match="plaintext"), TestClient(create_app(settings)):
        pass
    from api.db import make_engine

    engine = make_engine(settings.database_url)
    try:
        migrate_credentials(engine, KeyCipher(settings))
    finally:
        engine.dispose()
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200


def test_new_credentials_encrypted_and_configuration_is_stable(tmp_path, monkeypatch):
    key = generate_secret_key()
    settings = config(tmp_path, production=True, secret_key=key)
    with TestClient(create_app(settings)) as client:
        monkeypatch.delenv("EVALDIFF_SECRET_KEY", raising=False)
        _, run_id = submit(client, "long-provider-" + "x" * 4096)
        with state.session_factory() as session:
            stored = session.get(Run, run_id).api_key_ref
        assert stored.startswith("enc:v1:") and len(stored) > 512
        assert "long-provider" not in stored
        assert KeyCipher(settings).decrypt(stored) == "long-provider-" + "x" * 4096


def test_migration_mixed_rows_idempotent_and_long_keys(sessions):
    cipher = KeyCipher(Settings(secret_key=generate_secret_key()))
    values = ["plain-" + "p" * 500, cipher.encrypt("already-encrypted"), ""]
    ids = []
    with sessions() as session:
        for value in values:
            _, run = seed(session)
            run.api_key_ref = value
            ids.append(run.id)
        session.commit()
    engine = sessions.kw["bind"]
    # Simulate the legacy PostgreSQL column, where expansion used to overflow.
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE runs ALTER COLUMN api_key_ref TYPE VARCHAR(512)")
        with pytest.raises(SecretError, match="widening"):
            verify_credentials(engine, cipher, require_encrypted=True)
    result = migrate_credentials(engine, cipher)
    assert (result.checked, result.encrypted, result.rotated) == (3, 1, 0)
    with sessions() as session:
        stored = [session.get(Run, i).api_key_ref for i in ids]
    assert len(stored[0]) > 512
    assert stored[1:] == values[1:]
    assert [cipher.decrypt(v) for v in stored] == [values[0], "already-encrypted", ""]
    assert migrate_credentials(engine, cipher).encrypted == 0
    with sessions() as session:
        assert [session.get(Run, i).api_key_ref for i in ids] == stored
    assert verify_credentials(engine, cipher, require_encrypted=True) == 3
    if engine.dialect.name == "postgresql":
        col = next(c for c in inspect(engine).get_columns("runs") if c["name"] == "api_key_ref")
        assert isinstance(col["type"], Text)


def test_migration_wrong_key_rolls_back_without_disclosure(sessions, caplog):
    wrong = KeyCipher(Settings(secret_key=generate_secret_key()))
    right = KeyCipher(Settings(secret_key=generate_secret_key()))
    original = ["plaintext-sensitive", right.encrypt("encrypted-sensitive")]
    with sessions() as session:
        ids = []
        for value in original:
            _, run = seed(session)
            run.api_key_ref = value
            ids.append(run.id)
        session.commit()
    with pytest.raises(SecretError) as error:
        migrate_credentials(sessions.kw["bind"], wrong)
    with sessions() as session:
        assert [session.get(Run, i).api_key_ref for i in ids] == original
    for secret in [*original, "encrypted-sensitive"]:
        assert secret not in str(error.value) + caplog.text


def test_rotation_keeps_old_reads_until_migration_verified(sessions):
    old_key, new_key = generate_secret_key(), generate_secret_key()
    old = KeyCipher(Settings(secret_key=old_key))
    new = KeyCipher(Settings(secret_key=new_key))
    rotating = KeyCipher(Settings(secret_key=new_key, previous_secret_keys=old_key))
    with sessions() as session:
        _, run = seed(session)
        run.api_key_ref = old.encrypt("provider-key")
        run_id = run.id
        session.commit()
    engine = sessions.kw["bind"]
    assert verify_credentials(engine, rotating, require_encrypted=True) == 1
    with pytest.raises(SecretError):
        verify_credentials(engine, new, require_encrypted=True)
    assert migrate_credentials(engine, rotating, rotate=True).rotated == 1
    assert migrate_credentials(engine, rotating, rotate=True).rotated == 0
    assert verify_credentials(engine, new, require_encrypted=True) == 1
    with sessions() as session:
        assert new.decrypt(session.get(Run, run_id).api_key_ref) == "provider-key"
        with pytest.raises(SecretError):
            old.decrypt(session.get(Run, run_id).api_key_ref)


def test_backup_restore_queued_run_and_wrong_key_startup(tmp_path, monkeypatch, caplog):
    key = generate_secret_key()
    settings = config(tmp_path, production=True, secret_key=key)
    with TestClient(create_app(settings)) as client:
        headers, run_id = submit(client, "restart-provider-key")
    restored = tmp_path / "restored.db"
    with (
        sqlite3.connect(tmp_path / "secrets.db") as source,
        sqlite3.connect(restored) as target,
    ):
        source.backup(target)
    restore_settings = settings.model_copy(update={"database_url": f"sqlite:///{restored}"})
    bad = restore_settings.model_copy(
        update={"secret_key": Settings(secret_key=generate_secret_key()).secret_key}
    )
    with pytest.raises(SecretError) as error, TestClient(create_app(bad)):
        pass
    assert "restart-provider-key" not in str(error.value) + caplog.text
    assert key not in str(error.value) + caplog.text
    seen = []

    async def model(client, **kwargs):
        seen.append(kwargs["api_key"])
        return "hi", 1, 1

    monkeypatch.setattr("api.runner.call_model", model)
    restore_settings = restore_settings.model_copy(update={"enable_worker": True})
    with TestClient(create_app(restore_settings)) as client:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = client.get(f"/v1/runs/{run_id}", headers=headers).json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert result["status"] == "done"
        assert result["passed_cases"] == 1
        assert seen == ["restart-provider-key"]


def test_migration_cli_redacts_configuration_and_database_errors(tmp_path):
    env = {**os.environ, "EVALDIFF_SECRET_KEY": "sensitive-invalid-value"}
    result = subprocess.run(
        [sys.executable, "-m", "api.migrate_secrets", "--validate-config"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "sensitive-invalid-value" not in result.stdout + result.stderr
    env.update(
        EVALDIFF_SECRET_KEY=generate_secret_key(), EVALDIFF_DATABASE_URL="invalid-sensitive-url"
    )
    result = subprocess.run(
        [sys.executable, "-m", "api.migrate_secrets"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "invalid-sensitive-url" not in result.stdout + result.stderr
    assert env["EVALDIFF_SECRET_KEY"] not in result.stdout + result.stderr


@pytest.mark.parametrize("key_kind", ["missing", "invalid", "valid"])
def test_deploy_preflight_before_starting_services(tmp_path, key_kind):
    """Run the real deploy script with inert Docker/system command substitutes."""
    script = tmp_path / "deploy.sh"
    script.write_text(Path("deploy/deploy.sh").read_text())
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$TEST_TRACE"\n'
        'if [[ "$*" == *"--validate-config"* ]]; then\n'
        '  exec "$TEST_PYTHON" -m api.migrate_secrets --validate-config\n'
        "fi\n"
        "exit 0\n"
    )
    # Avoid touching /etc/hosts or making network requests during script tests.
    for name, body in (("grep", "exit 0"), ("curl", "echo healthy")):
        command = commands / name
        command.write_text("#!/usr/bin/env bash\n" + body + "\n")
        command.chmod(0o700)
    docker.chmod(0o700)
    key = {"missing": "", "invalid": "sensitive-invalid-key", "valid": generate_secret_key()}[
        key_kind
    ]
    trace = tmp_path / "commands"
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(Path.cwd()),
        "TEST_PYTHON": sys.executable,
        "TEST_TRACE": str(trace),
        "POSTGRES_PASSWORD": "test-password",
        "SEAWEEFS_SECRET": "test-s3-password",
        "EVALDIFF_SECRET_KEY": key,
    }
    result = subprocess.run(
        ["bash", str(script)], env=env, check=False, capture_output=True, text=True
    )
    calls = trace.read_text() if trace.exists() else ""
    if key_kind == "valid":
        assert result.returncode == 0
        assert calls.index("--validate-config") < calls.index("compose up -d")
    else:
        assert result.returncode != 0
        assert "compose up" not in calls
    if key:
        assert key not in result.stdout + result.stderr + calls


def test_migration_crosses_batches_on_legacy_sqlite_schema(tmp_path):
    from api.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path}/legacy.db")
    cipher = KeyCipher(Settings(secret_key=generate_secret_key()))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE runs (id INTEGER PRIMARY KEY, api_key_ref VARCHAR(512))"
            )
            conn.exec_driver_sql(
                "INSERT INTO runs (id, api_key_ref) VALUES (?, ?)",
                [(i, "provider-key-" + str(i)) for i in range(1, 504)],
            )
        result = migrate_credentials(engine, cipher)
        assert result.checked == result.encrypted == 503
        assert verify_credentials(engine, cipher, require_encrypted=True) == 503
        assert migrate_credentials(engine, cipher).encrypted == 0
    finally:
        engine.dispose()
