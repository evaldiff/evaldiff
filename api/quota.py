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
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    case,
    false,
    func,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session

from .models import Account, Run, RunCase

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
    value = session.execute(select(usage_expr(account_id, period).scalar_subquery())).scalar()
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
    per-account ``FOR NO KEY UPDATE`` lock serializes reservations
    under Postgres MVCC; SQLite uses an explicit write to serialize them.
    The caller must commit or roll back the transaction.

    The outcome is read from ``RETURNING`` (fetchall), not ``rowcount``:
    psycopg 3.x reports ``rowcount == -1`` for plain ``INSERT ... SELECT``
    statements, which would make a successful reservation look rejected.
    RETURNING yields one row per inserted row and is driver-portable.
    """
    # Serialize reservations before reading aggregate usage. FOR NO KEY
    # UPDATE is compatible with the FK key-share lock taken by Run inserts.
    lock_account(session, account_id)
    statement = (
        RUN_RESERVATIONS.insert()
        .from_select(
            ["run_id", "account_id", "period", "reserved", "charged", "settled", "created_at"],
            select(
                literal(run_id),
                literal(account_id),
                literal(period),
                literal(case_count),
                literal(0),
                false(),
                literal(time.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).where(usage_expr(account_id, period).scalar_subquery() + case_count <= quota),
        )
        .returning(RUN_RESERVATIONS.c.run_id)
    )
    result = session.execute(statement)
    # The caller owns the transaction: run, reservation, and job must
    # either all commit or all roll back.
    return len(result.fetchall()) == 1


def lock_account(session: Session, account_id: int) -> None:
    if session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT id FROM accounts WHERE id = :id FOR NO KEY UPDATE"),
            {"id": account_id},
        )
    else:
        session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(id=Account.id)
            .execution_options(synchronize_session=False)
        )


def settle_run(session: Session, *, run_id: int, charged: int, success: bool) -> bool:
    """Settle a run exactly once.

    - success: charged = OK cases (the rest is refunded from the reservation)
    - failure: charged = 0 (full refund)

    Returns True if THIS call performed the settlement, False if it had
    already been settled (crash recovery, duplicate call). The guarded
    UPDATE makes settlement idempotent. The caller commits it together
    with the terminal run/job status.
    """
    result = session.execute(
        update(RUN_RESERVATIONS)
        .where(RUN_RESERVATIONS.c.run_id == run_id)
        .where(RUN_RESERVATIONS.c.settled.is_(False))
        .values(settled=True, charged=charged if success else 0)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


def cleanup_for_reclaim(session: Session, run: Run) -> None:
    """Clear partial results while retaining the retry's quota reservation."""
    session.query(RunCase).filter(RunCase.run_id == run.id).delete(synchronize_session=False)
    run.started_at = None
    run.finished_at = None
    run.passed_cases = 0
    run.avg_score = None
    run.error = None


MIGRATIONS = Table(
    "quota_migrations",
    _ledger_meta,
    Column("name", String(80), primary_key=True),
)


def reserve_deadline(session: Session, run_id: int, run_max_seconds: int) -> None:
    """Reserve the run-wide deadline budget (NEEDS-DOING C).

    The deadline is an ABSOLUTE expiry persisted on the run, so a crash or a
    reclaimed attempt can never reset the budget: the remaining time is
    always ``deadline_at - now``. The caller owns the transaction (the run,
    its quota reservation, and the job commit together). Runs created with
    the limit disabled (0) have no deadline and execute unbounded.
    """
    if not run_max_seconds:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.execute(
        update(Run)
        .where(Run.id == run_id, Run.deadline_at.is_(None))
        .values(deadline_at=now + timedelta(seconds=run_max_seconds))
        .execution_options(synchronize_session=False)
    )


def remaining_deadline_seconds(run: Run) -> int | None:
    """Seconds until the run's absolute deadline; None when unlimited."""
    if run.deadline_at is None:
        return None
    return max(
        0, int((run.deadline_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
    )


def migrate_run_deadline_column(session: Session) -> None:
    """Add ``runs.deadline_at`` to databases created before Part C.

    ``create_all`` only creates missing TABLES — a pre-Part-C database
    stays healthy-looking while every run submission fails with
    ``no column named deadline_at``. This migration alters the existing
    table exactly once. Guarded by a marker row (portable across the
    psycopg 3.x ``rowcount == -1`` quirk), and additionally no-ops when
    the column already exists (e.g. a freshly ``create_all``-ed schema).
    The caller must stop all old processes first (as with the legacy
    usage backfill).
    """
    if (
        session.execute(
            select(MIGRATIONS.c.name).where(MIGRATIONS.c.name == "run_deadline_v1").limit(1)
        ).first()
        is not None
    ):
        return
    bind = session.bind
    if bind.dialect.name == "postgresql":
        exists = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_schema = current_schema AND table_name = 'runs'"
                " AND column_name = 'deadline_at' LIMIT 1"
            )
        ).first()
        if exists is None:
            session.execute(text("ALTER TABLE runs ADD COLUMN deadline_at TIMESTAMP"))
    else:
        cols = session.execute(text("PRAGMA table_info(runs)")).fetchall()
        if not any(col[1] == "deadline_at" for col in cols):
            session.execute(text("ALTER TABLE runs ADD COLUMN deadline_at DATETIME"))
    session.execute(MIGRATIONS.insert().values(name="run_deadline_v1"))
    session.commit()


def migrate_legacy_usage(session: Session) -> None:
    """Backfill once, atomically, before accepting requests or starting workers.

    Existing ledger entries are authoritative. Missing runs are reconstructed
    from their creation month and successful results. A synthetic, negative
    run ID preserves any legacy counter balance whose runs are unavailable.
    All old application processes must be stopped during this upgrade.

    Idempotency is guarded by a SELECT on the migration-marker row, not by
    ``ON CONFLICT DO NOTHING`` + ``rowcount``: some Postgres drivers
    (psycopg 3.x) report ``rowcount == -1`` for INSERT ... SELECT / ON
    CONFLICT statements, which would misreport an inserted marker row as a
    conflict and silently skip the backfill. A SELECT guard is portable.
    """
    if (
        session.execute(
            select(MIGRATIONS.c.name).where(MIGRATIONS.c.name == "legacy_usage_v1").limit(1)
        ).first()
        is not None
    ):
        return
    session.execute(MIGRATIONS.insert().values(name="legacy_usage_v1"))
    for account in session.query(Account).all():
        legacy_usage = 0
        missing = (
            session.query(Run)
            .filter(
                Run.account_id == account.id,
                ~Run.id.in_(select(RUN_RESERVATIONS.c.run_id)),
            )
            .all()
        )
        for run in missing:
            period = run.created_at.strftime("%Y-%m")
            terminal = run.status in ("done", "failed")
            charged = (
                sum(c.error is None and c.score is not None for c in run.cases)
                if run.status == "done"
                else 0
            )
            reserved = run.total_cases or run.dataset.case_count
            session.execute(
                RUN_RESERVATIONS.insert().values(
                    run_id=run.id,
                    account_id=account.id,
                    period=period,
                    reserved=reserved,
                    charged=charged,
                    settled=terminal,
                )
            )
            if period == account.quota_period:
                legacy_usage += charged if terminal else reserved
        # Pre-ledger counters stop changing once ledger accounting is active.
        # Subtract only reconstructed legacy runs, not new ledger-era usage.
        residual = max(0, account.used_cases - legacy_usage)
        if residual and account.quota_period:
            session.execute(
                RUN_RESERVATIONS.insert().values(
                    run_id=-account.id,
                    account_id=account.id,
                    period=account.quota_period,
                    reserved=0,
                    charged=residual,
                    settled=True,
                )
            )
    session.commit()
