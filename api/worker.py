"""Claim jobs atomically; recover only expired leases and fence stale writers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import update

from .db import state
from .leases import HEARTBEAT_SECONDS, LEASE_SECONDS, JobLease, LeaseLost, fence
from .models import Job, Run
from .quota import cleanup_for_reclaim, settle_run
from .runner import execute_run

_MAX_JOB_ATTEMPTS = 2
log = logging.getLogger("evaldiff.worker")


def enqueue_run(session, run_id: int) -> None:
    session.add(Job(kind="run", payload_json=json.dumps({"run_id": run_id}), status="pending"))
    session.commit()


def recover_abandoned_jobs(session) -> int:
    """Reclaim expired attempts atomically, retaining quota for a retry."""
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    # Pre-lease jobs can be recovered on upgrade. Old processes must be
    # stopped before starting this version (see the upgrade instructions).
    legacy = (
        session.query(Job)
        .outerjoin(JobLease, Job.id == JobLease.job_id)
        .filter(
            Job.status == "running",
            JobLease.job_id.is_(None),
        )
        .all()
    )
    for job in legacy:
        session.execute(
            insert(JobLease)
            .values(
                job_id=job.id,
                token="legacy",
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            .on_conflict_do_nothing()
        )
    session.commit()
    ids = (
        session.query(Job.id)
        .join(JobLease, Job.id == JobLease.job_id)
        .filter(
            Job.status == "running",
            Job.kind == "run",
            JobLease.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .all()
    )
    session.rollback()
    recovered = 0
    for (job_id,) in ids:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        won = session.execute(
            update(JobLease)
            .where(
                JobLease.job_id == job_id,
                JobLease.expires_at <= now,
            )
            .values(token=uuid4().hex, expires_at=now + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
        if won.rowcount != 1:
            session.rollback()
            continue
        job = session.get(Job, job_id, populate_existing=True)
        if job.status != "running":
            session.rollback()
            continue
        run_id = json.loads(job.payload_json or "{}").get("run_id")
        run = session.get(Run, run_id) if run_id else None
        if run is None or run.status in ("done", "failed"):
            job.status = "done"
        else:
            cleanup_for_reclaim(session, run)
            if job.attempts >= _MAX_JOB_ATTEMPTS:
                settle_run(session, run_id=run.id, charged=0, success=False)
                run.status = "failed"
                run.error = "abandoned: max attempts exceeded"
                run.finished_at = now
                job.status = "failed"
            else:
                run.status = "queued"
                job.status = "pending"
                recovered += 1
            job.claimed_by = None
        session.commit()
    return recovered


async def poll_once(session) -> int:
    job = session.query(Job).filter(Job.status == "pending").order_by(Job.id).first()
    if job is None:
        return 0
    job_id = job.id
    token = uuid4().hex
    updated = session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == "pending",
        )
        .values(status="running", claimed_by=token, attempts=Job.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        session.rollback()
        return 0
    session.merge(
        JobLease(
            job_id=job_id,
            token=token,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=LEASE_SECONDS),
        )
    )
    session.commit()
    return job_id


async def _heartbeat(job_id: int, token: str, attempt: asyncio.Task) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            with state.session_factory() as session:
                fence(session, job_id, token)
                session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("could not renew job %s; stopping attempt", job_id)
        attempt.cancel()


async def worker_loop(stop: asyncio.Event, interval: float = 2.0) -> None:
    while not stop.is_set():
        try:
            with state.session_factory() as session:
                recover_abandoned_jobs(session)
                job_id = await poll_once(session)
                job = session.get(Job, job_id) if job_id else None
                token = job.claimed_by if job else None
                payload = json.loads(job.payload_json) if job else {}
            if job_id:
                attempt = asyncio.create_task(
                    execute_run(
                        payload["run_id"],
                        job_id=job_id,
                        lease_token=token,
                    )
                )
                heartbeat = asyncio.create_task(_heartbeat(job_id, token, attempt))
                try:
                    await attempt
                    _finish_job(job_id, token)
                except asyncio.CancelledError:
                    if not heartbeat.done():
                        raise  # shutdown cancellation, not a failed heartbeat
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
        except Exception:
            log.exception("job loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _finish_job(job_id: int, token: str) -> None:
    with state.session_factory() as session:
        try:
            fence(session, job_id, token)
        except LeaseLost:
            return
        session.execute(
            update(Job).where(Job.id == job_id, Job.claimed_by == token).values(status="done")
        )
        session.commit()
