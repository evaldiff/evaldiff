"""End-to-end tests: signup -> dataset -> run (echo model) -> diff -> report."""

from __future__ import annotations

import time

import pytest
from sqlalchemy.orm import sessionmaker
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


def test_signup_duplicate_email_rejected(client) -> None:
    """P1: signup must never issue a key for an existing account (takeover)."""
    assert client.post("/v1/auth/signup", json={"email": "dup@example.com"}).status_code == 201
    r = client.post("/v1/auth/signup", json={"email": "dup@example.com"})
    assert r.status_code == 409
    assert "key" not in r.json()


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
            rate_limit_rpm=0,
            signup_rate_per_min=0,
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
    """Server-side allow (deployment admin) still works."""
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


def test_endpoint_ssrf_caller_cannot_override(ssrf_client, echo_model) -> None:
    """P1: a caller cannot override the SSRF policy per-request, even by
    sending allow_local_endpoints in the body (server env var is the only
    switch)."""
    client = ssrf_client
    key = client.post("/v1/auth/signup", json={"email": "override@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "d", "cases": [{"input": "hi", "expected": "hi"}]},
    ).json()
    r = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "m",
            "endpoint": echo_model,  # 127.0.0.1 — blocked on this deployment
            "allow_local_endpoints": True,  # caller attempt to override
        },
    )
    assert r.status_code == 400, f"override succeeded: {r.status_code} {r.text}"


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


# ---------- P1/P2: errored cases, failed runs, quota reservation ----------
def test_diff_treats_erred_b_as_regression(client, echo_model) -> None:
    """P1: a case that passed in A but ERRORED (or is missing) in B is a
    regression, not invisible."""
    key = client.post("/v1/auth/signup", json={"email": "errdiff@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "d",
            "cases": [
                {"input": "What is 2+2?", "expected": "4"},
                {"input": "capital of France?", "expected": "France"},
            ],
        },
    ).json()
    # run A: threshold 0 — echo repeats the prompt (which contains the
    # question, not the answer), so force pass with threshold 0 (any score).
    ra = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "echo", "endpoint": echo_model, "threshold": 0.0},
    ).json()["id"]
    ra_id = _wait_for(client, key, f"/v1/runs/{ra}", "done")["id"]
    # both cases passed in A (threshold 0, echo always scores >= 0)
    cases_a = client.get(f"/v1/runs/{ra_id}/cases", headers=headers).json()
    assert all(c["passed"] is True for c in cases_a), cases_a

    # run B: endpoint refuses the connection -> every case errors (passed=None)
    rb = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": "http://127.0.0.1:1/v1",  # connection refused
            "threshold": 0.0,
        },
    )
    assert rb.status_code == 202, rb.text
    rb_id = rb.json()["id"]
    rb = _wait_for(client, key, f"/v1/runs/{rb_id}", "done")
    cases_b = client.get(f"/v1/runs/{rb_id}/cases", headers=headers).json()
    assert all(c["error"] for c in cases_b), cases_b  # both errored

    d = client.get(f"/v1/runs/{rb_id}/diff", headers=headers, params={"compare": ra_id}).json()
    assert d["summary"]["regressions"] == 2, d["summary"]
    notes = {c["note"] for c in d["regressions"]}
    assert any("errored" in n for n in notes), notes


def test_failed_run_persists_status_and_refunds(client, echo_model) -> None:
    """P2: a run that fails (dataset unreadable) must persist status='failed'
    with an error, and release its quota reservation (no charge)."""
    key = client.post("/v1/auth/signup", json={"email": "failrun@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={"name": "d", "cases": [{"input": "i", "expected": "e"}]},
    ).json()

    # corrupt the stored payload so the worker cannot load it
    from api.db import state as _state

    sf = _state.session_factory()
    from api.models import Dataset as _DS

    row = sf.get(_DS, ds["id"])
    row.cases_json = '"not-a-list"'
    sf.commit()
    sf.close()

    r = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "echo", "endpoint": echo_model, "threshold": 0.0},
    )
    assert r.status_code == 202, r.text
    rid = r.json()["id"]

    last = _wait_for(client, key, f"/v1/runs/{rid}", "failed")
    assert last["error"], last  # P2: error is persisted, not lost
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["used"] == 0, usage  # failed run is free (reservation released)


def test_quota_reserved_at_enqueue(client, echo_model) -> None:
    """P2: quota is reserved when the run is QUEUED, not when it finishes —
    two 600-case runs cannot both pass the check against a 1000-case quota."""
    key = client.post("/v1/auth/signup", json={"email": "reserve@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    cases = [{"input": f"q{i}", "expected": f"e{i}"} for i in range(600)]
    ds = client.post("/v1/datasets", headers=headers, json={"name": "big", "cases": cases}).json()
    r1 = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "echo", "endpoint": echo_model, "threshold": 0.0},
    )
    assert r1.status_code == 202, r1.text
    # immediately (run 1 may still be queued/running): the reservation blocks
    # run 2 even though nothing has been settled yet
    r2 = client.post(
        "/v1/runs",
        headers=headers,
        json={"dataset_id": ds["id"], "model": "echo", "endpoint": echo_model, "threshold": 0.0},
    )
    assert r2.status_code == 429, f"reservation not enforced: {r2.status_code} {r2.text}"
    r1 = _wait_for(client, key, f"/v1/runs/{r1.json()['id']}", "done")
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["used"] == 600, usage  # exactly the one run that succeeded


def test_erred_cases_not_charged(client) -> None:
    """P2: cases where the model call failed are not billed, even though the
    run itself reaches 'done'."""
    key = client.post("/v1/auth/signup", json={"email": "nocharge@example.com"}).json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    ds = client.post(
        "/v1/datasets",
        headers=headers,
        json={
            "name": "d",
            "cases": [
                {"input": "a", "expected": "a"},
                {"input": "b", "expected": "b"},
            ],
        },
    ).json()
    r = client.post(
        "/v1/runs",
        headers=headers,
        json={
            "dataset_id": ds["id"],
            "model": "echo",
            "endpoint": "http://127.0.0.1:1/v1",  # every model call fails
            "threshold": 0.0,
        },
    )
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    r = _wait_for(client, key, f"/v1/runs/{rid}", "done")
    cases = client.get(f"/v1/runs/{rid}/cases", headers=headers).json()
    assert all(c["error"] for c in cases), cases
    usage = client.get("/v1/usage", headers=headers).json()
    assert usage["used"] == 0, usage  # 0 OK cases -> 0 charged


@pytest.mark.parametrize("case_count, expected_statuses", [(600, [202, 429]), (400, [202, 202])])
def test_concurrent_runs_cannot_overbook_quota(tmp_path, case_count, expected_statuses) -> None:
    """Concurrent reservations respect the limit without losing valid usage."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi import Depends, Header

    from api.auth import get_current_account
    from api.db import get_session
    from api.main import create_app
    from api.models import Job
    from api.settings import Settings

    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/concurrent-quota.db",
            default_quota=1000,
            enable_worker=False,
            rate_limit_rpm=0,
            signup_rate_per_min=0,
        )
    )
    both_authenticated = Barrier(2, timeout=10)

    def synchronized_account(
        authorization: str | None = Header(default=None),
        session=Depends(get_session),
    ):
        account = get_current_account(authorization=authorization, session=session)
        # Each request has its own real session and has read the same account
        # before either can reserve quota. No sleeps or mocked quota writes.
        both_authenticated.wait()
        return account

    try:
        with TestClient(app) as client:
            signup = client.post("/v1/auth/signup", json={"email": "race@example.com"})
            assert signup.status_code == 201, signup.text
            headers = {"Authorization": f"Bearer {signup.json()['key']}"}
            dataset = client.post(
                "/v1/datasets",
                headers=headers,
                json={"name": "big", "cases": [{"input": "i", "expected": "e"}] * case_count},
            )
            assert dataset.status_code == 201, dataset.text
            payload = {
                "dataset_id": dataset.json()["id"],
                "model": "unused",
                "endpoint": "https://example.com/v1",
            }
            app.dependency_overrides[get_current_account] = synchronized_account
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    requests = [
                        pool.submit(client.post, "/v1/runs", headers=headers, json=payload)
                        for _ in range(2)
                    ]
                    responses = [request.result(timeout=20) for request in requests]
            finally:
                app.dependency_overrides.clear()

            assert sorted(r.status_code for r in responses) == expected_statuses, [
                (r.status_code, r.json()) for r in responses
            ]
            runs = client.get("/v1/runs", headers=headers).json()
            accepted = expected_statuses.count(202)
            assert len(runs) == accepted
            assert all(run["status"] == "queued" for run in runs)
            usage = client.get("/v1/usage", headers=headers).json()
            assert usage["used"] == accepted * case_count, usage
            assert usage["remaining"] == 1000 - accepted * case_count, usage
            with state.session_factory() as session:
                assert session.query(Job).count() == accepted
    finally:
        state.session_factory = None
        state.storage = None


@pytest.mark.parametrize("success, charged", [(True, 200), (False, 0)])
def test_settlement_preserves_new_reservations(tmp_path, success, charged) -> None:
    """A refund must not overwrite reservations made after the account was read."""
    from api.db import Base, make_engine
    from api.models import Account, Dataset, Run
    from api.quota import ledger_metadata, reserve, settle_run, used_cases_for

    engine = make_engine(f"sqlite:///{tmp_path}/settlement.db")
    Base.metadata.create_all(engine)
    ledger_metadata().create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        with sessions() as session:
            account = Account(
                email="settlement@example.com",
                monthly_quota=1000,
            )
            dataset = Dataset(account=account, name="d", case_count=600, cases_json="[]")
            run = Run(
                account=account, dataset=dataset, model="unused", endpoint="https://example.com"
            )
            session.add(run)
            session.commit()
            run_id = run.id
            account_id = account.id
            dataset_id = dataset.id
            # Old run's reservation (600 cases) made first...
            assert reserve(
                session,
                run_id=run_id,
                account_id=account_id,
                case_count=600,
                period=time.strftime("%Y-%m"),
                quota=1000,
            )
            session.commit()
        # ...and a NEW reservation (300) made while the worker "read" usage.
        with sessions() as other:
            # Re-fetch account/dataset so this session owns them
            acct2 = other.query(Account).filter_by(id=account_id).one()
            ds2 = other.query(Dataset).filter_by(id=dataset_id).one()
            other_run = Run(
                account=acct2, dataset=ds2, model="unused", endpoint="https://example.com"
            )
            other.add(other_run)
            other.commit()
            assert reserve(
                other,
                run_id=other_run.id,
                account_id=account_id,
                case_count=300,
                period=time.strftime("%Y-%m"),
                quota=1000,
            )
            other.commit()
        # Settlement of the old run (success or refund) must not touch
        # the new reservation.
        with sessions() as worker:
            settle_run(worker, run_id=run_id, charged=charged, success=success)
            used = used_cases_for(account_id, time.strftime("%Y-%m"), worker)
            assert used == 300 + charged, used
    finally:
        engine.dispose()
