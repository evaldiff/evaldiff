"""Run PostgreSQL-enabled tests in a private, socket-only disposable cluster."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deploy.backup import write_json


def command(arguments, *, output=None, environment=None, timeout=60):
    with subprocess.Popen(
        arguments,
        cwd=ROOT,
        stdout=output or subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if process.returncode:
            raise RuntimeError("qualification_command_failed")
        return stdout


def interrupted(signum, frame):
    raise RuntimeError("qualification_interrupted")


def run(args):
    os.umask(0o077)
    evidence = Path(args.evidence_directory).resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    binaries = Path(args.bin_directory).resolve()
    metadata = {
        "scope": "local_disposable_postgresql",
        "started_at": time.time(),
        "outcome": "running",
        "python": sys.version.split()[0],
        "revision": command(["git", "rev-parse", "HEAD"]).decode().strip(),
        "checkout_dirty": bool(
            command(["git", "status", "--porcelain", "--untracked-files=all"]).strip()
        ),
        "test_inputs_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for pattern in ("api/*.py", "tests/*.py", "deploy/qualification/postgres.py")
            for path in sorted(ROOT.glob(pattern))
        },
        "selection": args.tests or ["full suite"],
        "ci_status": "not_run",
        "staging_status": "not_run",
    }
    write_json(evidence / "result.json", metadata)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("EVALDIFF_", "PG"))
    }
    with tempfile.TemporaryDirectory(prefix="evaldiff-pg-") as temporary:
        root = Path(temporary)
        data, sockets = root / "data", root / "sockets"
        sockets.mkdir(mode=0o700)
        with (evidence / "cluster.log").open("wb") as output:
            try:
                metadata["postgres_binary"] = (
                    command([str(binaries / "postgres"), "--version"]).decode().strip()
                )
                if not metadata["postgres_binary"].startswith("postgres (PostgreSQL) 16."):
                    raise RuntimeError("postgresql_16_required")
                command(
                    [
                        str(binaries / "initdb"),
                        "-D",
                        str(data),
                        "-U",
                        "evaldiff",
                        "--encoding=UTF8",
                        "--locale=C",
                        "--auth-local=trust",
                        "--auth-host=reject",
                    ],
                    output=output,
                    environment=environment,
                )
                command(
                    [
                        str(binaries / "pg_ctl"),
                        "-D",
                        str(data),
                        "-l",
                        str(evidence / "server.log"),
                        "-o",
                        f"-c listen_addresses='' -c unix_socket_directories='{sockets}' -p 5432",
                        "-w",
                        "start",
                    ],
                    output=output,
                    environment=environment,
                )
                command(
                    [str(binaries / "createdb"), "-h", str(sockets), "-U", "evaldiff", "evaldiff"],
                    output=output,
                    environment=environment,
                )
                url = f"postgresql+psycopg://evaldiff@/evaldiff?host={sockets}&port=5432"
                environment["EVALDIFF_TEST_POSTGRES_URL"] = url
                import psycopg
                import sqlalchemy

                metadata.update(psycopg=psycopg.__version__, sqlalchemy=sqlalchemy.__version__)
                test_command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--junitxml=" + str(evidence / "junit.xml"),
                    *(args.tests or []),
                ]
                metadata["command"] = test_command
                with (evidence / "pytest.log").open("wb") as test_output:
                    command(test_command, output=test_output, environment=environment, timeout=900)
                metadata["outcome"] = "pass"
            except Exception:  # noqa: BLE001 - diagnostics and environment may contain private data
                metadata["outcome"] = "fail"
            finally:
                if (data / "postmaster.pid").exists():
                    try:
                        command(
                            [str(binaries / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                            output=output,
                            environment=environment,
                        )
                    except Exception:  # noqa: BLE001 - do not expose cluster diagnostics
                        metadata["outcome"] = "fail"
                        metadata["cleanup_failed"] = True
                metadata["completed_at"] = time.time()
                if (evidence / "junit.xml").exists():
                    suites = ET.parse(evidence / "junit.xml").getroot().iter("testsuite")
                    metadata["counts"] = {
                        key: 0 for key in ("tests", "failures", "errors", "skipped")
                    }
                    for suite in suites:
                        for key in metadata["counts"]:
                            metadata["counts"][key] += int(suite.get(key, 0))
                if (evidence / "junit.xml").exists():
                    metadata["postgresql_parameterized_cases"] = sum(
                        "[postgresql]" in case.get("name", "")
                        for case in ET.parse(evidence / "junit.xml").getroot().iter("testcase")
                    )
                write_json(evidence / "result.json", metadata)
    print(
        json.dumps(
            {
                "code": "postgres_qualification_" + metadata["outcome"],
                "counts": metadata.get("counts", {}),
                "evidence_directory": str(evidence),
            }
        )
    )
    return 0 if metadata["outcome"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-directory", required=True)
    parser.add_argument("--evidence-directory", required=True)
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return run(args)
    except Exception:  # noqa: BLE001 - fixed diagnostics, no connection credentials
        print('{"code":"postgres_qualification_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
