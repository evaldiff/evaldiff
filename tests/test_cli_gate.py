"""Part E — CLI gate tests.

`run`/`diff` are exercised against an in-process mock API (httpx.MockTransport
swapped in via `main._gateway`), so every gate path is tested without network:
pass, fail, server-side failure, deadline, auth, 429 retry, incomplete-run and
invalid-comparison rejections, report writing, and the exit-code contract.
"""

from __future__ import annotations

import re

import httpx
import pytest
from typer.testing import CliRunner

import evaldiff_cli.main as cli


class MockAPI:
    """In-process evaldiff API for the CLI tests."""

    def __init__(self) -> None:
        self.runs: dict[int, dict] = {}
        self.run_cases: dict[int, list[dict]] = {}
        self.outputs: dict[tuple[int, int], dict] = {}
        self.dataset: dict = {
            "name": "ds",
            "cases": [
                # Deliberately NO "seq": real datasets are user JSON and the
                # API assigns seq as the 0-based position — the CLI must cope
                # (regression guard for the report join key).
                {"input": "in0", "expected": "exp0", "rubric": None},
                {"input": "in1", "expected": "exp1", "rubric": None},
            ],
        }
        self.diff = None
        self.diff_status = 200
        self.signup = None
        self.signup_status = 201
        self.signup_body = {"email": "a@b.se", "key": "sk-test-1", "quota": 100}
        # (status, body, headers); queue entries are popped one per POST /v1/runs
        self.submit_response = (202, {"id": 41, "status": "queued", "dataset_id": 7}, {})
        self.submit_queue: list[tuple[int, dict, dict]] = []
        # Fault injection: path -> times to answer 500 before passing through
        self.fail_once: dict[str, int] = {}
        # Fault injection: path -> times to raise a transport error
        self.raise_transport: dict[str, int] = {}
        # Calls per "METHOD /path" — proves creation requests are not retried
        self.request_counts: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        m, p = request.method, request.url.path
        self.request_counts[f"{m} {p}"] = self.request_counts.get(f"{m} {p}", 0) + 1
        if p in self.raise_transport and self.raise_transport[p] > 0:
            self.raise_transport[p] -= 1
            raise httpx.ConnectError("connection reset by peer")
        if p in self.fail_once and self.fail_once[p] > 0:
            self.fail_once[p] -= 1
            return httpx.Response(500, json={"detail": "injected 500"})
        if m == "POST" and p == "/v1/auth/signup":
            return httpx.Response(self.signup_status, json=self.signup_body)
        if m == "POST" and p == "/v1/runs":
            st, body, headers = (
                self.submit_queue.pop(0) if self.submit_queue else self.submit_response
            )
            return httpx.Response(st, json=body, headers=headers)
        if m == "GET" and p in (
            "/v1/datasets/7/cases",
            "/v1/datasets/8/cases",
        ):
            return httpx.Response(200, json=self.dataset)
        if m == "GET" and p.startswith("/v1/runs/"):
            parts = p.split("/")
            rid = int(parts[3])
            if len(parts) == 4 and rid in self.runs:
                return httpx.Response(200, json=self.runs[rid])
            if len(parts) == 4:
                return httpx.Response(404, json={"detail": "run not found"})
            if parts[4] == "cases" and len(parts) == 5:
                if rid in self.run_cases:
                    return httpx.Response(200, json=self.run_cases[rid])
                return httpx.Response(404, json={"detail": "not found"})
            if (
                parts[4] == "cases"
                and len(parts) == 7
                and parts[5].isdigit()
                and parts[6] == "output"
            ):
                key = (rid, int(parts[5]))
                if key in self.outputs:
                    return httpx.Response(200, json=self.outputs[key])
                return httpx.Response(404, json={"detail": "no persisted output"})
            if parts[4] == "diff" and len(parts) == 5:
                return httpx.Response(self.diff_status, json=self.diff)
        return httpx.Response(404, json={"detail": f"unrouted {m} {p}"})


@pytest.fixture
def mock_api(monkeypatch):
    api = MockAPI()
    gw = cli.Gateway("http://mock", transport=httpx.MockTransport(api.handler), timeout=1.0)
    monkeypatch.setattr(cli, "_gateway", lambda *a, **k: gw)
    return api


def _run_out(rid: int, status: str = "done", passed: int = 2, total: int = 2, **kw) -> dict:
    return {
        "id": rid,
        "status": status,
        "dataset_id": kw.pop("dataset_id", 7),
        "model": "m",
        "passed_cases": passed,
        "total_cases": total,
        "avg_score": kw.pop("avg_score", 0.9),
        "threshold": kw.pop("threshold", 0.8),
        "tokens_in": None,
        "tokens_out": None,
        "latency_ms": None,
        "error": kw.pop("error", None),
    }


def _cases(outcomes: dict[int, bool]) -> list[dict]:
    return [
        {
            "seq": s,
            "score": 1.0 if o else 0.2,
            "passed": o,
            "error": None,
            "latency_ms": 10,
            "tokens_in": 5,
            "tokens_out": 3,
        }
        for s, o in outcomes.items()
    ]


def _invoke(args: list[str]):
    return CliRunner().invoke(cli.app, args, catch_exceptions=False)


# ---------- run ----------
def test_run_pass_exit0(mock_api):
    mock_api.runs[41] = _run_out(41, "done", 2, 2)
    mock_api.run_cases[41] = _cases({0: True, 1: True})
    mock_api.outputs[(41, 0)] = {
        "seq": 0,
        "output": "actual0",
        "score": {"score": 1.0, "detail": "ok", "raw": {"verdicts": []}},
        "error": None,
    }
    mock_api.outputs[(41, 1)] = {
        "seq": 1,
        "output": "actual1",
        "score": {"score": 0.9, "detail": "ok", "raw": {}},
        "error": None,
    }
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
            "--report",
            "rpt",
            "--format",
            "both",
        ]
    )
    assert r.exit_code == 0, r.output
    import pathlib

    assert pathlib.Path("rpt.json").exists() and pathlib.Path("rpt.md").exists()
    md = pathlib.Path("rpt.md").read_text()
    js = pathlib.Path("rpt.json").read_text()
    assert "actual0" in md and "actual0" in js
    assert "PASS" in md
    for p in ("rpt.json", "rpt.md"):
        pathlib.Path(p).unlink()


def test_run_fail_exit1(mock_api):
    mock_api.runs[41] = _run_out(41, "done", 1, 2, avg_score=0.55)
    mock_api.run_cases[41] = _cases({0: False, 1: True})
    mock_api.outputs[(41, 0)] = {
        "seq": 0,
        "output": "bad answer",
        "score": {"score": 0.2, "detail": "missing 30-day", "raw": {}},
        "error": None,
    }
    mock_api.outputs[(41, 1)] = {
        "seq": 1,
        "output": "fine",
        "score": {"score": 0.9, "detail": "ok", "raw": {}},
        "error": None,
    }
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 1, r.output
    assert "FAIL" in r.output


def test_run_server_side_failure_exit2(mock_api):
    mock_api.runs[41] = _run_out(41, "failed", 0, 2, error="endpoint 500")
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 2, r.output
    assert "endpoint 500" in r.output


def test_run_deadline_never_green(mock_api):
    mock_api.runs[41] = _run_out(41, "queued", 0, 2)
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "1.5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 2, r.output
    assert "deadline" in r.output.lower()


def test_run_auth_failure_exit2(mock_api):
    mock_api.submit_queue = [(401, {"detail": "invalid api key"}, {})]
    r = _invoke(
        [
            "run",
            "--key",
            "wrong",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 2, r.output
    assert "auth failed" in r.output


def test_run_429_then_ok(mock_api):
    mock_api.submit_queue = [
        (429, {"id": 41, "status": "queued", "dataset_id": 7}, {"Retry-After": "0"})
    ]
    mock_api.runs[41] = _run_out(41, "done", 2, 2)
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 0, r.output


def test_run_post_5xx_not_retried(mock_api):
    # 500 on a creation request: the run may already exist server-side —
    # auto-retry would duplicate the run and reserve quota twice.
    mock_api.fail_once["/v1/runs"] = 3
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 2, r.output
    flat = re.sub(r"\s+", " ", r.output)  # console wraps + indents long lines
    assert "not auto-retried" in flat
    assert mock_api.request_counts.get("POST /v1/runs") == 1


def test_run_post_transport_error_not_retried(mock_api):
    # Connection dropped mid-request: same duplicate-prevention policy.
    mock_api.raise_transport["/v1/runs"] = 3
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 2, r.output
    flat = re.sub(r"\s+", " ", r.output)  # console wraps + indents long lines
    assert "transport error" in flat
    assert mock_api.request_counts.get("POST /v1/runs") == 1


def test_poll_get_still_retries_5xx(mock_api):
    # Idempotent reads keep backoff-retry (unchanged behavior).
    mock_api.runs[41] = _run_out(41, "done", 2, 2)
    mock_api.fail_once["/v1/runs/41"] = 2
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
            "--deadline",
            "5",
            "--poll",
            "0.5",
        ]
    )
    assert r.exit_code == 0, r.output
    assert mock_api.request_counts.get("GET /v1/runs/41", 0) >= 3


def test_run_requires_dataset_or_id(mock_api):
    r = _invoke(
        [
            "run",
            "--key",
            "k",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
        ]
    )
    assert r.exit_code == 2
    assert "exactly one of" in r.output


def test_run_no_key_exit2(mock_api, monkeypatch):
    monkeypatch.delenv("EVALDIFF_API_KEY", raising=False)
    r = _invoke(
        [
            "run",
            "--dataset-id",
            "7",
            "--endpoint",
            "http://x/v1",
            "--model",
            "m",
        ]
    )
    assert r.exit_code == 2, r.output
    assert "no API key" in r.output


# ---------- diff ----------
def _diff_payload(regressions: list[dict]) -> dict:
    total = 2
    reg = len(regressions)
    return {
        "summary": {
            "total_cases": total,
            "regressions": reg,
            "improvements": 0,
            "a_avg": 0.9,
            "b_avg": 0.55 if reg else 0.95,
            "net_delta": -0.35 if reg else 0.05,
        },
        "cases": [
            {
                "seq": 0,
                "a_score": 1.0,
                "b_score": 0.2,
                "delta": -0.8,
                "regression": True,
                "note": "new failure",
            }
            if reg
            else {
                "seq": 0,
                "a_score": 1.0,
                "b_score": 1.0,
                "delta": 0.0,
                "regression": False,
                "note": None,
            },
            {
                "seq": 1,
                "a_score": 0.9,
                "b_score": 0.9,
                "delta": 0.0,
                "regression": False,
                "note": None,
            },
        ],
        "regressions": [
            {"seq": 0, "note": "new failure", "a_score": 1.0, "b_score": 0.2, "delta": -0.8}
        ]
        if reg
        else [],
    }


def test_diff_pass_exit0(mock_api):
    mock_api.runs[10] = _run_out(10, "done")
    mock_api.runs[12] = _run_out(12, "done", avg_score=0.95)
    mock_api.diff = _diff_payload([])
    r = _invoke(["diff", "--key", "k", "--base", "10", "--new", "12"])
    assert r.exit_code == 0, r.output
    assert "PASS" in r.output


def test_diff_regressions_exit1(mock_api):
    mock_api.runs[10] = _run_out(10, "done")
    mock_api.runs[12] = _run_out(12, "done", avg_score=0.55)
    mock_api.diff = _diff_payload([{"seq": 0}])
    r = _invoke(["diff", "--key", "k", "--base", "10", "--new", "12"])
    assert r.exit_code == 1, r.output
    assert "regression" in r.output.lower()


def test_diff_incomplete_run_never_green(mock_api):
    mock_api.runs[10] = _run_out(10, "done")
    mock_api.runs[12] = _run_out(12, "running", 0, 2)
    r = _invoke(["diff", "--key", "k", "--base", "10", "--new", "12"])
    assert r.exit_code == 2, r.output
    assert "not done" in r.output


def test_diff_invalid_comparison_exit2(mock_api):
    mock_api.runs[10] = _run_out(10, "done")
    mock_api.runs[12] = _run_out(12, "done")
    mock_api.diff_status = 409
    mock_api.diff = {"detail": "runs use different datasets"}
    r = _invoke(["diff", "--key", "k", "--base", "10", "--new", "12"])
    assert r.exit_code == 2, r.output
    assert "comparison rejected" in r.output


def test_diff_missing_run_exit2(mock_api):
    r = _invoke(["diff", "--key", "k", "--base", "10", "--new", 999])
    assert r.exit_code == 2, r.output


def test_diff_report_with_evidence(mock_api):
    mock_api.runs[10] = _run_out(10, "done")
    mock_api.runs[12] = _run_out(12, "done", avg_score=0.55)
    mock_api.diff = _diff_payload([{"seq": 0}])
    mock_api.outputs[(12, 0)] = {
        "seq": 0,
        "output": "new-bad",
        "score": {"score": 0.2, "detail": "missing 30-day", "raw": {}},
        "error": None,
    }
    mock_api.outputs[(10, 0)] = {
        "seq": 0,
        "output": "old-good",
        "score": {"score": 1.0, "detail": "ok", "raw": {}},
        "error": None,
    }
    import pathlib

    r = _invoke(
        [
            "diff",
            "--key",
            "k",
            "--base",
            "10",
            "--new",
            "12",
            "--report",
            "dreport",
            "--format",
            "both",
        ]
    )
    assert r.exit_code == 1, r.output
    md = pathlib.Path("dreport.md").read_text()
    js = pathlib.Path("dreport.json").read_text()
    assert "new-bad" in md and "old-good" in md
    assert "new-bad" in js
    for p in ("dreport.json", "dreport.md"):
        pathlib.Path(p).unlink()


# ---------- signup / misc ----------
def test_signup_prints_key(mock_api):
    r = _invoke(["signup", "--email", "a@b.se"])
    assert r.exit_code == 0, r.output
    assert "sk-test-1" in r.output


def test_signup_conflict_exit2(mock_api):
    mock_api.signup_status = 409
    mock_api.signup_body = {"detail": "account already exists"}
    r = _invoke(["signup", "--email", "a@b.se"])
    assert r.exit_code == 2, r.output
    assert "already exists" in r.output


def test_status_command(mock_api):
    mock_api.runs[41] = _run_out(41, "done", 1, 2)
    r = _invoke(["status", "--key", "k", "--run", "41"])
    assert r.exit_code == 0, r.output
    assert "status=done" in r.output
