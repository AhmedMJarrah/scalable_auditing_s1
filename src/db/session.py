"""
Engine and session factory.

One engine per process, built from settings.database_url. SQLite gets WAL
mode and foreign-key enforcement turned on explicitly — neither is the
default, and both matter here (WAL for concurrent auditors, foreign keys so
a bad ingestion run fails loudly instead of leaving orphaned rows).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import Settings, get_settings
from src.core.logging_setup import get_logger

log = get_logger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    connect_args = {}
    if settings.is_sqlite:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False

    _engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        future=True,
    )
    if settings.is_sqlite:
        _configure_sqlite(_engine)

    log.info("db.engine.created", extra={
        "backend": "sqlite" if settings.is_sqlite else "postgres",
        "database_url": settings._redacted_db_url(),
    })
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, future=True
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    One transaction per block. Commits on clean exit, rolls back on any
    exception, always closes.

        with session_scope() as db:
            db.add(Auditor(...))
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
