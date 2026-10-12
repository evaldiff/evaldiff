"""Isolated staging scenarios and raw qualification evidence (Python 3.11+)."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

REQUEST_TIMEOUT = 10
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from deploy.backup import private_json, write_json
from deploy.check_deployment import validate_origin
from deploy.monitor import byte_size


class QualificationError(Exception):
    pass


def load_config(path):
    config = private_json(path)
    if config.get("purpose") != "isolated-staging" or not re.fullmatch(
        r"evaldiff-staging-[a-z0-9-]+", config.get("project", "")
    ):
        raise QualificationError("isolated_project_required")
    for name in ("base_url", "fixture_url"):
        validate_origin(config[name], public=True)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", config["revision"]):
        raise QualificationError("candidate_revision_required")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", config["image_id"]):
        raise QualificationError("candidate_image_id_required")
    if config.get("fixture_version") != 1 or not config.get("fixture_key"):
        raise QualificationError("fixture_contract_required")
    if Path(config["environment_file"]).stat().st_mode & 0o077:
        raise QualificationError("environment_permissions")
    return config


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def mix(seed):
    rng = random.Random(seed)
    while True:
        block = ["healthy"] * 16 + ["slow"] * 2 + ["error", "stall"]
        rng.shuffle(block)
        yield from block


class Harness:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config["evidence_directory"])
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.accounts = []
        self.records = []
        self.runs = []
        self.exclusion_until = 0
        self.client = httpx.AsyncClient(
            base_url=config["base_url"], timeout=10, follow_redirects=False, trust_env=False
        )

    def command(self, args, *, timeout=30, input=None, env=None):
        try:
            return subprocess.run(
                args,
                cwd=ROOT / "deploy",
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                input=input if isinstance(input, bytes) else None,
                stdin=input if input is not None and not isinstance(input, bytes) else None,
                env=env,
            ).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise QualificationError("command_failed") from error

    def compose(self, *args, timeout=30, input=None, overrides=None):
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("EVALDIFF_") and k not in ("POSTGRES_PASSWORD", "SEAWEEFS_SECRET")
        }
        environment.update(
            EVALDIFF_STAGING_PROJECT=self.config["project"],
            EVALDIFF_STAGING_IMAGE=self.config["image"],
            EVALDIFF_STAGING_HOST=urlsplit(self.config["base_url"]).hostname,
        )
        environment.update(overrides or {})
        return self.command(
            [
                "docker",
                "compose",
                "--env-file",
                self.config["environment_file"],
                "-f",
                str(ROOT / "deploy/staging-compose.yml"),
                "-p",
                self.config["project"],
                *args,
            ],
            timeout=timeout,
            input=input if isinstance(input, bytes) else None,
            stdin=input if input is not None and not isinstance(input, bytes) else None,
            env=environment,
        )

    def record(self, kind, **fields):
        record = {"time": time.time(), "kind": kind, **fields}
        self.records.append(record)
        path = self.directory / "events.jsonl"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as output:
            output.write(json.dumps(record) + "\n")

    def freeze(self):
        public = {
            name: self.config[name]
            for name in (
                "purpose",
                "project",
                "base_url",
                "fixture_url",
                "fixture_version",
                "version",
                "revision",
                "image",
                "image_id",
                "seed",
                "volume_path",
            )
        }
        public["contract_sha256"] = hashlib.sha256(
            (ROOT / "deploy/OPERATIONS.md").read_bytes()
        ).hexdigest()
        path = self.directory / "manifest.json"
        if path.exists() and private_json(path) != public:
            raise QualificationError("evidence_candidate_changed")
        write_json(path, public)

    def check_image(self):
        image = json.loads(self.command(["docker", "image", "inspect", self.config["image"]]))[0]
        labels = image["Config"].get("Labels") or {}
        if (
            image["Id"] != self.config["image_id"]
            or labels.get("org.opencontainers.image.revision") != self.config["revision"]
            or labels.get("org.opencontainers.image.version") != self.config["version"]
        ):
            raise QualificationError("candidate_image_mismatch")

    async def request(
        self,
        method,
        path,
        account=None,
        body=None,
        *,
        category="control",
        expected=200,
        measured=True,
    ):
        started = time.monotonic()
        headers = {"Authorization": "Bearer " + account["key"]} if account else {}
        code, content = 0, b""
        isolated = False
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self.client.stream(method, path, json=body, headers=headers) as response:
                    code = response.status_code
                    isolated = (
                        response.headers.get("X-Evaldiff-Staging-Project") == self.config["project"]
                    )
                    async for chunk in response.aiter_bytes():
                        content += chunk
                        if len(content) > 8 * 1024**2:
                            raise QualificationError("response_limit")
        except (httpx.HTTPError, QualificationError, TimeoutError):
            code = 0
        elapsed = time.monotonic() - started
        self.record(
            "request",
            category=category,
            status=code,
            expected=expected,
            seconds=elapsed,
            measured=measured,
            excluded=time.time() <= self.exclusion_until,
        )
        if code != expected or not isolated:
            raise QualificationError("unexpected_http_status")
        if expected >= 400:
            return {}
        return content.decode() if path.split("?")[0].endswith(".md") else json.loads(content)

    def stack(self):
        metadata = {}
        for service in ("api", "db", "seaweed", "caddy"):
            ids = self.compose("ps", "-q", service).decode().split()
            if len(ids) != 1:
                raise QualificationError("staging_service_count")
            item = json.loads(self.command(["docker", "inspect", ids[0]]))[0]
            if item["Config"]["Labels"].get("com.docker.compose.project") != self.config["project"]:
                raise QualificationError("staging_service_project")
            if service == "api":
                if item["Image"] != self.config["image_id"]:
                    raise QualificationError("running_candidate_mismatch")
                environment = dict(value.split("=", 1) for value in item["Config"]["Env"])
                if (
                    environment.get("EVALDIFF_PRODUCTION") != "true"
                    or environment.get("EVALDIFF_ALLOW_LOCAL_ENDPOINTS", "false") != "false"
                ):
                    raise QualificationError("production_settings_required")
            metadata[service] = {"image_id": item["Image"], "reference": item["Config"]["Image"]}
        metadata["packages"] = json.loads(
            self.compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import importlib.metadata as m, json,platform;print(json.dumps({'python':platform.python_version(),**{p:m.version(p) for p in ('evaldiff','psycopg','boto3','SQLAlchemy','fastapi','uvicorn')}}))",
            )
        )
        metadata["postgres_version"] = (
            self.compose("exec", "-T", "db", "postgres", "--version").decode().strip()
        )
        metadata["seaweed_version"] = (
            self.compose("exec", "-T", "seaweed", "weed", "version").decode().strip()
        )
        metadata["caddy_version"] = (
            self.compose("exec", "-T", "caddy", "caddy", "version").decode().strip()
        )
        write_json(self.directory / "stack.json", metadata)

    async def identity(self):
        health = await self.request("GET", "/health")
        ready = await self.request("GET", "/ready")
        if (
            health.get("revision") != self.config["revision"]
            or health.get("version") != self.config["version"]
            or ready.get("status") != "ready"
            or not all(
                ready.get("checks", {}).get(k) is True
                for k in ("database", "storage", "worker", "probe_fresh")
            )
        ):
            raise QualificationError("identity_or_readiness")
        await self.request("GET", "/ops", expected=404)

    async def provision(self):
        path = self.directory / "accounts.json"
        if path.exists():
            self.accounts = private_json(path)
        else:
            self.accounts = []
            for index in range(10):
                account = await self.request(
                    "POST",
                    "/v1/auth/signup",
                    body={
                        "email": f"{self.config['project']}-{index}-{time.time_ns()}@example.invalid"
                    },
                    expected=201,
                )
                self.accounts.append(account)
                write_json(path, self.accounts)
                if index < 9:
                    await asyncio.sleep(13)  # Keep the production signup limiter enabled.
        if len(self.accounts) != 10:
            raise QualificationError("incomplete_account_provisioning")
        for account in self.accounts:
            usage = await self.request("GET", "/v1/usage", account)
            if usage["quota"] != 100000:
                raise QualificationError("staging_quota_required")
            if "dataset" not in account:
                dataset = await self.upload(account, 20)
                account["dataset"] = dataset["id"]
                write_json(path, self.accounts)

    async def upload(self, account, count, measured=True):
        return await self.request(
            "POST",
            "/v1/datasets",
            account,
            {
                "name": "qualification",
                "cases": [
                    {"input": f"case-{index}", "expected": "qualification-ok", "rubric": []}
                    for index in range(count)
                ],
            },
            expected=201,
            category="upload",
            measured=measured,
        )

    async def submit(self, account, behavior="healthy", dataset=None, measured=True):
        run = await self.request(
            "POST",
            "/v1/runs",
            account,
            {
                "dataset_id": dataset or account["dataset"],
                "model": "qualification-fixture-v1",
                "endpoint": self.config["fixture_url"].rstrip("/") + f"/{behavior}/v1",
                "api_key": self.config["fixture_key"],
            },
            expected=202,
            measured=measured,
        )
        item = {
            "id": run["id"],
            "account": account,
            "behavior": behavior,
            "accepted_at": time.time(),
            "last_poll": 0,
            "terminal": False,
            "measured": measured,
        }
        self.runs.append(item)
        self.record("accepted", run_id=run["id"], behavior=behavior, measured=measured)
        return item

    async def poll(self, item, measured=True):
        item["last_poll"] = time.monotonic()
        result = await self.request(
            "GET", f"/v1/runs/{item['id']}", item["account"], measured=measured
        )
        item["status"] = result["status"]
        if result["status"] in ("done", "failed"):
            item["terminal"] = True
            item["result"] = {
                key: result[key] for key in ("status", "total_cases", "passed_cases", "avg_score")
            }
            self.record(
                "terminal",
                run_id=item["id"],
                behavior=item["behavior"],
                **item["result"],
                measured=item["measured"],
            )
        return result

    async def wait(self, item):
        deadline = time.monotonic() + 1990
        while time.monotonic() < deadline:
            result = await self.poll(item)
            if item["terminal"]:
                return result
            await asyncio.sleep(10)
        raise QualificationError("run_drain_timeout")

    async def e2e(self):
        await self.identity()
        await self.provision()
        account, other = self.accounts[:2]
        first = await self.submit(account)
        first_result = await self.wait(first)
        second = await self.submit(account)
        second_result = await self.wait(second)
        for result in (first_result, second_result):
            if result["status"] != "done" or result["passed_cases"] != 20:
                raise QualificationError("synthetic_run_failed")
        cases = await self.request("GET", f"/v1/runs/{first['id']}/cases", account)
        if sorted(case["seq"] for case in cases) != list(range(20)):
            raise QualificationError("duplicate_or_missing_cases")
        await self.request("GET", f"/v1/datasets/{account['dataset']}", other, expected=404)
        await self.request("GET", f"/v1/runs/{first['id']}", other, expected=404)
        difference = await self.request(
            "GET", f"/v1/runs/{second['id']}/diff?compare={first['id']}", account, category="diff"
        )
        report = await self.request(
            "GET", f"/v1/runs/{second['id']}/report.md", account, category="report"
        )
        if difference.get("regressions") or not report.strip():
            raise QualificationError("report_or_diff_failed")
        write_json(
            self.directory / "markers.json",
            {"account": account, "runs": [first["id"], second["id"]], "created_at": time.time()},
        )
        self.record("e2e", outcome="pass")

    def resources(self):
        ids = self.compose("ps", "-q").decode().split()
        if len(ids) != 4:
            raise QualificationError("resource_services_missing")
        rows = [
            json.loads(line)
            for line in self.command(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids]
            ).splitlines()
        ]
        states = json.loads(self.command(["docker", "inspect", *ids]))
        names = {
            state["Name"].lstrip("/"): state["Config"]["Labels"]["com.docker.compose.service"]
            for state in states
        }
        memory = {names[row["Name"]]: byte_size(row["MemUsage"].split("/")[0]) for row in rows}
        if set(memory) != {"api", "db", "seaweed", "caddy"}:
            raise QualificationError("resource_services_missing")
        disk = shutil.disk_usage(self.config["volume_path"])
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        operations = json.loads(
            self.compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ops',timeout=2).read().decode())",
            )
        )
        return {
            "memory": memory,
            "disk_used_bytes": disk.used,
            "disk_free_bytes": disk.free,
            "host_available_bytes": available,
            "oom": any(s["State"]["OOMKilled"] for s in states),
            "operations": operations,
        }

    def persisted_runs(self):
        ids = ",".join(str(int(item["id"])) for item in self.runs)
        if not ids:
            return []
        query = (
            """BEGIN READ ONLY; SET LOCAL statement_timeout = '2000ms';
        SELECT COALESCE(json_agg(t), '[]'::json) FROM (
          SELECT r.id, r.status, r.total_cases,
            extract(epoch FROM r.started_at - r.created_at) AS latest_claim_wait_seconds,
            (SELECT max(j.attempts) FROM jobs j WHERE j.kind='run'
              AND j.payload_json::json->>'run_id'=r.id::text) AS attempts,
            (SELECT count(*) FROM run_cases c WHERE c.run_id=r.id) AS case_rows,
            (SELECT count(DISTINCT c.seq) FROM run_cases c WHERE c.run_id=r.id) AS distinct_cases,
            (SELECT count(*) FROM run_cases c WHERE c.run_id=r.id AND c.error IS NULL) AS billable_cases,
            q.settled, q.charged, q.reserved
          FROM runs r LEFT JOIN run_reservations q ON q.run_id=r.id
          WHERE r.id IN ("""
            + ids
            + ") ORDER BY r.id) t; COMMIT;"
        )
        result = json.loads(
            self.compose(
                "exec", "-T", "db", "psql", "-U", "evaldiff", "-d", "evaldiff", "-qAt", "-c", query
            )
        )
        write_json(self.directory / "persisted-runs.json", result)
        return result

    async def restart(self):
        self.exclusion_until = time.time() + 225
        self.record("restart_started")
        try:
            await asyncio.to_thread(self.compose, "stop", "--timeout", "30", "api", timeout=45)
            ids = self.compose("ps", "-aq", "api").decode().split()
            states = json.loads(self.command(["docker", "inspect", *ids]))
            if (
                len(states) != 1
                or states[0]["State"]["ExitCode"] == 137
                or states[0]["State"]["OOMKilled"]
            ):
                raise QualificationError("forced_shutdown")
        finally:
            await asyncio.to_thread(self.compose, "start", "api")
            self.exclusion_until = time.time() + 180
            self.record("restart_finished", exclusion_until=self.exclusion_until)

    async def fairness(self):
        await self.identity()
        await self.provision()
        items = [await self.submit(self.accounts[0], "slow") for _ in range(10)]
        for account in self.accounts[1:]:
            items.append(await self.submit(account, "slow"))
        account = self.accounts[0]
        await self.request(
            "POST",
            "/v1/runs",
            account,
            {
                "dataset_id": account["dataset"],
                "model": "qualification-fixture-v1",
                "endpoint": self.config["fixture_url"] + "/slow/v1",
                "api_key": self.config["fixture_key"],
            },
            expected=429,
        )
        for item in items:
            result = await self.wait(item)
            if result["status"] != "done":
                raise QualificationError("fairness_run_failed")
        rows = self.persisted_runs()
        others = {item["id"] for item in items[10:]}
        if any(
            row["attempts"] != 1
            or row["latest_claim_wait_seconds"] is None
            or row["latest_claim_wait_seconds"] > 180
            for row in rows
            if row["id"] in others
        ):
            raise QualificationError("fairness_wait_failed")
        self.record(
            "fairness_finished",
            remaining_evidence="continuous global/per-account active-slot bounds",
        )

    async def sample(self, stop):
        while not stop.is_set():
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(self.resources)
                self.record("resources", **result)
            except Exception:  # noqa: BLE001 - preserve a missing sample without sensitive diagnostics
                self.record("resource_sample_failed")
            duration = time.monotonic() - started
            if duration > 5:
                self.record("resource_sample_late", seconds=duration)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.01, 5 - duration))
            except TimeoutError:
                continue

    async def load(self, phase):
        await self.identity()
        await self.provision()
        duration = 3600 if phase == "baseline" else 86400
        self.record(
            "phase",
            name=phase,
            warmup_seconds=900,
            duration_seconds=duration,
            seed=self.config["seed"],
        )
        behaviors = mix(self.config["seed"])
        self.records = []
        start = time.monotonic()
        stop = asyncio.Event()
        sampler = asyncio.create_task(self.sample(stop))
        tasks = set()
        counts = {"upload": 0, "report": 0, "diff": 0}
        tick, submitted, reads = 0, 0, 0
        failed = False
        restarted = set()

        async def perform(operation):
            nonlocal failed
            try:
                await operation
            except Exception as error:  # noqa: BLE001 - fixed diagnostics, consume every task exception
                if str(error) != "unexpected_http_status" or time.time() > self.exclusion_until:
                    failed = True
                self.record("operation_failed", excluded=time.time() <= self.exclusion_until)

        try:
            while time.monotonic() - start < 900 + duration:
                elapsed = time.monotonic() - start
                measured = elapsed >= 900
                if elapsed - tick * 0.5 >= 1:
                    self.record("arrival_missed")
                    failed = True
                    tick = math.ceil(elapsed * 2)
                if phase == "soak":
                    for hour in (6, 18):
                        if (
                            hour not in restarted
                            and elapsed >= 900 + hour * 3600
                            and any(item.get("status") == "running" for item in self.runs)
                        ):
                            task = asyncio.create_task(perform(self.restart()))
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)
                            restarted.add(hour)
                operation = None
                if submitted <= elapsed // 60:
                    account = self.accounts[submitted % 10]
                    behavior = next(behaviors) if phase == "soak" else "healthy"
                    operation = self.submit(account, behavior, measured=measured)
                    submitted += 1
                else:
                    pending = [
                        run
                        for run in self.runs
                        if not run["terminal"] and time.monotonic() - run["last_poll"] >= 10
                    ]
                    done = [
                        run for run in self.runs if run.get("result", {}).get("status") == "done"
                    ]
                    if pending:
                        item = min(pending, key=lambda run: run["last_poll"])
                        item["last_poll"] = time.monotonic()
                        operation = self.poll(item, measured=measured)
                    elif measured and counts["upload"] < 100:
                        operation = self.upload(self.accounts[reads % 10], 20)
                        counts["upload"] += 1
                    elif measured and done and counts["report"] < 100:
                        item = done[reads % len(done)]
                        operation = self.request(
                            "GET",
                            f"/v1/runs/{item['id']}/report.md",
                            item["account"],
                            category="report",
                        )
                        counts["report"] += 1
                    elif measured and done and counts["diff"] < 100:
                        item = done[reads % len(done)]
                        operation = self.request(
                            "GET",
                            f"/v1/runs/{item['id']}/diff?compare={item['id']}",
                            item["account"],
                            category="diff",
                        )
                        counts["diff"] += 1
                    else:
                        operation = self.request(
                            "GET", "/v1/usage", self.accounts[reads % 10], measured=measured
                        )
                    reads += 1
                if len(tasks) >= 8:
                    operation.close()
                    failed = True
                    self.record("arrival_missed")
                else:
                    task = asyncio.create_task(perform(operation))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                tick += 1
                await asyncio.sleep(max(0, start + tick * 0.5 - time.monotonic()))
            await asyncio.gather(*tasks)
            for item in self.runs:
                if not item["terminal"]:
                    await self.wait(item)
            await asyncio.to_thread(self.persisted_runs)
            if failed or phase == "soak" and restarted != {6, 18}:
                raise QualificationError("load_requests_or_restarts_failed")
        finally:
            stop.set()
            await sampler
            write_json(self.directory / f"{phase}-summary.json", self.summary())
        self.record("phase_finished", name=phase)

    def summary(self):
        requests = [
            row
            for row in self.records
            if row["kind"] == "request" and row.get("measured") and not row.get("excluded")
        ]
        statistics = {
            category: {
                "samples": len(
                    values := [row["seconds"] for row in requests if row["category"] == category]
                ),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
                "max": max(values) if values else None,
            }
            for category in ("control", "upload", "report", "diff")
        }
        return {
            "outcome": "incomplete",
            "latencies": statistics,
            "unexpected_requests": sum(row["status"] != row["expected"] for row in requests),
            "required_evidence": [
                "persisted first-claim and retry queue waits",
                "hourly soak gates",
                "memory stability",
                "restart/fault recovery",
                "accounting correctness",
                "fairness",
                "large datasets",
                "Part C acceptance",
                "RPO/RTO and rollback",
            ],
        }


async def main_async(args):
    config = load_config(args.config)
    harness = Harness(config)
    try:
        harness.freeze()
        if args.mode != "up":
            harness.stack()
        if args.mode == "up":
            if harness.compose("ps", "-aq").strip():
                raise QualificationError("staging_target_not_empty")
            harness.check_image()
            harness.compose("config", "--quiet")
            harness.compose(
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", timeout=150
            )
            harness.stack()
            await harness.identity()
        elif args.mode == "e2e":
            await harness.e2e()
        elif args.mode in ("baseline", "soak"):
            await harness.load(args.mode)
        elif args.mode == "large":
            await harness.identity()
            await harness.provision()
            stop = asyncio.Event()
            sampler = asyncio.create_task(harness.sample(stop))
            try:
                for account in harness.accounts:
                    dataset = await harness.upload(account, 5000)
                    item = await harness.submit(account, dataset=dataset["id"])
                    result = await harness.wait(item)
                    if result["status"] != "done" or result["passed_cases"] != 5000:
                        raise QualificationError("large_dataset_failed")
                harness.persisted_runs()
            finally:
                stop.set()
                await sampler
        elif args.mode == "fairness":
            await harness.fairness()
        elif args.mode == "restart":
            await harness.restart()
        elif args.mode == "outage":
            harness.record("fault_started", service=args.service)
            try:
                harness.compose("stop", "--timeout", "30", args.service, timeout=45)
                await asyncio.sleep(12)
                await harness.request("GET", "/ready", expected=503)
            finally:
                harness.compose("start", args.service)
                harness.record("fault_finished", service=args.service)
            deadline = time.monotonic() + 120
            while True:
                try:
                    await harness.identity()
                    break
                except QualificationError:
                    if time.monotonic() > deadline:
                        raise QualificationError("outage_recovery_timeout") from None
                    await asyncio.sleep(2)
        harness.record("command_finished", mode=args.mode)
        return 0
    finally:
        await harness.client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "mode", choices=("up", "e2e", "baseline", "soak", "large", "fairness", "restart", "outage")
    )
    parser.add_argument("--service", choices=("db", "seaweed"), default="db")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return asyncio.run(main_async(args))
    except Exception:  # noqa: BLE001 - suppress credentials and response bodies
        print(json.dumps({"code": "qualification_failed", "mode": args.mode}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
