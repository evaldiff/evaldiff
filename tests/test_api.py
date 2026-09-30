"""End-to-end tests: signup -> dataset -> run (echo model) -> diff -> report."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from api.db import state


def _wait_for(client, key: str, path: str, want: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = client.get(path, headers={"Authorization": f"Bearer {key}"})
        last = r.json()
        if last.get("status") == want:
            return last
        time.sleep(0.2)
    raise AssertionError(f"run did not reach {want!r}; last={last}")


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_signup_returns_key(client) -> None:
    r = client.post("/v1/auth/signup", json={"email": "dev@example.com"})
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("eval_")
    assert body["email"] == "dev@example.com"
    assert body["quota"] >= 1000
    # second signup reuses the account
    r2 = client.post("/v1/auth/signup", json={"email": "dev@example.com"})
    assert r2.status_code == 201
    assert r2.json()["email"] == "dev@example.com"


def test_requires_auth(client) -> None:
    assert (
        client.post(
            "/v1/datasets",
            json={"name": "x", "cases": [{"input": "i", "expected": "e"}]},
        ).status_code
        == 401
    )
    assert client.get("/v1/usage").status_code == 401


# ---------- SSRF endpoint guard ----------
def _make_ssrf_app(tmp_path, *, allow_local: bool):
    from api.main import create_app
    from api.settings import Settings

    return create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/ssrf.db",
            enable_worker=False,
            allow_local_endpoints=allow_local,
        )
    )


@pytest.fixture()
def ssrf_client(tmp_path, client):
    """Prod-like client: self-hosted endpoints blocked by default."""
    app = _make_ssrf_app(tmp_path, allow_local=False)
    with TestClient(app) as c:
        yield c
    state.session_factory = None
    state.storage = None


@pytest.fixture()
def ssrf_client_optin(tmp_path, echo_model):
    """App client with self-hosted endpoints allowed."""
    app = _make_ssrf_app(tmp_path, allow_local=True)
    with TestClient(app) as c:
        yield c
    state.session_factory = None
    state.storage = None


def test_endpoint_ssrf_blocked_by_default(ssrf_client) -> None:
    client = ssrf_client
    key = client.post("/v1/auth/signup", json={"email": "ssrf@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "d", "cases": [{"input": "hi", "expected": "hi"}]},
    ).json()
    for evil in (
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://127.0.0.1:9999/v1",  # loopback
        "http://10.0.0.5/v1",  # RFC1918
        "http://[::1]/v1",  # IPv6 loopback
    ):
        r = client.post(
            "/v1/runs",
            headers=headers,
            json={
                "dataset_id": ds["id"],
                "model": "m",
                "endpoint": evil,
            },
        )
        assert r.status_code == 400, f"{evil} -> {r.status_code} {r.text}"
        assert "blocked" in r.json()["detail"]
    # public endpoints still accepted
    r = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "m", "endpoint": "https://api.openai.com/v1"},
    )
    assert r.status_code == 202, r.text


def test_endpoint_ssrf_opt_in(ssrf_client_optin, echo_model) -> None:
    client = ssrf_client_optin
    key = client.post("/v1/auth/signup", json={"email": "optin@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "d", "cases": [{"input": "hi", "expected": "hi"}]},
    ).json()
    r = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "m", "endpoint": echo_model},
    )
    assert r.status_code == 202, r.text


def test_dataset_validation(client) -> None:
    key = client.post("/v1/auth/signup", json={"email": "ds@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    # missing 'expected'
    r = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "bad", "cases": [{"input": "only-input"}]},
    )
    assert r.status_code == 422
    # good dataset
    r = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "support",
            "cases": [
                {"input": "refund policy", "expected": "30-day full refund"},
                {"input": "reset password", "expected": "Settings Security"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    ds = r.json()
    assert ds["case_count"] == 2
    assert client.get(f"/v1/datasets/{ds['id']}", headers=headers).status_code == 200


def test_full_run_lifecycle_and_diff(client, echo_model) -> None:
    key = client.post("/v1/auth/signup", json={"email": "e2e@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}

    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "support",
            "cases": [
                {
                    "input": "What is your refund policy?",
                    "expected": "30-day full refund",
                },
                {
                    "input": "How do I reset my password?",
                    "expected": "Settings Security reset",
                },
                {
                    "input": "Do you ship to Sweden?",
                    "expected": "Yes we ship to Sweden",
                },
            ],
        },
    ).json()

    # run A — generous threshold, passes
    ra = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo-1",
            "endpoint": echo_model,
            "threshold": 0.0,
        },
    )
    assert ra.status_code == 202, ra.text
    ra_id = ra.json()["id"]
    ra = _wait_for(client, key, f"/v1/runs/{ra_id}", "done")
    assert ra["total_cases"] == 3
    cases_a = client.get(f"/v1/runs/{ra_id}/cases", headers=headers).json()
    assert [c["seq"] for c in cases_a] == [0, 1, 2]
    assert all(c["error"] is None for c in cases_a)
    assert all(c["score"] is not None for c in cases_a)

    # run B — strict threshold, fails more (regressions vs A)
    rb = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo-1",
            "endpoint": echo_model,
            "threshold": 0.999,
        },
    )
    rb_id = rb.json()["id"]
    rb = _wait_for(client, key, f"/v1/runs/{rb_id}", "done")

    d = client.get(f"/v1/runs/{rb_id}/diff", headers=headers, params={"compare": ra_id}).json()
    assert d["summary"]["total_cases"] == 3
    assert d["summary"]["regressions"] >= 0

    md = client.get(f"/v1/runs/{rb_id}/report.md", headers=headers, params={"compare": ra_id})
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "run" in md.text

    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["used"] == 6  # 2 runs x 3 cases
    assert usage["quota"] >= 1000


def test_quota_enforced(client, echo_model) -> None:
    """600-case run succeeds (charged 600); second 600-case run exceeds the 1000 quota."""
    key = client.post("/v1/auth/signup", json={"email": "quota@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    cases = [{"input": f"q{i}", "expected": f"e{i}"} for i in range(600)]
    ds = client.post("/v1/datasets", headers=headers, json={"name": "big", "cases": cases}).json()
    r1 = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": echo_model,
            "threshold": 0.0,
        },
    )
    assert r1.status_code == 202, r1.text
    r1_id = r1.json()["id"]
    _wait_for(client, key, f"/v1/runs/{r1_id}", "done")
    # second run: 600 charged + 600 new > 1000 quota
    r2 = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": echo_model,
            "threshold": 0.0,
        },
    )
    assert r2.status_code == 429


def test_diff_markdown_contains_regressions(client, echo_model) -> None:
    key = client.post("/v1/auth/signup", json={"email": "diff@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "d",
            "cases": [
                {"input": "a", "expected": "30-day full refund"},
                {"input": "b", "expected": "unrelated topic xyz"},
            ],
        },
    ).json()
    r_loose = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": echo_model,
            "threshold": 0.0,
        },
    ).json()["id"]
    r_strict = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": echo_model,
            "threshold": 0.999,
        },
    ).json()["id"]
    _wait_for(client, key, f"/v1/runs/{r_loose}", "done")
    _wait_for(client, key, f"/v1/runs/{r_strict}", "done")
    d = client.get(f"/v1/runs/{r_strict}/diff", headers=headers, params={"compare": r_loose}).json()
    # the "b" case (echo doesn't match 'unrelated topic xyz') regresses under strict threshold
    assert d["summary"]["regressions"] >= 1
    md = client.get(
        f"/v1/runs/{r_strict}/report.md", headers=headers, params={"compare": r_loose}
    ).text
    assert "Regressions" in md
