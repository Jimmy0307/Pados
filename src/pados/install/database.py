# SPDX-License-Identifier: AGPL-3.0-only
"""Database lanes for an installation: reachability, Alembic compatibility, and upgrades.

Targets come from Core ``Settings`` (``operational_revision`` / ``corpus_revision``) and upgrades run through the
existing ``alembic.ini`` / ``migrations/`` of the active runtime; nothing here defines a schema or a revision. The
optional proof lane needs the P6.19B graph and is reported, never migrated automatically.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from pados.config import Settings, lane_settings

_DATABASE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class LaneState(StrEnum):
    CURRENT = "CURRENT"
    UPGRADE_AVAILABLE = "UPGRADE_AVAILABLE"
    UNINITIALIZED = "UNINITIALIZED"
    INCOMPATIBLE = "INCOMPATIBLE"
    DATABASE_MISSING = "DATABASE_MISSING"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    UNREACHABLE = "UNREACHABLE"
    NOT_MANAGED = "NOT_MANAGED"


@dataclass(frozen=True)
class Lane:
    name: str
    database: str
    target_revision: str


@dataclass(frozen=True)
class LaneStatus:
    lane: str
    database: str
    target_revision: str
    state: LaneState
    revisions: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["revisions"] = list(self.revisions)
        return payload


def app_root() -> Path:
    """The directory holding ``alembic.ini`` and ``migrations/`` next to the imported ``src/pados``."""
    import pados

    return Path(pados.__file__).resolve().parents[2]


def managed_lanes(settings: Settings) -> tuple[Lane, ...]:
    return (
        Lane("operational", settings.operational_database_name, settings.operational_revision),
        Lane("corpus", settings.corpus_database_name, settings.corpus_revision),
    )


def target_error(settings: Settings) -> str | None:
    names = [settings.db_name, settings.operational_database_name, settings.corpus_database_name]
    if settings.product_database_name:
        names.append(settings.product_database_name)
    if any(not _DATABASE_NAME.fullmatch(name) for name in names):
        return "DATABASE_NAME_INVALID"
    # Protected developer databases, lane separation and the active lane: the existing doctor rule.
    from pados.cli.main import _doctor_database_target_error

    return _doctor_database_target_error(settings)


def classify_connection_error(exc: BaseException) -> LaneState:
    original = getattr(exc, "orig", exc)
    sqlstate = str(getattr(original, "sqlstate", "") or "")
    message = str(original).casefold()
    if sqlstate == "3D000" or ("database" in message and "does not exist" in message):
        return LaneState.DATABASE_MISSING
    if sqlstate in {"28P01", "28000"} or "authentication failed" in message or "no password supplied" in message:
        return LaneState.AUTHENTICATION_FAILED
    return LaneState.UNREACHABLE


def ancestors_of(target: str, root: Path | None = None) -> frozenset[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    base = root or app_root()
    config = Config(str(base / "alembic.ini"))
    config.set_main_option("script_location", str(base / "migrations"))
    script = ScriptDirectory.from_config(config)
    return frozenset(item.revision for item in script.iterate_revisions(target, "base"))


def _read_revisions(settings: Settings) -> tuple[str, ...] | None:
    from pados.db import get_engine

    engine = get_engine(settings)
    try:
        with engine.connect() as connection:
            exists = connection.execute(text("select to_regclass('public.alembic_version')")).scalar()
            if not exists:
                return None
            rows = connection.execute(text("select version_num from alembic_version")).scalars().all()
            return tuple(sorted(str(row) for row in rows))
    finally:
        engine.dispose()


def lane_status(settings: Settings, lane: Lane, ancestors: Callable[[str], frozenset[str]] = ancestors_of) -> LaneStatus:
    try:
        revisions = _read_revisions(lane_settings(settings, lane.database))
    except (DBAPIError, SQLAlchemyError) as exc:
        return LaneStatus(lane.name, lane.database, lane.target_revision, classify_connection_error(exc),
                          detail=type(getattr(exc, "orig", exc)).__name__)
    if revisions is None or not revisions:
        return LaneStatus(lane.name, lane.database, lane.target_revision, LaneState.UNINITIALIZED)
    if settings.excluded_revision in revisions or len(revisions) != 1:
        return LaneStatus(lane.name, lane.database, lane.target_revision, LaneState.INCOMPATIBLE, revisions,
                          "revision set is not a single accepted head")
    (current,) = revisions
    if current == lane.target_revision:
        return LaneStatus(lane.name, lane.database, lane.target_revision, LaneState.CURRENT, revisions)
    if current in ancestors(lane.target_revision):
        return LaneStatus(lane.name, lane.database, lane.target_revision, LaneState.UPGRADE_AVAILABLE, revisions)
    return LaneStatus(lane.name, lane.database, lane.target_revision, LaneState.INCOMPATIBLE, revisions,
                      "database revision is not an ancestor of this runtime's target (newer or foreign schema)")


def driver_error() -> str | None:
    """psycopg needs a libpq: bundled (psycopg-binary) or the operating system's own (pure-Python mode)."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return "DATABASE_DRIVER_UNAVAILABLE"
    return None


def check(settings: Settings) -> dict[str, Any]:
    error = driver_error() or target_error(settings)
    if error:
        return {"status": "BLOCKED", "error": error, "lanes": []}
    statuses = [lane_status(settings, lane) for lane in managed_lanes(settings)]
    if settings.product_database_name:
        statuses.append(LaneStatus("proof", settings.product_database_name, "P6.19B", LaneState.NOT_MANAGED,
                                   detail="proof lane is migrated explicitly (docs/product/QUICKSTART.md)"))
    return {"status": _summary(statuses), "lanes": [item.as_dict() for item in statuses]}


def _summary(statuses: list[LaneStatus]) -> str:
    states = {item.state for item in statuses if item.state is not LaneState.NOT_MANAGED}
    if states <= {LaneState.CURRENT}:
        return "CURRENT"
    if states & {LaneState.UNREACHABLE, LaneState.AUTHENTICATION_FAILED, LaneState.DATABASE_MISSING,
                 LaneState.INCOMPATIBLE}:
        return "BLOCKED"
    return "UPGRADE_REQUIRED"


def create_missing_databases(settings: Settings) -> list[str]:
    """Create managed lane databases that do not exist (explicit operator flag only)."""
    from sqlalchemy import create_engine

    error = target_error(settings)
    if error:
        raise ValueError(error)
    created: list[str] = []
    maintenance = lane_settings(settings, "postgres")
    engine = create_engine(maintenance.database_url, isolation_level="AUTOCOMMIT",
                           connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as connection:
            for lane in managed_lanes(settings):
                exists = connection.execute(text("select 1 from pg_database where datname = :name"),
                                            {"name": lane.database}).scalar()
                if not exists:
                    connection.execute(text(f'CREATE DATABASE "{lane.database}"'))
                    created.append(lane.database)
    finally:
        engine.dispose()
    return created


def upgrade(settings: Settings, *, root: Path | None = None) -> dict[str, Any]:
    """Upgrade managed lanes that are UNINITIALIZED or UPGRADE_AVAILABLE to their exact target revision."""
    before = check(settings)
    if before["status"] == "BLOCKED":
        return {"status": "BLOCKED", "before": before, "applied": []}
    base = root or app_root()
    applied: list[dict[str, Any]] = []
    for lane in before["lanes"]:
        if lane["state"] not in {LaneState.UNINITIALIZED.value, LaneState.UPGRADE_AVAILABLE.value}:
            continue
        environment = {**os.environ, "PADOS_DB_NAME": lane["database"]}
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "alembic", "-c", str(base / "alembic.ini"), "upgrade",
             lane["target_revision"]],
            cwd=base, env=environment, capture_output=True, text=True, check=False, timeout=600,
        )
        applied.append({"lane": lane["lane"], "target_revision": lane["target_revision"],
                        "exit_code": completed.returncode})
        if completed.returncode != 0:
            return {"status": "FAILED", "before": before, "applied": applied,
                    "detail": completed.stderr.strip().splitlines()[-1:] if completed.stderr else []}
    after = check(settings)
    return {"status": after["status"], "before": before, "applied": applied, "after": after}
