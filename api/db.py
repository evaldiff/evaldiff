"""Database engine, Base, and the shared state holder."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # SERIALIZABLE => BEGIN IMMEDIATE: each transaction takes
        # SQLite's write lock at its first statement, so concurrent
        # writes (e.g. two parallel quota reservations) are fully
        # serialized — the conditional INSERT in api/quota.reserve()
        # then reads committed ledger state instead of a stale snapshot.
        kwargs["isolation_level"] = "SERIALIZABLE"
        # Generous lock-wait so a blocked writer waits for the concurrent
        # writer instead of failing after 5s.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        engine = create_engine(url, **kwargs)
        if not url.endswith(":memory:"):
            # WAL: readers (API list endpoints) run concurrently with
            # writers (worker settling reservations) on the same file.
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        return engine
    return create_engine(url, **kwargs)


class State:
    """Process-global wiring (v0: single process API + worker)."""

    settings = None
    storage = None
    session_factory: Callable[[], Session] | None = None
    limiter = None


state = State()


def get_session() -> Iterator[Session]:
    session = state.session_factory()
    try:
        yield session
    finally:
        session.close()
