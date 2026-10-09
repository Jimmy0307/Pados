# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pados.config import Settings, get_settings


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


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    engine = get_engine(settings)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
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
