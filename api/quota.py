"""Quota settlement on an explicit reservation ledger.

Why a ledger instead of arithmetic on ``accounts.used_cases``
--------------------------------------------------------------
The v0 accounting mutated one mutable counter and, on settlement, netted
the reservation against the CURRENT month. Two failures follow:

- Month-boundary bug: a run reserved in September and settled in October
  erased October usage (the reservation was subtracted from the wrong
  period).
- Non-idempotent refunds: after a crash, a run could be settled twice and
  refund twice.

The ledger makes billing state explicit:

  reserved — cases reserved at enqueue time (immutable after insert)
  charged  — cases actually billed at settlement (OK cases only)
  settled  — settlement happened (guarded UPDATE => at most once)

Usage for a period is ``SUM(settled ? charged : reserved)`` over the
account's reservations in that period — a refunded run drops out of
usage automatically. Enqueue is a single conditional INSERT with a
WHERE clause, so concurrent submissions are serialized by the database
instead of a read-then-write race.
"""

from __future__ import annotations

import time

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    case,
    func,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session

from .models import Run, RunCase

# Declarative table, created via create_all alongside the ORM models.
# A dedicated MetaData keeps the ledger independent of the model set.
_ledger_meta = MetaData()
RUN_RESERVATIONS = Table(
    "run_reservations",
    _ledger_meta,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, unique=True, index=True, nullable=False),
    Column("account_id", Integer, index=True, nullable=False),
    Column("period", String(7), nullable=False),  # quota month the reservation was made in
    Column("reserved", Integer, nullable=False, default=0),
    Column("charged", Integer, nullable=False, default=0),
    Column("settled", Boolean, nullable=False, default=False),
    Column("created_at", String(40)),
)


def ledger_metadata() -> MetaData:
    """Return the ledger's MetaData so create_all builds the table."""
    return _ledger_meta


def now_period() -> str:
    return time.strftime("%Y-%m")


def usage_expr(account_id: int, period: str):
    """SQL expression: cases the account owes for ``period`` right now.

    Unsettled reservations count at their reserved size; settled ones at
    what was actually billed (0 for fully refunded runs).
    """
    return select(
        func.coalesce(
            func.sum(
                case(
                    (RUN_RESERVATIONS.c.settled.is_(True), RUN_RESERVATIONS.c.charged),
                    else_=RUN_RESERVATIONS.c.reserved,
                )
            ),
            0,
        ),
    ).where(RUN_RESERVATIONS.c.account_id == account_id, RUN_RESERVATIONS.c.period == period)


def used_cases_for(account_id: int, period: str, session: Session) -> int:
    """Current owed cases for the account in ``period`` (for reporting)."""
    value = session.execute(
        select(usage_expr(account_id, period).scalar_subquery())
    ).scalar()
    return int(value or 0)


def reserve(
    session: Session,
    *,
    run_id: int,
    account_id: int,
    case_count: int,
    period: str,
    quota: int,
) -> bool:
    """Atomically reserve ``case_count`` cases for a run.

    Returns False when the account would exceed its monthly quota — in
    that case nothing was written. The quota check lives inside a
    conditional ``INSERT ... SELECT`` (scalar aggregate over the ledger
    in the WHERE clause), so there is no read-then-write race. A
    per-account ``FOR UPDATE`` lock serializes concurrent reservations
    under Postgres MVCC (SQLite ignores FOR UPDATE; its write lock
    already serializes).
    """
    sql = (
        "INSERT INTO run_reservations "
        "(run_id, account_id, period, reserved, charged, settled, created_at) "
        "SELECT :run_id, :account_id, :period, :case_count, 0, 0, :created_at "
        "WHERE (SELECT COALESCE(SUM(CASE WHEN settled THEN charged ELSE reserved END), 0) "
        "FROM run_reservations WHERE account_id = :account_id AND period = :period) "
        "+ :case_count <= :quota"
    )
    params = {
        "run_id": run_id,
        "account_id": account_id,
        "period": period,
        "case_count": case_count,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quota": quota,
    }
    if session.bind.dialect.name == "postgresql":
        # Per-account row lock serializes concurrent reservations under
        # MVCC; the conditional INSERT's WHERE clause then sees committed
        # state at commit time.
        session.execute(
            text("SELECT 1 FROM accounts WHERE id = :aid FOR UPDATE"), {"aid": account_id}
        )
    # SQLite: the caller's transaction is SERIALIZABLE (BEGIN IMMEDIATE,
    # see api/db.make_engine) — it holds the write lock from its first
    # statement, so a concurrent reservation commits (or is rejected)
    # BEFORE ours reads the ledger. The WHERE clause then sees committed
    # state and exactly one of the two passes. No read-then-write race.
    result = session.execute(text(sql), params)
    if (result.rowcount or 0) == 1:
        session.commit()
        return True
    # Rejected: roll back so the caller's pending rows (e.g. the flushed
    # Run) never get committed — the endpoint returns 429 and discards.
    session.rollback()
    return False


def settle_run(session: Session, *, run_id: int, charged: int, success: bool) -> bool:
    """Settle a run exactly once.

    - success: charged = OK cases (the rest is refunded from the reservation)
    - failure: charged = 0 (full refund)

    Returns True if THIS call performed the settlement, False if it had
    already been settled (crash recovery, duplicate call). The guarded
    UPDATE makes settlement idempotent by construction.
    """
    result = session.execute(
        update(RUN_RESERVATIONS)
        .where(RUN_RESERVATIONS.c.run_id == run_id)
        .where(RUN_RESERVATIONS.c.settled.is_(False))
        .values(settled=True, charged=charged if success else 0)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return (result.rowcount or 0) == 1


def cleanup_for_reclaim(session: Session, run: Run) -> None:
    """Idempotent cleanup for a run whose job died mid-flight: refund the
    reservation (if not already settled) and drop partial case rows so a
    requeued attempt starts clean. The job row is requeued by the caller."""
    settle_run(session, run_id=run.id, charged=0, success=False)
    session.query(RunCase).filter(RunCase.run_id == run.id).delete(synchronize_session=False)
    session.commit()



