import base64
import json
from pathlib import Path

import pytest

from deploy.backup import Backup, BackupError, digest, validate, write_json
from deploy.monitor import backup_alerts


def config(tmp_path):
    for name in ("data", "keys"):
        password = tmp_path / (name + "-password")
        password.write_text(name)
        password.chmod(0o600)
    return {
        "host": "test",
        "off_host_confirmed": True,
        "staging_directory": str(tmp_path),
        "restic": "/tmp/evaldiff-restic",
        **{
            name: {
                "repository": str(tmp_path / name),
                "password_file": str(tmp_path / (name + "-password")),
            }
            for name in ("data", "keys")
        },
    }


def test_private_manifest_and_stale_alerts(tmp_path):
    path = tmp_path / "status.json"
    write_json(path, {"last_success": 100, "outcome": "failed"})
    assert path.stat().st_mode & 0o077 == 0
    assert [x["code"] for x in backup_alerts(path, 21700)] == ["backup_failed", "backup_stale"]
    assert backup_alerts(tmp_path / "missing")[0]["code"] == "backup_status_unavailable"


def test_remote_separate_repository_validation(tmp_path):
    settings = config(tmp_path)
    with pytest.raises(BackupError, match="remote_repository"):
        validate(settings)
    settings["data"]["repository"] = "sftp:data@offhost:/data"
    settings["keys"]["repository"] = "sftp:keys@otherhost:/keys"
    validate(settings)
    Path(settings["keys"]["password_file"]).write_text("data")
    with pytest.raises(BackupError, match="separate_repository_passwords"):
        validate(settings)


@pytest.mark.parametrize("failure", ["dump", "objects", "forced_stop", None])
def test_capture_always_resumes_original_containers(tmp_path, failure):
    backup = Backup(config(tmp_path))
    backup.containers = {
        name: {"Id": name + "-original", "Image": "sha256:original"}
        for name in ("api", "db", "seaweed")
    }
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "inspect":
            stopped = not any(call[0] == "start" for call in calls)
            return json.dumps(
                {
                    "Running": not stopped,
                    "OOMKilled": False,
                    "ExitCode": 137 if failure == "forced_stop" else 0,
                    "Health": {"Status": "healthy"},
                }
            ).encode()
        if args[0] == "exec" and failure == "dump" or args[0] == "run" and failure == "objects":
            raise BackupError("injected")
        if kwargs.get("output"):
            kwargs["output"].write(b"payload")
        return b""

    backup.docker = docker
    if failure:
        with pytest.raises(BackupError):
            backup.capture(tmp_path, "weed-volume")
    else:
        consistency, pause = backup.capture(tmp_path, "weed-volume")
        assert consistency > 0 and pause >= 0
        helper = next(call for call in calls if call[0] == "run")
        assert "type=volume,src=weed-volume,dst=/data,readonly" in helper
    started = [call[1] for call in calls if call[0] == "start"]
    assert "api-original" in started
    if failure != "forced_stop":
        assert started == ["seaweed-original", "api-original"]


def test_run_keeps_keys_out_of_data_and_manifest(tmp_path):
    backup = Backup(config(tmp_path))
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    backup.containers = {
        name: {
            "Id": name,
            "Image": "sha256:" + name,
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": "revision",
                    "org.opencontainers.image.version": "version",
                }
            },
        }
        for name in ("api", "db", "seaweed")
    }
    backup.inspect = lambda: "volume"
    backup.docker = lambda *args, **kwargs: json.dumps(
        {"EVALDIFF_SECRET_KEY": key, "EVALDIFF_PREVIOUS_SECRET_KEYS": ""}
    ).encode()
    backup.restic = lambda *args, **kwargs: b"{}"
    published = {}

    def publish(name, directory):
        published[name] = {p.name: p.read_bytes() for p in directory.iterdir()}
        return name + "-snapshot"

    def capture(directory, volume):
        (directory / "database.dump").write_bytes(b"db")
        (directory / "objects.tar").write_bytes(b"objects")
        return 100, 1

    backup.publish = publish
    backup.capture = capture
    result = backup.run()
    assert result["snapshot"] == "data-snapshot"
    assert key.encode() in published["keys"]["keys.json"]
    assert all(key.encode() not in content for content in published["data"].values())
    manifest = json.loads(published["data"]["manifest.json"])
    assert manifest["key_snapshot"] == "keys-snapshot"
    assert set(manifest["sha256"]) == {"database.dump", "objects.tar"}


def test_real_restic_verified_round_trip(tmp_path):
    if not Path("/tmp/evaldiff-restic").exists():
        pytest.skip("optional verified restic binary unavailable")
    backup = Backup(config(tmp_path))
    backup.restic("data", "init")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "database.dump").write_bytes(b"postgres dump")
    (payload / "objects.tar").write_bytes(b"object archive")
    write_json(payload / "manifest.json", {"sha256": digest(payload / "database.dump")})
    snapshot = backup.publish("data", payload)
    rows = json.loads(backup.restic("data", "snapshots", "--json"))
    assert rows[0]["id"] == snapshot
    assert "verified" in rows[0]["tags"] and "pending" not in rows[0]["tags"]
    backup.restic(
        "data",
        "forget",
        "--host",
        "test",
        "--tag",
        "data,verified",
        "--group-by",
        "host",
        "--keep-within",
        "7d",
        "--keep-within-daily",
        "30d",
        "--keep-within-weekly",
        "84d",
    )
    backup.restic("data", "prune")
    assert len(json.loads(backup.restic("data", "snapshots", "--json"))) == 1


def test_corrupt_readback_never_marks_verified(tmp_path):
    backup = Backup(config(tmp_path))
    payload = tmp_path / "capture"
    payload.mkdir()
    (payload / "database.dump").write_bytes(b"original")
    commands = []

    def restic(name, *args, **kwargs):
        commands.append(args)
        if args[0] == "backup":
            return b'{"message_type":"summary","snapshot_id":"snapshot"}\n'
        if args[0] == "restore":
            target = Path(args[args.index("--target") + 1]) / str(payload).lstrip("/")
            target.mkdir(parents=True)
            (target / "database.dump").write_bytes(b"corrupted")
        return b""

    backup.restic = restic
    with pytest.raises(BackupError, match="data_verification"):
        backup.publish("data", payload)
    assert not any(args[0] == "tag" for args in commands)


def test_key_upload_failure_does_not_pause_writers(tmp_path):
    backup = Backup(config(tmp_path))
    backup.containers = {"api": {"Id": "api"}}
    backup.inspect = lambda: "volume"
    backup.restic = lambda *args, **kwargs: b"{}"
    backup.docker = lambda *args, **kwargs: json.dumps(
        {
            "EVALDIFF_SECRET_KEY": base64.urlsafe_b64encode(b"k" * 32).decode(),
            "EVALDIFF_PREVIOUS_SECRET_KEYS": "",
        }
    ).encode()

    def publish(*args):
        raise BackupError("keys_upload")

    backup.publish = publish
    backup.capture = lambda *args: pytest.fail("writers must remain available")
    with pytest.raises(BackupError, match="keys_upload"):
        backup.run()
