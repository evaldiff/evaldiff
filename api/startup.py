"""Serialize schema creation and one-time migrations before serving traffic."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import Base
from .quota import ledger_metadata, migrate_legacy_usage, migrate_run_deadline_column

# Database-scoped, stable across releases; acquire before touching any tables.
_STARTUP_LOCK = 0x4556414C44494646


def initialize_database(engine) -> None:
    """Initialize atomically on one connection, including migration commits.

    Sessions bound to an externally managed transaction do not commit that
    transaction. The migration helpers' commits therefore cannot release the
    startup lock early. PostgreSQL releases the transaction advisory lock on
    commit, rollback, or connection loss; SQLite uses its database write lock.
    Old-version processes must still be stopped before upgrading.
    """
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # A broken/blocked startup must fail rather than wait indefinitely.
            connection.exec_driver_sql("SET LOCAL lock_timeout = '30s'")
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STARTUP_LOCK})
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise RuntimeError("startup migrations support PostgreSQL and SQLite only")
        Base.metadata.create_all(connection)
        ledger_metadata().create_all(connection)
        with Session(bind=connection) as session:
            # Legacy backfill reads ORM Run rows, including deadline_at.
            migrate_run_deadline_column(session)
            migrate_legacy_usage(session)
