"""Run coordinated backup against the explicitly configured isolated staging stack."""

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.backup import Backup, interrupted, private_json, validate, write_json
from deploy.qualification.run import Harness, QualificationError, load_config


class StagingBackup(Backup):
    def __init__(self, config, harness):
        super().__init__(config)
        self.harness = harness

    def docker(self, *arguments, **kwargs):
        if arguments[0] == "compose":
            return self.harness.compose(*arguments[1:], timeout=kwargs.get("timeout", 120))
        return super().docker(*arguments, **kwargs)


async def run(args):
    config = load_config(args.config)
    backup_config = private_json(args.backup_config)
    validate(backup_config)
    if backup_config["host"] != config["project"]:
        raise QualificationError("staging_backup_host_mismatch")
    harness = Harness(config)
    status_path = Path(backup_config["status_file"])
    backup = StagingBackup(backup_config, harness)
    status = {}
    try:
        harness.freeze()
        harness.stack()
        await harness.identity()
        with status_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if status_path.exists():
                status = private_json(status_path)
            status.update(outcome="running", attempt_started=time.time())
            write_json(status_path, status)
            try:
                result = backup.run()
            except Exception:
                status.update(outcome="failed", failure_stage=backup.stage, failed_at=time.time())
                write_json(status_path, status)
                raise
            status.update(result, outcome="success")
            write_json(status_path, status)
            harness.record(
                "backup_verified", snapshot=result["snapshot"], key_snapshot=result["key_snapshot"]
            )
    finally:
        await harness.client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--backup-config", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        asyncio.run(run(args))
    except Exception:  # noqa: BLE001 - never disclose repository/key material
        print(json.dumps({"code": "staging_backup_failed"}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
