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
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from api.db import Base, make_engine, state
from api.leases import JobLease
from api.models import Account, Dataset, Job, Run, RunCase
from api.quota import (
    MIGRATIONS,
    ledger_metadata,
    migrate_run_deadline_column,
    now_period,
    reserve,
    reserve_deadline,
    used_cases_for,
)
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


def seed_backlog(sessions, *, n=1, count=1):
    """ONE account with ``n`` queued runs (same account, sequential ids).
    Returns (account_id, [run_id, ...])."""
    with sessions() as session:
        account = Account(email=uuid.uuid4().hex + "@example.com", quota_period=now_period())
        dataset = Dataset(
            account=account,
            name="d",
            case_count=count,
            cases_json=json.dumps([{"input": "x", "expected": "x"}] * count),
        )
        run_ids = []
        for _ in range(n):
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
            enqueue_run(session, run.id)
            run_ids.append(run.id)
        return account.id, run_ids


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


def test_deadline_cancels_in_flight_case(sessions, monkeypatch):
    """P1: the deadline must stop an IN-FLIGHT model call, not just refuse
    to start a new case. A model call that finishes after the deadline must
    be cancelled, the run failed, and the case NOT charged."""
    aid, rid = seed(sessions, count=2, deadline=2)
    state_holder: dict = {}

    async def slow_model(*args, **kwargs):
        state_holder["entered"] = True
        try:
            await asyncio.sleep(30)  # far past the deadline
        except asyncio.CancelledError:
            state_holder["cancelled"] = True
            raise
        return "x", 1, 1

    monkeypatch.setattr("api.runner.call_model", slow_model)
    jid, token = claim(sessions, rid)
    asyncio.run(execute_run(rid, job_id=jid, lease_token=token))
    with sessions() as session:
        run = session.get(Run, rid)
        assert run.status == "failed"
        assert "deadline" in (run.error or "").lower()
        assert state_holder.get("cancelled")  # the in-flight call was cancelled
        assert session.query(RunCase).count() == 0  # no case produced/charged
        assert used_cases_for(aid, now_period(), session) == 0  # full refund


def test_legacy_database_gains_deadline_column(sessions):
    """P1: a pre-Part-C schema (no deadline_at column) must be migrated at
    startup, not left failing with 'no column named deadline_at'."""
    with sessions() as session:
        if session.bind.dialect.name == "sqlite":
            cols = [c[1] for c in session.execute(text("PRAGMA table_info(runs)")).fetchall()]
            assert "deadline_at" in cols  # create_all gave us the column
            session.execute(text("ALTER TABLE runs DROP COLUMN deadline_at"))
        else:
            session.execute(text("ALTER TABLE runs DROP COLUMN deadline_at"))
        session.commit()
    with sessions() as session:
        assert (
            session.execute(
                select(MIGRATIONS.c.name).where(MIGRATIONS.c.name == "run_deadline_v1")
            ).first()
            is None
        )
        migrate_run_deadline_column(session)
    # Idempotent: a second call must be a no-op (marker row guards it).
    with sessions() as session:
        migrate_run_deadline_column(session)
    with sessions() as session:
        if session.bind.dialect.name == "sqlite":
            cols = [c[1] for c in session.execute(text("PRAGMA table_info(runs)")).fetchall()]
        else:
            cols = [
                r[0]
                for r in session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = current_schema AND table_name = 'runs'"
                    )
                ).fetchall()
            ]
        assert "deadline_at" in cols


def test_backlog_account_cannot_monopolize_slots(sessions):
    """P2: with an active cap of 1, a 4-job backlog (ONE account) cannot
    fill all slots — the second claim must go to the smaller account."""
    # Backlog account A: 4 jobs in one account (sequential ids, first is
    # oldest). B: 1 job, created after A's.
    _, rid_a_list = seed_backlog(sessions, n=4, count=1)
    rid_a1 = rid_a_list[0]
    _, rid_b1 = seed(sessions, count=1)

    claimed_run_ids: list[int] = []
    for _ in range(4):
        with sessions() as session:
            jid = asyncio.run(poll_once(session, max_active_per_account=1))
            if not jid:
                break
            run_id = json.loads(session.get(Job, jid).payload_json)["run_id"]
            claimed_run_ids.append(run_id)
    # Slot 1: A's first job (oldest). A is now at its cap of 1, so A2-A4
    # are skipped and slot 2 goes to B. A plain id-order claim (the old
    # behavior) would have given A all four slots — monopolization.
    assert claimed_run_ids[0] == rid_a1
    assert rid_b1 in claimed_run_ids[1:]
    assert sum(1 for r in claimed_run_ids if r in rid_a_list) == 1  # A got 1 slot
    with sessions() as session:
        # A's remaining backlog stays pending (drained after A1 finishes).
        assert session.query(Job).filter(Job.status == "pending").count() == 3
