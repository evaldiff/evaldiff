"""Section C (part 1): run-wide deadlines and worker concurrency.

Set EVALDIFF_TEST_POSTGRES_URL to a disposable PostgreSQL database to run
these same tests against PostgreSQL as well as SQLite.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from api.db import Base, make_engine, state
from api.leases import JobLease
from api.models import Account, Dataset, Job, Run, RunCase
from api.quota import ledger_metadata, now_period, reserve, reserve_deadline, used_cases_for
from api.runner import execute_run
from api.settings import Settings
from api.storage import InMemoryStorage
from api.worker import enqueue_run, poll_once, recover_abandoned_jobs, worker_loop

DATABASES = ["sqlite"] + (["postgresql"] if os.environ.get("EVALDIFF_TEST_POSTGRES_URL") else [])


@pytest.fixture(params=DATABASES)
def sessions(request, tmp_path, monkeypatch):
    admin = None
    schema = "c1_" + uuid.uuid4().hex
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


def seed(sessions, *, count=2, deadline: int | None = None):
    """Account + dataset + queued run + quota reservation (+optional deadline)."""
    with sessions() as session:
        account = Account(email=uuid.uuid4().hex + "@example.com", quota_period=now_period())
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
        reserve(
            session,
            run_id=run.id,
            account_id=account.id,
            case_count=count,
            period=now_period(),
            quota=10_000,
        )
        if deadline is not None:
            reserve_deadline(session, run.id, deadline)
        enqueue_run(session, run.id)
        return account.id, run.id


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def claim(sessions, rid):
    """Claim the run's job; return (job_id, token)."""
    with sessions() as session:
        jid = asyncio.run(poll_once(session))
        assert jid
        token = session.get(Job, jid).claimed_by
    return jid, token


def fake_model(sessions, *, block_seconds: float = 0.0):
    async def model(*args, **kwargs):
        if block_seconds:
            await asyncio.sleep(block_seconds)
        return "x", 1, 1

    return model


def test_expired_deadline_fails_immediately_and_refunds(sessions, monkeypatch):
    aid, rid = seed(sessions, count=2, deadline=1)
    time.sleep(1.2)  # burn the whole budget
    jid, token = claim(sessions, rid)
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))
    asyncio.run(execute_run(rid, job_id=jid, lease_token=token))
    with sessions() as session:
        run = session.get(Run, rid)
        assert run.status == "failed"
        assert "deadline" in (run.error or "").lower()
        assert session.query(RunCase).count() == 0  # no case produced
        assert used_cases_for(aid, now_period(), session) == 0  # full refund


def test_deadline_is_absolute_across_reclaims(sessions, monkeypatch):
    """A reclaimed attempt inherits the REMAINING budget, not a fresh one."""
    aid, rid = seed(sessions, count=2, deadline=4)
    jid, _token = claim(sessions, rid)
    with sessions() as session:
        # Simulate attempt 1 burning the whole budget (clock past deadline).
        session.execute(
            update(Run).where(Run.id == rid).values(deadline_at=now_utc() - timedelta(seconds=1))
        )
        session.commit()
        # Force lease expiry so recovery re-queues the job.
        session.execute(
            update(JobLease)
            .where(JobLease.job_id == jid)
            .values(expires_at=now_utc() - timedelta(seconds=1))
        )
        session.commit()
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))
    with sessions() as session:
        assert recover_abandoned_jobs(session) == 1
        assert session.get(Run, rid).status == "queued"
    jid2, token2 = claim(sessions, rid)
    asyncio.run(execute_run(rid, job_id=jid2, lease_token=token2))
    with sessions() as session:
        run = session.get(Run, rid)
        # Budget must NOT have been reset to a fresh 4s by the reclaim:
        # the attempt sees an already-expired deadline and fails, rather
        # than burning another full budget.
        assert run.deadline_at <= now_utc()
        assert run.status == "failed"
        assert "deadline" in (run.error or "").lower()
        # No case rows were produced by either attempt.
        assert session.query(RunCase).count() == 0
        # The reservation is refunded exactly once despite two attempts.
        assert used_cases_for(aid, now_period(), session) == 0


def test_concurrent_workers_complete_multiple_runs(sessions, monkeypatch):
    """Two slow jobs overlap under the default concurrency (>= 2)."""
    _, rid_a = seed(sessions, count=1)
    _, rid_b = seed(sessions, count=1)
    started: list[float] = []

    async def model(client, *, endpoint, model, api_key, prompt, timeout=120.0):
        started.append(time.monotonic())
        await asyncio.sleep(0.6)
        return "x", 1, 1

    monkeypatch.setattr("api.runner.call_model", model)

    async def drive():
        stop = asyncio.Event()
        loop_task = asyncio.create_task(worker_loop(stop, interval=0.05))
        deadline = time.monotonic() + 10
        with sessions() as session:
            while (
                session.get(Run, rid_a).status not in ("done", "failed")
                or session.get(Run, rid_b).status not in ("done", "failed")
            ) and time.monotonic() < deadline:
                session.rollback()
                await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=5)
        with sessions() as session:
            assert session.get(Run, rid_a).status == "done"
            assert session.get(Run, rid_b).status == "done"

    asyncio.run(drive())
    assert len(started) == 2
    # Concurrent: both jobs started before the first call's 0.6s sleep ended;
    # sequential starts would be >= 0.6s apart.
    assert started[1] - started[0] < 0.5, (
        f"jobs did not overlap (gap={started[1] - started[0]:.3f}s)"
    )


def test_concurrency_limit_respected(sessions, monkeypatch):
    """With concurrency 1, the second job must not start before the first ends."""
    started: list[float] = []

    async def model(client, *, endpoint, model, api_key, prompt, timeout=120.0):
        started.append(time.monotonic())
        await asyncio.sleep(0.5)
        return "x", 1, 1

    monkeypatch.setattr("api.runner.call_model", model)
    monkeypatch.setattr(state, "settings", Settings(worker_concurrency=1))

    _, rid_a = seed(sessions, count=1)
    _, rid_b = seed(sessions, count=1)

    async def drive():
        stop = asyncio.Event()
        loop_task = asyncio.create_task(worker_loop(stop, interval=0.05))
        deadline = time.monotonic() + 15
        with sessions() as session:
            while (
                session.get(Run, rid_a).status not in ("done", "failed")
                or session.get(Run, rid_b).status not in ("done", "failed")
            ) and time.monotonic() < deadline:
                session.rollback()
                await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=5)
        with sessions() as session:
            assert session.get(Run, rid_a).status == "done"
            assert session.get(Run, rid_b).status == "done"

    asyncio.run(drive())
    assert len(started) == 2
    gap = started[1] - started[0]
    # Sequential: the second start is >= the first call's sleep (0.5s).
    assert gap >= 0.4, f"jobs overlapped despite concurrency=1 (gap={gap:.3f}s)"


def test_per_account_queued_counts(sessions):
    """The API's admission counts: a full account is at its limit, a fresh
    account is below it (the 429 decision is exactly these two counts)."""
    limit = Settings().max_queued_runs_per_account
    with sessions() as session:
        full = Account(email=uuid.uuid4().hex + "@example.com", quota_period=now_period())
        session.add(full)
        session.flush()
        d = Dataset(
            account=full,
            name="d",
            case_count=1,
            cases_json=json.dumps([{"input": "x", "expected": "x"}]),
        )
        for _ in range(limit):
            run = Run(
                account=full,
                dataset=d,
                model="m",
                endpoint="https://example.com",
                total_cases=1,
                status="queued",
            )
            session.add(run)
            session.flush()
            reserve(
                session,
                run_id=run.id,
                account_id=full.id,
                case_count=1,
                period=now_period(),
                quota=10_000,
            )
            enqueue_run(session, run.id)

        fresh = Account(email=uuid.uuid4().hex + "@example.com", quota_period=now_period())
        session.add(fresh)
        session.flush()
        od = Dataset(
            account=fresh,
            name="od",
            case_count=1,
            cases_json=json.dumps([{"input": "x", "expected": "x"}]),
        )
        orun = Run(
            account=fresh,
            dataset=od,
            model="m",
            endpoint="https://example.com",
            total_cases=1,
            status="queued",
        )
        session.add(orun)
        session.flush()
        reserve(
            session,
            run_id=orun.id,
            account_id=fresh.id,
            case_count=1,
            period=now_period(),
            quota=10_000,
        )
        enqueue_run(session, orun.id)
        full_id, fresh_id = full.id, fresh.id

    with sessions() as session:
        full_queued = (
            session.query(Run.id).filter(Run.account_id == full_id, Run.status == "queued").count()
        )
        fresh_queued = (
            session.query(Run.id).filter(Run.account_id == fresh_id, Run.status == "queued").count()
        )
        assert full_queued >= limit  # would be rejected by create_run
        assert fresh_queued < limit  # would be accepted


def test_disabled_deadline_runs_unbounded(sessions, monkeypatch):
    """run_max_seconds=0 means no deadline: the run executes normally."""
    aid, rid = seed(sessions, count=2)  # no deadline set
    jid, token = claim(sessions, rid)
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))
    asyncio.run(execute_run(rid, job_id=jid, lease_token=token))
    with sessions() as session:
        assert session.get(Run, rid).status == "done"
        assert used_cases_for(aid, now_period(), session) == 2
