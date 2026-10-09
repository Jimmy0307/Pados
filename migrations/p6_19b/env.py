from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool, text

from pados.config import get_settings


config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
target_metadata = None

EXPECTED_MAIN_ALEMBIC_HEADS = frozenset({"20260730_0011", "20260808_0013"})


def _assert_main_schema_precondition(connection: Connection) -> None:
    """Require the exact formal main-schema heads before the proof graph runs."""
    version_table = connection.execute(
        text("SELECT to_regclass('public.alembic_version')")
    ).scalar_one_or_none()
    if version_table is None:
        raise RuntimeError(
            "P6_19B_MAIN_SCHEMA_PRECONDITION_FAILED: public.alembic_version is missing"
        )
    revisions = frozenset(
        str(value)
        for value in connection.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        ).scalars()
    )
    if revisions != EXPECTED_MAIN_ALEMBIC_HEADS:
        expected = ",".join(sorted(EXPECTED_MAIN_ALEMBIC_HEADS))
        actual = ",".join(sorted(revisions)) or "<empty>"
        raise RuntimeError(
            "P6_19B_MAIN_SCHEMA_PRECONDITION_FAILED: "
            f"expected heads {expected}; found {actual}"
        )


def run_migrations_offline() -> None:
    raise RuntimeError(
        "P6_19B_MAIN_SCHEMA_PRECONDITION_REQUIRES_ONLINE_PREFLIGHT"
    )


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _assert_main_schema_precondition(connection)
        # The preflight SELECT starts SQLAlchemy's autobegin transaction. End it
        # before Alembic opens its own migration transaction; otherwise the
        # migration can be rolled back when the connection closes.
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="p6_19b_alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
