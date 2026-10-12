"""Section C (part 1): run-wide deadlines and worker concurrency.

Set EVALDIFF_TEST_POSTGRES_URL to a disposable PostgreSQL database to run
these same tests against PostgreSQL as well as SQLite.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
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
    with sessions() as session:
        # A's first attempt is running (the cap counts running jobs).
        session.execute(update(Run).where(Run.id == rid_a1).values(status="running"))
        session.commit()
    for _ in range(3):
        with sessions() as session:
            jid = asyncio.run(poll_once(session, max_active_per_account=1))
            if not jid:
                break
            run_id = json.loads(session.get(Job, jid).payload_json)["run_id"]
            claimed_run_ids.append(run_id)
    assert rid_b1 in claimed_run_ids[1:], (
        f"smaller account starved behind the capped backlog: {claimed_run_ids}"
    )
    assert sum(1 for r in claimed_run_ids if r in rid_a_list) == 1  # A got 1 slot
    with sessions() as session:
        # A's remaining backlog stays pending (drained after A1 finishes).
        assert session.query(Job).filter(Job.status == "pending").count() == 3


def test_deadline_covers_storage_write(sessions, monkeypatch):
    """P1: the deadline covers the case's result write to storage, not
    just the model call. A storage backend that finishes AFTER the
    deadline must not charge the case or mark the run done.

    P2 (bounded pool): the write must NOT be awaited to completion — a
    stalled write keeps running in its (bounded) pool thread while the
    run settles at the deadline. The previous version of this test
    asserted ``elapsed >= 2.8``, which ``asyncio.run``'s executor
    teardown made pass even when the run awaited the slow write:
    asyncio.run() joins the loop's executor threads during teardown,
    so the clock always included the 3s stall. The assertion that
    catches the real bug is the opposite bound: the run finishes at the
    deadline (elapsed < the write's 3s), with the slow thread still
    alive — proving the run did NOT wait for it.
    """
    aid, rid = seed(sessions, count=1, deadline=2)

    # Isolated fresh pool for this test: the admission/saturation tests
    # leave threads parked in blocker.wait() for ~10s, and they share
    # this pool (default 4 threads). A write delayed behind them would
    # change the deadline-vs-write timing this test pins. A fresh, empty
    # pool makes the write start immediately so the deadline (2s) lapses
    # deterministically before the 3s write lands.
    import api.runner as runner_module

    monkeypatch.setattr(runner_module, "_pool", None)
    monkeypatch.setattr(runner_module, "_gate", None)
    monkeypatch.setattr(state, "settings", Settings(max_storage_threads=1))

    class SlowStorage(InMemoryStorage):
        started = threading.Event()
        finished = threading.Event()

        def put_json(self, key: str, obj: object) -> str:
            self.started.set()
            # Hold the write well past the 2s deadline (then finish,
            # like a slow backend that completes late).
            time.sleep(3)
            out = super().put_json(key, obj)
            self.finished.set()
            return out

    slow = SlowStorage()
    monkeypatch.setattr(state, "storage", slow)
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))
    jid, token = claim(sessions, rid)
    started_at = time.monotonic()
    asyncio.run(asyncio.wait_for(execute_run(rid, job_id=jid, lease_token=token), timeout=15))
    elapsed = time.monotonic() - started_at
    with sessions() as session:
        run = session.get(Run, rid)
        assert run.status == "failed"  # NOT done — the late storage write counts for nothing
        assert "deadline" in (run.error or "").lower()
        assert session.query(RunCase).count() == 0  # case not charged
        assert used_cases_for(aid, now_period(), session) == 0  # full refund
    assert SlowStorage.started.is_set()  # the storage write was actually entered
    # P1: the run settled at the deadline, BEFORE the 3s write landed.
    assert elapsed < 2.9, (
        f"run awaited the slow storage write (elapsed={elapsed:.2f}s) — the deadline "
        "must settle the run at 2s while the write finishes in its pool thread"
    )
    # P2: the stalled write is still running in its thread (not awaited,
    # not killed — it finishes later, discarded, and occupies at most one
    # of the bounded pool's threads).
    assert slow.started.is_set() and not slow.finished.is_set()


def seed_storage_dataset(sessions, *, deadline: int | None = None):
    """Account + STORAGE-based dataset (cases in object storage, not inline)
    + queued run + quota reservation. Returns (account_id, run_id)."""
    with sessions() as session:
        account = Account(email=uuid.uuid4().hex + "@example.com", quota_period=now_period())
        dataset = Dataset(
            account=account,
            name="d",
            case_count=1,
            storage_key=f"datasets/{uuid.uuid4().hex}",
            cases_json=None,
        )
        run = Run(
            account=account,
            dataset=dataset,
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
            account_id=account.id,
            case_count=1,
            period=now_period(),
            quota=10_000,
        )
        if deadline is not None:
            reserve_deadline(session, run.id, deadline)
        enqueue_run(session, run.id)
        return account.id, run.id


def test_stalled_dataset_read_fails_run(sessions, monkeypatch):
    """P1: the deadline covers the DATASET READ (before the first case),
    not just case work. A stalled S3 read must fail the run — settling
    quota and freeing the worker slot at the deadline — instead of
    holding the slot and the reservation until the client's transport
    timeout. As with the write test, the real assertion is the OPPOSITE
    bound: the run settles at the deadline (elapsed < the read's stall),
    with the slow thread still alive (proving it was not awaited;
    asyncio.run's executor join would have masked an awaited read)."""
    aid, rid = seed_storage_dataset(sessions, deadline=2)

    class StalledRead(InMemoryStorage):
        started = threading.Event()
        finished = threading.Event()

        def get_json(self, key: str) -> object:
            self.started.set()
            time.sleep(3)  # well past the 2s deadline
            out = super().get_json(key)
            self.finished.set()
            return out

    stalled = StalledRead()
    monkeypatch.setattr(state, "storage", stalled)
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))
    jid, token = claim(sessions, rid)
    started_at = time.monotonic()
    asyncio.run(asyncio.wait_for(execute_run(rid, job_id=jid, lease_token=token), timeout=15))
    elapsed = time.monotonic() - started_at
    with sessions() as session:
        run = session.get(Run, rid)
        assert run.status == "failed"  # NOT left running — slot freed, quota settled
        assert "deadline" in (run.error or "").lower()
        assert session.query(RunCase).count() == 0  # nothing was executed, nothing charged
        assert used_cases_for(aid, now_period(), session) == 0  # full refund
        job = session.query(Job).first()
        assert job.status == "running"  # the JOB is finalized by the worker; the run is settled
    assert StalledRead.started.is_set()  # the read was actually entered
    # The run settled at the 2s deadline, while the 3s read kept running.
    assert elapsed < 2.9, (
        f"run awaited the stalled dataset read (elapsed={elapsed:.2f}s) — the "
        "deadline must settle the run at 2s, not at the read's completion"
    )
    assert stalled.started.is_set() and not stalled.finished.is_set()


def test_stalled_writes_cannot_exhaust_pool(sessions, monkeypatch):
    """P2: repeated stalled writes must not consume an unbounded number
    of threads. With a pool of ONE storage thread, two concurrent runs
    whose writes each stall for 2.5s cannot run their writes in parallel
    — the second waits at admission (BEFORE the executor queue), so at
    most ONE write thread is ever live. An unbounded pool
    (asyncio.to_thread) would have two live threads at once.

    Also pins the deadline semantics for the queued case: the run whose
    write could not get a slot before its budget ran out is failed and
    refunded, not charged."""
    # One storage thread + fresh pool/gate (other tests may have created
    # their own).
    import api.runner as runner_module

    monkeypatch.setattr(runner_module, "_pool", None)
    monkeypatch.setattr(runner_module, "_gate", None)
    monkeypatch.setattr(state, "settings", Settings(max_storage_threads=1))

    live = 0
    peak = 0
    lock = threading.Lock()

    class BoundedStorage(InMemoryStorage):
        def _track(self, fn):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                return fn()
            finally:
                with lock:
                    live -= 1

        def get_json(self, key: str) -> object:
            def _get():
                time.sleep(1.5)
                return [{"input": "x", "expected": "x"}]

            return self._track(_get)

        def put_json(self, key: str, obj: object) -> str:
            def _put():
                time.sleep(2.5)  # longer than the budget left for the queued run
                return key

            return self._track(_put)

    storage = BoundedStorage()
    monkeypatch.setattr(state, "storage", storage)
    monkeypatch.setattr("api.runner.call_model", fake_model(sessions))

    # deadline=6: run A's 1.5s read + 2.5s write land inside its budget;
    # run B is serialised behind A on the single pool thread, so its
    # write starts after A's and lands past its own deadline — failed +
    # refunded. (Both exceed a 3s budget, so 6 is the tight working value.)
    aid_a, rid_a = seed_storage_dataset(sessions, deadline=6)
    aid_b, rid_b = seed_storage_dataset(sessions, deadline=6)

    async def drive():
        a = asyncio.wait_for(execute_run(rid_a), timeout=20)
        b = asyncio.wait_for(execute_run(rid_b), timeout=20)
        return await asyncio.gather(a, b, return_exceptions=True)

    asyncio.run(drive())

    with sessions() as session:
        run_a = session.get(Run, rid_a)
        run_b = session.get(Run, rid_b)
        statuses = sorted([run_a.status, run_b.status])
        # With one thread and a 2.5s write, exactly one run can finish its
        # write inside its 6s budget; the other is failed + refunded.
        assert statuses == ["done", "failed"], f"expected one done + one failed, got {statuses}"
        for aid, run in ((aid_a, run_a), (aid_b, run_b)):
            if run.status == "failed":
                assert "deadline" in (run.error or "").lower()
                assert used_cases_for(aid, now_period(), session) == 0  # refunded
            else:
                assert session.query(RunCase).filter(RunCase.run_id == run.id).count() == 1
    # The core P2 assertion: the writes never ran in parallel — the pool
    # capped live storage threads at 1. (An unbounded pool: peak == 2.)
    assert peak <= 1, f"storage pool was not bounded: {peak} concurrent storage threads"


def test_timed_out_submissions_do_not_enter_queue(sessions, monkeypatch):
    """P2 (queue admission): calls that time out while waiting for a pool
    slot must not leave work items behind in the executor's internal
    queue — the reported leak. With submit-then-wait (run_in_executor),
    100 timed-out submissions enqueue 100 cancelled work items behind
    one blocked thread and the queue grows with every stall. With
    admission, a waiter that times out is rejected BEFORE pool.submit:
    zero items are ever enqueued for it."""
    import api.runner as runner_module

    monkeypatch.setattr(runner_module, "_pool", None)
    monkeypatch.setattr(runner_module, "_gate", None)
    monkeypatch.setattr(runner_module, "_pool_size", 1)
    monkeypatch.setattr(state, "settings", Settings(max_storage_threads=1))

    blocker = threading.Event()
    entered = threading.Event()

    def blocked():
        entered.set()
        blocker.wait(10)
        return "ok"

    async def drive():
        pool, _gate = runner_module._storage_pool()

        async def _wait_threaded(event: threading.Event, seconds: float = 10.0) -> None:
            deadline = time.monotonic() + seconds
            while not event.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        in_flight = asyncio.create_task(runner_module._storage_call(blocked))
        await _wait_threaded(entered)
        # The in-flight item is RUNNING (dequeued), not queued.
        assert pool._work_queue.qsize() == 0

        waiters = [
            asyncio.wait_for(runner_module._storage_call(lambda: "x"), timeout=0.3)
            for _ in range(100)
        ]
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(r, asyncio.TimeoutError) for r in results)
        queued = pool._work_queue.qsize()
        blocker.set()
        assert await in_flight == "ok"
        return queued

    queued = asyncio.run(drive())
    # The 100 timed-out submissions left ZERO work items in the executor
    # queue — they were rejected at admission, before pool.submit.
    assert queued == 0, f"{queued} work items queued behind the blocked thread"


def test_cross_loop_admission_still_bounded(sessions, monkeypatch):
    """P2 (cross-loop admission, the reported leak): admission must be
    PROCESS-WIDE, not per event loop — the pool is process-wide, so a
    per-loop semaphore reopens the queue-growth path when a new loop
    appears in the same process (e.g. loop restarts in tests or a
    worker re-spawning an executor).

    Reproduction the user verified: one blocked call occupies the
    single storage thread, then 20 successive event loops each attempt
    a submission. With a per-loop semaphore each loop sees a fresh full
    capacity and submits — 20 cancelled work items pile up behind the
    blocked thread and the queue grows without bound.

    With a process-wide gate, the 2nd..20th loops have zero free slots
    and are rejected at admission BEFORE pool.submit: no work items are
    ever enqueued from any of them, and the gate stays consistent
    (free == 0 while the blocked call is in flight, no phantom slots)
    across all 20 loops.
    """
    import api.runner as runner_module

    monkeypatch.setattr(runner_module, "_pool", None)
    monkeypatch.setattr(runner_module, "_gate", None)
    monkeypatch.setattr(state, "settings", Settings(max_storage_threads=1))

    blocker = threading.Event()
    entered = threading.Event()

    def blocked():
        entered.set()
        blocker.wait(10)
        return "ok"

    def submit_blocked():
        # One loop: submit the single in-flight blocked call and hold it.
        async def inner():
            t = asyncio.create_task(runner_module._storage_call(blocked, timeout=60))
            # Wait until the call is REALLY in the pool (entered), so the
            # main thread can rely on the slot being occupied.
            deadline = time.monotonic() + 10
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            await t

        return asyncio.run(inner())

    def submit_waiter(timeout_s: float):
        # One loop: attempt a fast (non-blocking) storage call that
        # should be rejected at admission (zero free slots).
        async def inner():
            try:
                await asyncio.wait_for(
                    runner_module._storage_call(lambda: "x"),
                    timeout=timeout_s,
                )
                return "admitted"
            except asyncio.TimeoutError:
                return "timeout"

        return asyncio.run(inner())

    # Loop 1: the blocked call occupies the single slot.
    t = threading.Thread(target=submit_blocked, daemon=True)
    t.start()
    # Deterministic readiness: wait for the blocked call to be RUNNING in
    # the pool before spawning waiters (no startup race between the two
    # threads — a previous version raced and let the first waiter in).
    assert entered.wait(timeout=10), "blocked call never started in the pool"

    # Loops 2..21: 20 successive loops each attempt a submission.
    # Each times out at admission (zero free slots) — none enqueues.
    for _ in range(20):
        assert submit_waiter(0.3) == "timeout", (
            "a waiter was admitted while the pool was stalled — admission is not process-wide"
        )

    pool, gate = runner_module._storage_pool()
    # The blocked thread is still running (blocker not yet set): the
    # single slot is held, so free == 0 (not 20 phantom slots leaked
    # by per-loop admission).
    assert gate.free == 0, f"gate leaked {gate.free} phantom slots across loops"
    assert len(gate._waiters) == 0, f"{len(gate._waiters)} waiters parked"
    # The 20 timed-out loops left ZERO work items behind the blocked
    # thread — the queue did not grow.
    queued = pool._work_queue.qsize()
    blocker.set()
    t.join(timeout=15)
    # Give the release a moment to fire on the pool thread.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and gate.free != 1:
        time.sleep(0.01)
    assert gate.free == 1, f"gate did not recover after blocked call finished (free={gate.free})"
    assert queued == 0, f"{queued} work items queued across 20 loops"


def test_storage_call_lifetime_budget_bounds_total(sessions, monkeypatch):
    """P1: a storage call's TOTAL lifetime is bounded, not just the time
    BETWEEN socket reads (botocore's read_timeout is a per-read value —
    a peer dribbling bytes can outlive it, and a plain to_thread wait
    would block the caller for the whole worker lifetime). A call whose
    worker sleeps 4s with a 1.2s TOTAL budget must unblock the caller at
    ~the budget — NOT at 4s — surface a TimeoutError (so deadline guards
    treat it exactly like a deadline lapse: discarded, not charged), and
    leave the worker thread abandoned in place (not killed, not joined)."""
    import api.runner as runner_module

    monkeypatch.setattr(runner_module, "_pool", None)
    monkeypatch.setattr(runner_module, "_gate", None)
    monkeypatch.setattr(state, "settings", Settings(max_storage_threads=1))

    slow_worker = {}

    def slow():
        slow_worker["name"] = threading.current_thread().name
        time.sleep(4)  # well past the 1.2s TOTAL budget
        return "late"

    async def drive():
        started = time.monotonic()
        try:
            await asyncio.wait_for(runner_module._storage_call(slow, timeout=1.2), timeout=3.0)
            raise AssertionError("the 4s call should have been cut at its budget")
        except asyncio.TimeoutError:
            # TimeoutError (NOT the raw worker result, NOT a hang) is what
            # the deadline guards in _execute_case / execute_run catch.
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, (
            f"caller blocked {elapsed:.2f}s for a 4s worker with a 1.2s budget — "
            "the TOTAL-lifetime bound must unblock the caller at the budget"
        )

    asyncio.run(drive())
    # The worker thread was entered (it really ran in the pool) and was
    # ABANDONED in place — still sleeping, not killed or awaited.
    assert slow_worker.get("name", "").startswith("evaldiff-storage")


def test_account_cap_atomic_across_workers(sessions):
    """P2: the per-account cap must be atomic across concurrent workers.
    Two workers claiming the SAME account's two pending jobs at the SAME
    instant, cap 1: at most ONE may win (the old snapshot-then-claim race
    let both pass the check and both claim, leaving two running jobs for
    a cap of one)."""
    with sessions() as session:
        if session.bind.dialect.name != "postgresql":
            pytest.skip("FOR NO KEY UPDATE row locking requires PostgreSQL")
    aid, run_ids = seed_backlog(sessions, n=2, count=1)
    with sessions() as session:
        # Preconditions: both jobs pending, nothing running yet.
        jobs = session.query(Job).all()
        assert len(jobs) == 2
        for j in jobs:
            assert j.status == "pending"
        assert (
            session.query(Run.id).filter(Run.account_id == aid, Run.status == "running").count()
            == 0
        )
        # Both seeded runs belong to the same account (the race's subject).
        for rid in run_ids:
            assert session.get(Run, rid).account_id == aid

    import threading

    barrier = threading.Barrier(2, timeout=10)
    results: list[int] = []
    result_lock = threading.Lock()

    def worker():
        with sessions() as session:
            barrier.wait()
            r = asyncio.run(poll_once(session, max_active_per_account=1))
        with result_lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(results) == 2, "one or both workers did not complete"

    with sessions() as session:
        winners = [r for r in results if r]
        assert len(winners) == 1, (
            f"cap of 1 broken by the claim race: {len(winners)} workers claimed "
            f"for the same account (results={results})"
        )
        running = (
            session.query(Run.id).filter(Run.account_id == aid, Run.status == "running").count()
        )
        assert running == 0  # the claim flips the JOB, not the run — exactly one slot held
        running_jobs = session.query(Job).filter(Job.status == "running").count()
        assert running_jobs == 1
        pending = session.query(Job).filter(Job.status == "pending").count()
        assert pending == 1  # the other job is still queued, not lost
        assert used_cases_for(aid, now_period(), session) == 2  # both reservations intact
