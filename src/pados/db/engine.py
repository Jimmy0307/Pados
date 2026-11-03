# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pados.config import Settings, get_settings
from pados.operability.errors import WriteOutcome
from pados.operability.resilience import DeadlineExceeded, current_deadline

DB_DEADLINE_EXCEEDED = "DB_DEADLINE_EXCEEDED"
_QUERY_CANCELED = "57014"  # PostgreSQL query_canceled, raised when statement_timeout fires


def get_engine(settings: Settings | None = None) -> Engine:
    active_settings = settings or get_settings()
    return create_engine(
        active_settings.database_url,
        echo=active_settings.db_echo,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 2},
    )


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), expire_on_commit=False)


def bind_parent_deadline(session: Session) -> Session:
    """OP-004: every transaction of ``session`` gets the caller's remaining budget as its ``statement_timeout``.

    Applied at each transaction begin (``SET LOCAL`` semantics through ``set_config(..., true)``), so a commit inside
    the scope does not drop it. A budget already spent refuses the transaction before its first statement. Without a
    parent deadline nothing is set and the server default applies.
    """
    def apply(_session: Session, _transaction: Any, connection: Any) -> None:
        deadline = current_deadline()
        if deadline is None:
            return
        milliseconds = max(1, int(deadline.cap(float("inf")) * 1000))
        connection.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(milliseconds)})

    event.listen(session, "after_begin", apply)
    return session


def _cancelled_by_deadline(exc: BaseException) -> bool:
    return current_deadline() is not None and getattr(getattr(exc, "orig", None), "sqlstate", None) == _QUERY_CANCELED


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """One unit of work. A statement cancelled by the parent deadline is ``DB_DEADLINE_EXCEEDED``: nothing was written
    when it hit a statement (the transaction rolls back), and the outcome is UNKNOWN when it hit the commit."""
    engine = get_engine(settings)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = bind_parent_deadline(factory())
    try:
        try:
            yield session
        except Exception as exc:
            session.rollback()
            if _cancelled_by_deadline(exc):
                raise DeadlineExceeded(DB_DEADLINE_EXCEEDED) from exc
            raise
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            if _cancelled_by_deadline(exc):
                raise DeadlineExceeded(DB_DEADLINE_EXCEEDED, write_occurred=WriteOutcome.UNKNOWN) from exc
            raise
    finally:
        session.close()
        engine.dispose()


def check_database(settings: Settings | None = None) -> dict[str, str]:
    active_settings = settings or get_settings()
    engine = get_engine(active_settings)
    with engine.connect() as conn:
        version = conn.execute(text("select version()")).scalar_one()
        vector_installed = conn.execute(
            text("select exists(select 1 from pg_extension where extname = 'vector')")
        ).scalar_one()
    return {
        "status": "ok",
        "database_url": active_settings.safe_database_url,
        "version": str(version),
        "pgvector": str(bool(vector_installed)).lower(),
    }
