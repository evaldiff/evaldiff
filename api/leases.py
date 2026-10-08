"""Renewable job leases and write fencing for worker recovery."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 15


class JobLease(Base):
    __tablename__ = "job_leases"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    token: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class LeaseLost(Exception):
    """This attempt may no longer write results or settle quota."""


def fence(session, job_id: int, token: str) -> None:
    """Lock and renew the lease in the SAME transaction as protected writes."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session.no_autoflush:
        result = session.execute(
            update(JobLease)
            .where(JobLease.job_id == job_id, JobLease.token == token, JobLease.expires_at > now)
            .values(expires_at=now + timedelta(seconds=LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise LeaseLost(f"job {job_id}: lease expired or ownership changed")
