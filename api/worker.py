"""Worker: poll the jobs table and execute runs. v0: in-process asyncio loop."""

from __future__ import annotations

import asyncio
import json
import os
import socket

from .db import state
from .models import Job
from .runner import execute_run

_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def enqueue_run(session, run_id: int) -> None:
    session.add(Job(kind="run", payload_json=json.dumps({"run_id": run_id}), status="pending"))
    session.commit()


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
            # v0: log-and-continue; a stuck worker must not kill the process
            import logging

            logging.getLogger("evaldiff.worker").error("job loop error", exc_info=exc)
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
