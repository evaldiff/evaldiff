"""Restore a verified point into an EMPTY isolated staging project; retain drill evidence."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.backup import Backup, digest, private_json, validate, write_json
from deploy.qualification.run import Harness, QualificationError, load_config


def checked_snapshot(backup, name, identifier):
    if not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise QualificationError("full_snapshot_id_required")
    rows = json.loads(backup.restic(name, "snapshots", "--json", "--host", backup.config["host"]))
    if not any(
        row["id"] == identifier and {name, "verified"} <= set(row.get("tags", [])) for row in rows
    ):
        raise QualificationError("verified_snapshot_required")


def check_archive(path):
    with tarfile.open(path) as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if (
                not parts
                or parts[0] != "data"
                or ".." in parts
                or member.name.startswith("/")
                or not (member.isfile() or member.isdir())
            ):
                raise QualificationError("unsafe_object_archive")


def check_key_ids(values, expected):
    keys = [values["EVALDIFF_SECRET_KEY"], *values["EVALDIFF_PREVIOUS_SECRET_KEYS"].split(",")]
    identifiers = []
    for key in filter(None, (value.strip() for value in keys)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}=", key):
            raise QualificationError("invalid_restore_key")
        identifiers.append(hashlib.sha256(base64.urlsafe_b64decode(key)).hexdigest())
    if identifiers != expected:
        raise QualificationError("restore_keys_mismatch")


async def restore(args):
    started = time.monotonic()
    config = load_config(args.config)
    if not config["project"].startswith("evaldiff-staging-restore-"):
        raise QualificationError("restore_project_required")
    harness = Harness(config)
    backup_config = private_json(args.backup_config)
    validate(backup_config)
    backup = Backup(backup_config)
    try:
        harness.freeze()
        harness.check_image()
        if (
            harness.compose("ps", "-aq").strip()
            or harness.command(
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + config["project"],
                ]
            ).strip()
        ):
            raise QualificationError("restore_target_not_empty")
        harness.record("recovery_declared", incident_timestamp=args.incident_timestamp)
        checked_snapshot(backup, "data", args.snapshot)
        with tempfile.TemporaryDirectory(dir=backup_config["staging_directory"]) as temporary:
            root = Path(temporary)
            backup.restic(
                "data", "restore", args.snapshot, "--target", str(root / "data"), "--verify"
            )
            manifests = list((root / "data").rglob("manifest.json"))
            if len(manifests) != 1:
                raise QualificationError("restore_manifest_required")
            manifest_path = manifests[0]
            manifest = json.loads(manifest_path.read_text())
            if (
                manifest.get("schema") != 1
                or manifest.get("revision") != config["revision"]
                or manifest.get("version") != config["version"]
                or manifest["images"]["api"] != config["image_id"]
                or set(manifest["sha256"]) != {"database.dump", "objects.tar"}
            ):
                raise QualificationError("restore_manifest_mismatch")
            for name, expected in manifest["sha256"].items():
                if digest(manifest_path.parent / name) != expected:
                    raise QualificationError("restore_digest_mismatch")
            check_archive(manifest_path.parent / "objects.tar")
            checked_snapshot(backup, "keys", manifest["key_snapshot"])
            backup.restic(
                "keys",
                "restore",
                manifest["key_snapshot"],
                "--target",
                str(root / "keys"),
                "--verify",
            )
            files = list((root / "keys").rglob("keys.json"))
            if len(files) != 1:
                raise QualificationError("restore_key_bundle_required")
            keys = private_json(files[0])
            check_key_ids(keys, manifest["key_ids"])
            # Persist matching keys privately so later target commands use the same configuration.
            environment = Path(config["environment_file"]).read_text().splitlines()
            names = {
                "EVALDIFF_SECRET_KEY",
                "EVALDIFF_PREVIOUS_SECRET_KEYS",
                "EVALDIFF_STAGING_DATABASE_IMAGE",
                "EVALDIFF_STAGING_STORAGE_IMAGE",
            }
            environment = [
                line for line in environment if line.partition("=")[0].strip() not in names
            ]
            environment += [
                name + "=" + keys[name]
                for name in ("EVALDIFF_SECRET_KEY", "EVALDIFF_PREVIOUS_SECRET_KEYS")
            ]
            environment += [
                "EVALDIFF_STAGING_DATABASE_IMAGE=" + manifest["images"]["db"],
                "EVALDIFF_STAGING_STORAGE_IMAGE=" + manifest["images"]["seaweed"],
            ]
            environment_path = harness.directory / "restore.env"
            with environment_path.open("x") as output:
                output.write("\n".join(environment) + "\n")
            config["environment_file"] = str(environment_path)
            write_json(harness.directory / "restore-config.json", config)
            harness.compose(
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "db", timeout=150
            )
            harness.compose("create", "--no-build", "seaweed")
            identifier = harness.compose("ps", "-aq", "seaweed").decode().strip()
            container = json.loads(harness.command(["docker", "inspect", identifier]))[0]
            volume = next(
                mount["Name"]
                for mount in container["Mounts"]
                if mount["Destination"] == "/data" and mount["Type"] == "volume"
            )
            # Stream dump/archive rather than loading large backups in host memory.
            with (manifest_path.parent / "database.dump").open("rb") as source:
                harness.compose(
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "-U",
                    "evaldiff",
                    "-d",
                    "evaldiff",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    input=source,
                    timeout=900,
                )
            script = "import tarfile,sys;t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|');t.extractall('/target',filter='data')"
            with (manifest_path.parent / "objects.tar").open("rb") as source:
                harness.command(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-i",
                        "--network",
                        "none",
                        "--user",
                        "0:0",
                        "--mount",
                        f"type=volume,src={volume},dst=/target/data",
                        "--entrypoint",
                        "python",
                        config["image_id"],
                        "-c",
                        script,
                    ],
                    input=source,
                    timeout=900,
                )
            harness.compose(
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", timeout=150
            )
            harness.compose("exec", "-T", "api", "python", "-m", "api.migrate_secrets", "--check")
            await harness.identity()
            markers = private_json(args.markers)
            if markers["created_at"] > manifest["consistency_timestamp"]:
                raise QualificationError("markers_postdate_recovery_point")
            account = markers["account"]
            await harness.request("GET", f"/v1/datasets/{account['dataset']}", account)
            for run_id in markers["runs"]:
                result = await harness.request("GET", f"/v1/runs/{int(run_id)}", account)
                if result["status"] != "done":
                    raise QualificationError("restored_marker_run_incomplete")
                await harness.request("GET", f"/v1/runs/{int(run_id)}/report.md", account)
            item = await harness.submit(account)
            result = await harness.wait(item)
            if result["status"] != "done" or result["passed_cases"] != 20:
                raise QualificationError("restored_dataset_execution_failed")
            rto = time.monotonic() - started
            rpo = args.incident_timestamp - manifest["consistency_timestamp"]
            outcome = "pass" if 0 <= rpo <= 21600 and rto <= 3600 else "fail"
            write_json(
                harness.directory / "restore-result.json",
                {
                    "outcome": outcome,
                    "scope": "measured_recovery_checks",
                    "qualification_outcome": "incomplete",
                    "snapshot": args.snapshot,
                    "key_snapshot": manifest["key_snapshot"],
                    "rto_seconds": rto,
                    "rpo_seconds": rpo,
                    "known_marker_runs": markers["runs"],
                    "remaining_evidence": [
                        "enumerate acknowledged writes lost after the recovery point"
                    ],
                },
            )
            if outcome != "pass":
                raise QualificationError("recovery_targets_failed")
    finally:
        await harness.client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "backup-config", "snapshot", "markers"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--incident-timestamp", required=True, type=float)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(restore(args))
    except Exception:  # noqa: BLE001 - repository credentials/key values must never reach logs
        print('{"code":"restore_drill_failed"}', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
