"""Regression coverage for upgrades, atomic enqueue, and leased retries.

Set EVALDIFF_TEST_POSTGRES_URL to a disposable PostgreSQL database to run
these same tests against PostgreSQL as well as SQLite.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from api.db import Base, make_engine, state
from api.leases import JobLease, LeaseLost, fence
from api.models import Account, Dataset, Job, Run, RunCase
from api.quota import (
    RUN_RESERVATIONS,
    ledger_metadata,
    migrate_legacy_usage,
    now_period,
    reserve,
    settle_run,
    used_cases_for,
)
from api.runner import execute_run
from api.storage import InMemoryStorage
from api.worker import enqueue_run, poll_once, recover_abandoned_jobs

DATABASES = ["sqlite"] + (["postgresql"] if os.environ.get("EVALDIFF_TEST_POSTGRES_URL") else [])


@pytest.fixture(params=DATABASES)
def sessions(request, tmp_path, monkeypatch):
    admin = None
    schema = "review_" + uuid4().hex
    if request.param == "postgresql":
        url = make_url(os.environ["EVALDIFF_TEST_POSTGRES_URL"])
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = make_engine(
            str(
                url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
                    hide_password=False
                )
            )
        )
    else:
        engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    ledger_metadata().create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(state, "session_factory", factory)
    monkeypatch.setattr(state, "storage", InMemoryStorage())
    monkeypatch.setattr(state, "settings", None)
    yield factory
    engine.dispose()
    if admin:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed(session, *, count=2, used=0):
    account = Account(
        email=uuid4().hex + "@example.com", used_cases=used, quota_period=now_period()
    )
    dataset = Dataset(
        account=account,
        name="d",
        case_count=count,
        cases_json=json.dumps([{"input": "x", "expected": "x"}] * count),
    )
    run = Run(
        account=account,
        dataset=dataset,
        model="m",
        endpoint="https://example.com",
        total_cases=count,
        status="queued",
    )
    session.add(run)
    session.flush()
    return account, run


def queue(session, *, count=2, period=None):
    account, run = seed(session, count=count)
    assert reserve(
        session,
        run_id=run.id,
        account_id=account.id,
        case_count=count,
        period=period or now_period(),
        quota=1000,
    )
    enqueue_run(session, run.id)
    return account.id, run.id


def expire(session, job_id):
    session.execute(
        update(JobLease)
        .where(JobLease.job_id == job_id)
        .values(expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1))
    )
    session.commit()


def test_upgrade_preserves_counter_and_pending_reservations(sessions):
    with sessions() as session:
        account, pending = seed(session, count=200, used=900)
        aid, rid = account.id, pending.id
        session.commit()
        migrate_legacy_usage(session)
        assert used_cases_for(aid, now_period(), session) == 900
        assert settle_run(session, run_id=rid, charged=150, success=True)
        session.commit()
        migrate_legacy_usage(session)
        assert used_cases_for(aid, now_period(), session) == 850
        assert len(session.execute(select(RUN_RESERVATIONS)).all()) == 2


def test_upgrade_preserves_existing_ledger(sessions):
    with sessions() as session:
        account, legacy = seed(session, count=100, used=900)
        legacy.status = "done"
        session.add(RunCase(run_id=legacy.id, seq=0, score=1, passed=True))
        other = Run(
            account=account,
            dataset=legacy.dataset,
            model="m",
            endpoint="http://example.com",
            total_cases=100,
        )
        session.add(other)
        session.flush()
        aid = account.id
        assert reserve(
            session,
            run_id=other.id,
            account_id=aid,
            case_count=100,
            period=now_period(),
            quota=1000,
        )
        session.commit()
        migrate_legacy_usage(session)
        assert used_cases_for(aid, now_period(), session) == 1000
        migrate_legacy_usage(session)
        assert used_cases_for(aid, now_period(), session) == 1000


def test_enqueue_failure_rolls_back_run_and_reservation(sessions, monkeypatch):
    from starlette.testclient import TestClient

    from api.main import create_app
    from api.settings import Settings

    app = create_app(
        Settings(
            database_url=sessions.kw["bind"].url.render_as_string(hide_password=False),
            enable_worker=False,
            rate_limit_rpm=0,
            signup_rate_per_min=0,
        )
    )

    def fail_enqueue(session, run_id):
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr("api.main.enqueue_run", fail_enqueue)
    with TestClient(app, raise_server_exceptions=False) as client:
        key = client.post("/v1/auth/signup", json={"email": "atomic@example.com"}).json()["key"]
        headers = {"Authorization": f"Bearer {key}"}
        ds = client.post(
            "/v1/datasets",
            headers=headers,
            json={"name": "d", "cases": [{"input": "x", "expected": "x"}]},
        ).json()
        response = client.post(
            "/v1/runs",
            headers=headers,
            json={"dataset_id": ds["id"], "model": "m", "endpoint": "https://example.com"},
        )
        assert response.status_code == 500
        assert client.get("/v1/usage", headers=headers).json()["used"] == 0
        with sessions() as session:
            assert session.query(Run).count() == 0
            assert session.query(Job).count() == 0
            assert session.execute(select(RUN_RESERVATIONS)).all() == []


def test_reservation_and_month_settlement(sessions):
    with sessions() as session:
        aid, rid = queue(session, count=600, period="2025-12")
        newer = Run(
            account_id=aid,
            dataset_id=session.get(Run, rid).dataset_id,
            model="m",
            endpoint="https://example.com",
        )
        session.add(newer)
        session.flush()
        assert reserve(
            session,
            run_id=newer.id,
            account_id=aid,
            case_count=300,
            period=now_period(),
            quota=1000,
        )
        session.commit()
        assert settle_run(session, run_id=rid, charged=200, success=True)
        session.commit()
        assert not settle_run(session, run_id=rid, charged=0, success=False)
        assert used_cases_for(aid, now_period(), session) == 300
        assert used_cases_for(aid, "2025-12", session) == 200


async def test_live_lease_is_not_reclaimed_and_retry_is_charged(sessions, monkeypatch):
    with sessions() as session:
        aid, rid = queue(session)
        jid = await poll_once(session)
        token = session.get(Job, jid).claimed_by
        session.add(RunCase(run_id=rid, seq=0, score=1, passed=True))
        session.commit()
        assert recover_abandoned_jobs(session) == 0
        assert session.query(RunCase).count() == 1
        expire(session, jid)
        assert recover_abandoned_jobs(session) == 1
        assert recover_abandoned_jobs(session) == 0
        assert session.query(RunCase).count() == 0
        assert used_cases_for(aid, now_period(), session) == 2
        assert await poll_once(session) == jid
        with pytest.raises(LeaseLost):
            fence(session, jid, token)
        session.rollback()
        token = session.get(Job, jid).claimed_by

    async def model(*args, **kwargs):
        return "x", 1, 1

    monkeypatch.setattr("api.runner.call_model", model)
    await execute_run(rid, job_id=jid, lease_token=token)
    with sessions() as session:
        assert session.get(Run, rid).status == "done"
        assert session.query(RunCase).count() == 2
        row = session.execute(select(RUN_RESERVATIONS)).one()
        assert row.settled and row.charged == 2
        assert used_cases_for(aid, now_period(), session) == 2


async def test_exhausted_attempt_refunds_only_its_reservation(sessions):
    with sessions() as session:
        aid, rid = queue(session)
        jid = await poll_once(session)
        expire(session, jid)
        assert recover_abandoned_jobs(session) == 1
        assert await poll_once(session) == jid
        expire(session, jid)
        assert recover_abandoned_jobs(session) == 0
        assert session.get(Run, rid).status == "failed"
        assert session.get(Job, jid).status == "failed"
        assert used_cases_for(aid, now_period(), session) == 0


async def test_stale_attempt_cannot_write_results(sessions, monkeypatch):
    with sessions() as session:
        aid, rid = queue(session)
        jid = await poll_once(session)
        token = session.get(Job, jid).claimed_by

    async def model(*args, **kwargs):
        with sessions() as other:
            expire(other, jid)
            assert recover_abandoned_jobs(other) == 1
            assert await poll_once(other) == jid
        return "x", 1, 1

    monkeypatch.setattr("api.runner.call_model", model)
    await execute_run(rid, job_id=jid, lease_token=token)
    with sessions() as session:
        assert session.query(RunCase).count() == 0
        assert session.get(Run, rid).status == "queued"
        assert used_cases_for(aid, now_period(), session) == 2


def test_concurrent_reservations_and_claims(sessions):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with sessions() as session:
        account, run = seed(session)
        aid, did = account.id, run.dataset_id
        session.delete(run)
        session.commit()
    barrier = Barrier(2)

    def submit():
        with sessions() as session:
            barrier.wait(timeout=10)
            run = Run(
                account_id=aid,
                dataset_id=did,
                model="m",
                endpoint="http://example.com",
                total_cases=600,
            )
            session.add(run)
            session.flush()
            accepted = reserve(
                session,
                run_id=run.id,
                account_id=aid,
                case_count=600,
                period=now_period(),
                quota=1000,
            )
            if accepted:
                enqueue_run(session, run.id)
            else:
                session.rollback()
            return accepted

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: submit(), range(2))) == [False, True]

    def claim():
        with sessions() as session:
            barrier.wait(timeout=10)
            return asyncio.run(poll_once(session))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: claim(), range(2)))
    assert sum(bool(jid) for jid in claimed) == 1
    with sessions() as session:
        assert used_cases_for(aid, now_period(), session) == 600


async def test_heartbeat_renews_during_model_io(sessions, monkeypatch):
    from api.worker import _heartbeat

    with sessions() as session:
        _, _ = queue(session)
        jid = await poll_once(session)
        token = session.get(Job, jid).claimed_by
        near_expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=5)
        session.execute(
            update(JobLease).where(JobLease.job_id == jid).values(expires_at=near_expiry)
        )
        session.commit()
    monkeypatch.setattr("api.worker.HEARTBEAT_SECONDS", 0.01)
    renewed = asyncio.Event()
    original_fence = fence

    def observe(session, job_id, token):
        original_fence(session, job_id, token)
        renewed.set()

    monkeypatch.setattr("api.worker.fence", observe)
    attempt = asyncio.create_task(asyncio.Event().wait())
    heartbeat = asyncio.create_task(_heartbeat(jid, token, attempt))
    try:
        await asyncio.wait_for(renewed.wait(), timeout=2)
        with sessions() as session:
            assert session.get(JobLease, jid).expires_at > near_expiry
            assert recover_abandoned_jobs(session) == 0
        assert not attempt.done()
    finally:
        heartbeat.cancel()
        attempt.cancel()
        await asyncio.gather(heartbeat, attempt, return_exceptions=True)


async def test_cancelled_attempt_can_be_recovered(sessions, monkeypatch):
    with sessions() as session:
        aid, rid = queue(session)
        jid = await poll_once(session)
        token = session.get(Job, jid).claimed_by
    started = asyncio.Event()

    async def model(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("api.runner.call_model", model)
    attempt = asyncio.create_task(execute_run(rid, job_id=jid, lease_token=token))
    await asyncio.wait_for(started.wait(), timeout=2)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    with sessions() as session:
        expire(session, jid)
        assert recover_abandoned_jobs(session) == 1
        assert session.get(Run, rid).status == "queued"
        assert used_cases_for(aid, now_period(), session) == 2


def test_concurrent_recovery_has_one_winner(sessions):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with sessions() as session:
        aid, _rid = queue(session)
        jid = asyncio.run(poll_once(session))
        expire(session, jid)
    barrier = Barrier(2)

    def recover():
        with sessions() as session:
            barrier.wait(timeout=10)
            return recover_abandoned_jobs(session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: recover(), range(2))) == [0, 1]
    with sessions() as session:
        assert session.get(Job, jid).status == "pending"
        assert used_cases_for(aid, now_period(), session) == 2
