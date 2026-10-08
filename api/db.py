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
        kwargs["connect_args"] = {"check_same_thread": False}
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
