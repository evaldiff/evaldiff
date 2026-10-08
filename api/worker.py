"""Worker: poll the jobs table and execute runs. v0: in-process asyncio loop.

Crash recovery (P2 fix)
------------------------
Only *pending* jobs were claimed before, so a process that died (or was
shut down) mid-run left its job stuck at "running" and its run at
"running" forever, with the quota reservation never refunded.
``recover_abandoned_jobs`` runs at startup and on each poll: it finds
running jobs whose run never reached a terminal state, refunds the
reservation (idempotent ledger settle), clears partial case rows, and
requeues the job. Reclaim is safe to repeat — settlement is guarded and
case-row cleanup is a no-op when there is nothing to delete.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket

from .db import state
from .models import Job, Run
from .quota import cleanup_for_reclaim
from .runner import execute_run

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_MAX_JOB_ATTEMPTS = 2  # a run that fails twice is a bad run, not a flaky one

log = logging.getLogger("evaldiff.worker")


def enqueue_run(session, run_id: int) -> None:
    session.add(Job(kind="run", payload_json=json.dumps({"run_id": run_id}), status="pending"))
    session.commit()


def recover_abandoned_jobs(session) -> int:
    """Refund + requeue runs whose previous job holder died. Returns count."""
    recovered = 0
    stuck = (
        session.query(Job)
        .filter(Job.status == "running", Job.kind == "run")
        .order_by(Job.id)
        .all()
    )
    for job in stuck:
        payload = json.loads(job.payload_json or "{}")
        run_id = payload.get("run_id")
        if run_id is None:
            job.status = "done"  # no run attached: nothing to recover
            session.commit()
            continue
        run = session.get(Run, run_id)
        if run is not None and run.status in ("done", "failed"):
            # The previous attempt actually finished; it just didn't get to
            # mark the job done. Close it out.
            job.status = "done"
            session.commit()
            continue
        if run is None:
            job.status = "done"  # run was deleted: nothing to recover
            session.commit()
            continue
        if job.attempts >= _MAX_JOB_ATTEMPTS:
            # Already ran (and died) twice: settle the reservation as a
            # failure and park the job — requeueing forever is not a fix.
            cleanup_for_reclaim(session, run)
            run.status = "failed"
            run.error = (run.error or "") + " [abandoned: max attempts exceeded]"
            job.status = "failed"
            session.commit()
            continue
        # Refund (idempotent) + drop partial cases, then requeue.
        cleanup_for_reclaim(session, run)
        run.status = "queued"
        job.status = "pending"
        job.claimed_by = None
        session.commit()
        recovered += 1
        log.info("recovered abandoned run %s (job %s)", run_id, job.id)
    return recovered


async def poll_once(session) -> int:
    """Claim one pending job (atomic on Postgres; SQLite is single-connection safe in v0)."""
    job = session.query(Job).filter(Job.status == "pending").order_by(Job.id).first()
    if job is None:
        return 0
    if session.bind.dialect.name == "postgresql":
        # idempotent claim (SQLite path is single-process; Postgres: rely on row lock)
        updated = (
            session.query(Job)
            .filter(Job.id == job.id, Job.status == "pending")
            .update(
                {
                    Job.status: "running",
                    Job.claimed_by: _WORKER_ID,
                    Job.attempts: Job.attempts + 1,
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if updated == 0:
            return 0
    else:
        job.status = "running"
        job.claimed_by = _WORKER_ID
        job.attempts += 1
        session.commit()
    return job.id


async def worker_loop(stop: asyncio.Event, interval: float = 2.0) -> None:
    while not stop.is_set():
        try:
            session = state.session_factory()
            try:
                recover_abandoned_jobs(session)
                job_id = await poll_once(session)
            finally:
                session.close()
            if job_id:
                payload = _load_payload(job_id)
                run_id = payload.get("run_id")
                if run_id:
                    await execute_run(run_id)
                _finish_job(job_id)
        except Exception as exc:
            # A stuck worker must not kill the process — log and continue;
            # the next tick (or the next process start) recovers the job.
            log.error("job loop error", exc_info=exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def _load_payload(job_id: int) -> dict:
    session = state.session_factory()
    try:
        job = session.get(Job, job_id)
        return json.loads(job.payload_json) if job else {}
    finally:
        session.close()


def _finish_job(job_id: int) -> None:
    session = state.session_factory()
    try:
        job = session.get(Job, job_id)
        if job:
            job.status = "done"
            session.commit()
    finally:
        session.close()
