"""Coordinated, verified restic backups; diagnostics never contain command output."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


class BackupError(Exception):
    pass


def private_json(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise BackupError("configuration_permissions")
    return json.loads(path.read_text())


def digest(path):
    with Path(path).open("rb") as source:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
        return checksum.hexdigest()


def write_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as target:
        json.dump(value, target)
        temporary = Path(target.name)
    temporary.replace(path)


def validate(config):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", config["host"]):
        raise BackupError("invalid_host")
    repositories = [config[name]["repository"] for name in ("data", "keys")]
    if repositories[0] == repositories[1] or not config.get("off_host_confirmed"):
        raise BackupError("separate_off_host_repositories_required")
    for repository in repositories:
        if not repository.startswith(("sftp:", "s3:", "rest:https://")):
            raise BackupError("remote_repository_required")
    passwords = []
    for name in ("data", "keys"):
        profile = config[name]
        password = Path(profile["password_file"])
        if password.stat().st_mode & 0o077:
            raise BackupError("password_permissions")
        passwords.append(password.read_bytes())
        if "credentials_file" in profile:
            private_json(profile["credentials_file"])
    if not all(passwords) or passwords[0] == passwords[1]:
        raise BackupError("separate_repository_passwords_required")


class Backup:
    def __init__(self, config):
        self.config = config
        self.stage = "preflight"
        self.deadline = time.monotonic() + 3000
        self.run_id = uuid.uuid4().hex
        self.helper = "evaldiff-backup-" + self.run_id
        self.containers = {}
        self.resume = []

    def command(self, arguments, *, output=None, env=None, timeout=120, recovery=False):
        budget = timeout if recovery else min(timeout, self.deadline - time.monotonic())
        if budget <= 0:
            raise BackupError(self.stage)
        with subprocess.Popen(
            arguments,
            stdout=output or subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=budget)
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            if process.returncode:
                raise BackupError(self.stage)
            return stdout

    def docker(self, *arguments, **kwargs):
        return self.command(["docker", *arguments], **kwargs)

    def restic(self, name, *arguments, **kwargs):
        profile = self.config[name]
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "USER", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if key in os.environ
        }
        if "credentials_file" in profile:
            environment.update(private_json(profile["credentials_file"]))
        # Repository/password settings cannot be overridden by credential profiles.
        return self.command(
            [
                self.config.get("restic", "restic"),
                "--repo",
                profile["repository"],
                "--password-file",
                profile["password_file"],
                *arguments,
            ],
            env=environment,
            timeout=1800,
            **kwargs,
        )

    def publish(self, name, directory):
        self.stage = name + "_upload"
        output = self.restic(
            name,
            "backup",
            "--json",
            "--host",
            self.config["host"],
            "--tag",
            name,
            "--tag",
            "pending",
            "--tag",
            self.run_id,
            str(directory),
        )
        rows = [json.loads(line) for line in output.splitlines()]
        snapshot = next(row["snapshot_id"] for row in rows if row.get("message_type") == "summary")
        self.stage = name + "_verification"
        with tempfile.TemporaryDirectory(dir=self.config["staging_directory"]) as restored:
            self.restic(name, "restore", snapshot, "--target", restored, "--verify")
            root = Path(restored) / str(directory).lstrip("/")
            for source in directory.iterdir():
                if digest(source) != digest(root / source.name):
                    raise BackupError(self.stage)
        self.restic(name, "tag", "--remove", "pending", "--add", "verified", snapshot)
        snapshots = json.loads(
            self.restic(name, "snapshots", "--json", "--host", self.config["host"])
        )
        matches = [
            row["id"]
            for row in snapshots
            if {name, "verified", self.run_id} <= set(row.get("tags", []))
        ]
        if len(matches) != 1:
            raise BackupError(self.stage)
        return matches[0]

    def inspect(self):
        for service in ("api", "db", "seaweed"):
            identifiers = self.docker("compose", "ps", "-q", service).decode().split()
            if len(identifiers) != 1:
                raise BackupError("container_count")
            container = json.loads(self.docker("inspect", identifiers[0]))[0]
            state = container["State"]
            if not state["Running"] or state.get("Health", {}).get("Status") != "healthy":
                raise BackupError("container_health")
            self.containers[service] = container
        volumes = [
            mount["Name"]
            for mount in self.containers["seaweed"]["Mounts"]
            if mount["Destination"] == "/data" and mount["Type"] == "volume"
        ]
        if len(volumes) != 1:
            raise BackupError("object_volume")
        return volumes[0]

    def capture(self, directory, volume):
        self.stage = "capture"
        pause_started = time.monotonic()
        try:
            for service in ("api", "seaweed"):
                identifier = self.containers[service]["Id"]
                self.resume.append(identifier)  # Also recover when stop times out.
                self.docker("stop", "--time", "30", identifier, timeout=45)
                state = json.loads(
                    self.docker("inspect", "--format", "{{json .State}}", identifier)
                )
                if state["Running"] or state["OOMKilled"] or state["ExitCode"] == 137:
                    raise BackupError("unclean_stop")
            consistency = time.time()
            application = "evaldiff-backup-" + self.run_id
            try:
                with (directory / "database.dump").open("wb") as output:
                    self.docker(
                        "exec",
                        "-e",
                        "PGAPPNAME=" + application,
                        self.containers["db"]["Id"],
                        "pg_dump",
                        "-U",
                        "evaldiff",
                        "-d",
                        "evaldiff",
                        "-Fc",
                        output=output,
                        timeout=300,
                    )
            except BaseException:
                # Killing docker exec does not guarantee termination on the daemon.
                try:
                    self.docker(
                        "exec",
                        self.containers["db"]["Id"],
                        "psql",
                        "-U",
                        "evaldiff",
                        "-d",
                        "evaldiff",
                        "-c",
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        f"WHERE application_name = '{application}' AND pid <> pg_backend_pid()",
                        timeout=20,
                        recovery=True,
                    )
                except Exception:  # noqa: BLE001, S110 - resume services even if cleanup fails
                    pass
                raise
            script = "import tarfile; t=tarfile.open(fileobj=__import__('sys').stdout.buffer,mode='w|'); t.add('/data',arcname='data'); t.close()"
            with (directory / "objects.tar").open("wb") as output:
                self.docker(
                    "run",
                    "--rm",
                    "--name",
                    self.helper,
                    "--network",
                    "none",
                    "--memory",
                    "256m",
                    "--cpus",
                    "0.5",
                    "--user",
                    "0:0",
                    "--mount",
                    f"type=volume,src={volume},dst=/data,readonly",
                    "--entrypoint",
                    "python",
                    self.containers["api"]["Image"],
                    "-c",
                    script,
                    output=output,
                    timeout=max(1, 780 - (time.monotonic() - pause_started)),
                )
        finally:
            # A timed-out Docker CLI can leave its helper running on the daemon.
            try:
                self.docker("rm", "-f", self.helper, timeout=20, recovery=True)
            except Exception:  # noqa: BLE001, S110 - missing helper is expected; resume must proceed
                pass
            failures = []
            for identifier in reversed(self.resume):
                try:
                    self.docker("start", identifier, timeout=45, recovery=True)
                except Exception:  # noqa: BLE001 - attempt recovery of every original container
                    failures.append(identifier)
            if failures:
                raise BackupError("restart_failed")
        self.stage = "restart_health"
        end = time.monotonic() + 120
        while True:
            states = [
                json.loads(
                    self.docker(
                        "inspect",
                        "--format",
                        "{{json .State}}",
                        identifier,
                        timeout=10,
                        recovery=True,
                    )
                )
                for identifier in self.resume
            ]
            if all(
                state["Running"] and state.get("Health", {}).get("Status") == "healthy"
                for state in states
            ):
                break
            if time.monotonic() >= end:
                raise BackupError("restart_health")
            time.sleep(2)
        return consistency, time.monotonic() - pause_started

    def run(self):
        volume = self.inspect()
        for name in ("data", "keys"):
            self.restic(name, "cat", "config")
        with tempfile.TemporaryDirectory(dir=self.config["staging_directory"]) as temporary:
            root = Path(temporary)
            data, keys = root / "data", root / "keys"
            data.mkdir(mode=0o700)
            keys.mkdir(mode=0o700)
            script = "import os,json; print(json.dumps({k:os.environ.get(k,'') for k in ('EVALDIFF_SECRET_KEY','EVALDIFF_PREVIOUS_SECRET_KEYS')}))"
            key_values = json.loads(
                self.docker("exec", self.containers["api"]["Id"], "python", "-c", script)
            )
            values = [
                key_values["EVALDIFF_SECRET_KEY"],
                *key_values["EVALDIFF_PREVIOUS_SECRET_KEYS"].split(","),
            ]
            identifiers = []
            for value in filter(None, (value.strip() for value in values)):
                decoded = base64.urlsafe_b64decode(value)
                if len(decoded) != 32:
                    raise BackupError("invalid_application_key")
                identifiers.append(hashlib.sha256(decoded).hexdigest())
            if not identifiers:
                raise BackupError("missing_application_key")
            write_json(keys / "keys.json", key_values)
            key_snapshot = self.publish("keys", keys)
            consistency, pause = self.capture(data, volume)
            labels = self.containers["api"]["Config"].get("Labels") or {}
            manifest = {
                "schema": 1,
                "consistency_timestamp": consistency,
                "pause_seconds": pause,
                "key_ids": identifiers,
                "key_snapshot": key_snapshot,
                "images": {name: container["Image"] for name, container in self.containers.items()},
                "revision": labels.get("org.opencontainers.image.revision"),
                "version": labels.get("org.opencontainers.image.version"),
                "sha256": {path.name: digest(path) for path in data.iterdir()},
            }
            write_json(data / "manifest.json", manifest)
            snapshot = self.publish("data", data)
            self.stage = "retention"
            self.restic(
                "data",
                "forget",
                "--host",
                self.config["host"],
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
            self.restic("data", "prune")
            return {
                "last_success": consistency,
                "completed_at": time.time(),
                "snapshot": snapshot,
                "key_snapshot": key_snapshot,
                "pause_seconds": pause,
            }


def interrupted(signum, frame):
    raise BackupError("interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    status = {}
    backup = None
    try:
        config = private_json(args.config)
        validate(config)
        status_path = Path(config["status_file"])
        lock = status_path.with_suffix(".lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if status_path.exists():
            status = private_json(status_path)
        backup = Backup(config)
        status.update(attempt_started=time.time(), outcome="running")
        write_json(status_path, status)
        status.update(backup.run(), outcome="success")
        write_json(status_path, status)
    except Exception:  # noqa: BLE001 - never expose secrets in exception diagnostics
        stage = backup.stage if backup else "configuration_or_lock"
        if backup:
            status.update(outcome="failed", failure_stage=stage, failed_at=time.time())
            write_json(status_path, status)
        print(json.dumps({"code": "backup_failed", "stage": stage}), flush=True)
        return 1
    print(json.dumps({"code": "backup_verified", "snapshot": status["snapshot"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
