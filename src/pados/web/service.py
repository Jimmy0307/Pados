# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import datetime as dt
import platform
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, Table, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pados import __version__
from pados.config import Settings, lane_revisions_accepted
from pados.control import service as control_service
from pados.db import check_database, get_engine
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.web import WEB_VERSION

MAX_PAGE_SIZE = 100
PUBLIC_SCHEMA = "public"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|credential|connection[_-]?string|private[_-]?key|(?:^|[_-])token(?:$|[_-]))",
    re.IGNORECASE,
)
_ZERO_PROJECT = "00000000-0000-0000-0000-000000000000"


class UnknownTableError(ValueError):
    pass


def _safe_value(value: Any, *, field_name: str | None = None) -> Any:
    if field_name and _SECRET_NAME.search(field_name):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (UUID, bytes, bytearray, memoryview)):
        return str(value) if not isinstance(value, (bytes, bytearray, memoryview)) else "[BINARY]"
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(item) for item in value]
    return str(value)


def safe_row(row: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): _safe_value(value, field_name=str(key)) for key, value in row.items()}


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise UnknownTableError(f"invalid {label}")
    return value


def _public_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names(schema=PUBLIC_SCHEMA))


def _reflect_table(engine: Engine, schema: str, table_name: str) -> Table:
    schema = _validate_identifier(schema, "schema")
    table_name = _validate_identifier(table_name, "table")
    if schema != PUBLIC_SCHEMA or table_name not in _public_tables(engine):
        raise UnknownTableError("table is not available in the read-only explorer")
    metadata = MetaData()
    return Table(table_name, metadata, schema=schema, autoload_with=engine)


def _pagination(page: int, page_size: int, total: int) -> dict[str, Any]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": page * page_size < total,
    }


def _migration_status(settings: Settings, engine: Engine, expected_revision: str) -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            current = sorted(
                str(item)
                for item in connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
        excluded_applied = settings.excluded_revision in current
        lane = (
            "corpus"
            if expected_revision == settings.corpus_revision != settings.operational_revision
            else "operational"
        )
        accepted = lane_revisions_accepted(settings, lane, current)
        return {
            "status": "OK" if accepted and not excluded_applied else "ERROR",
            "current_revision": current[0] if len(current) == 1 else current,
            "expected_revision": expected_revision,
            "expected_head": expected_revision,
            "current_heads": current,
            "expected_heads": [expected_revision],
            "excluded_revision": settings.excluded_revision,
            "excluded_revision_applied": excluded_applied,
        }
    except Exception:  # noqa: BLE001 - migration status is reported, never raised
        return {
            "status": "UNKNOWN",
            "current_revision": None,
            "expected_revision": expected_revision,
            "expected_head": expected_revision,
            "current_heads": [],
            "expected_heads": [expected_revision],
            "excluded_revision": settings.excluded_revision,
            "excluded_revision_applied": False,
        }


def _expected_revision_for_database(settings: Settings) -> str:
    if settings.db_name == settings.corpus_database_name:
        return settings.corpus_revision
    return settings.operational_revision


def _count_table(session: Session, engine: Engine, table_name: str) -> int | None:
    if table_name not in _public_tables(engine):
        return None
    table = _reflect_table(engine, PUBLIC_SCHEMA, table_name)
    return int(session.execute(select(func.count()).select_from(table)).scalar_one())


def _project_counts(session: Session, engine: Engine, table_names: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = _public_tables(engine)
    for table_name in table_names:
        if table_name not in tables:
            continue
        table = _reflect_table(engine, PUBLIC_SCHEMA, table_name)
        if "project_id" not in table.c:
            continue
        rows = session.execute(
            select(table.c.project_id, func.count().label("count")).group_by(table.c.project_id)
        ).all()
        for project_id, count in rows:
            if project_id is not None:
                counts[str(project_id)] = counts.get(str(project_id), 0) + int(count)
    return counts


def metrics(session: Session, engine: Engine) -> dict[str, int]:
    def count(*names: str) -> int:
        return sum(_count_table(session, engine, name) or 0 for name in names)

    return {
        "projects": count("projects"),
        "memories": count("memory_records"),
        "tasks": count("tasks", "project_tasks", "task_goals"),
        "runs": count("context_runs", "agent_runs", "task_loops", "project_goal_loops"),
        "events": count("run_events", "audit_events", "control_sync_events", "task_loop_events"),
    }


def _domain_health(
    settings: Settings,
    *,
    database_name: str,
    expected_revision: str,
    include_metrics: bool,
) -> dict[str, Any]:
    bound_settings = settings.model_copy(update={"db_name": database_name})
    engine = get_engine(bound_settings)
    try:
        try:
            database = check_database(bound_settings)
            database_payload = {
                "status": "OK",
                "name": database_name,
                "host": bound_settings.db_host,
                "port": bound_settings.db_port,
                "version": database.get("version"),
                "pgvector": database.get("pgvector") == "true",
            }
            db_status = "OK"
        except Exception:  # noqa: BLE001 - domain health reports ERROR instead of raising
            database_payload = {
                "status": "ERROR",
                "name": database_name,
                "host": bound_settings.db_host,
                "port": bound_settings.db_port,
                "version": None,
                "pgvector": None,
            }
            db_status = "ERROR"
        migration = _migration_status(bound_settings, engine, expected_revision) if db_status == "OK" else {
            "status": "UNKNOWN",
            "current_revision": None,
            "expected_revision": expected_revision,
            "expected_head": expected_revision,
            "current_heads": [],
            "expected_heads": [expected_revision],
            "excluded_revision": bound_settings.excluded_revision,
            "excluded_revision_applied": False,
        }
        ready = db_status == "OK" and migration.get("status") == "OK"
        current_metrics = {"projects": 0, "memories": 0, "tasks": 0, "runs": 0, "events": 0}
        if include_metrics and ready:
            with Session(engine) as session:
                current_metrics = metrics(session, engine)
        return {
            "readiness": "READY" if ready else "BLOCKED",
            "database": database_payload,
            "migration": migration,
            "metrics": current_metrics,
        }
    finally:
        engine.dispose()


def health(settings: Settings) -> dict[str, Any]:
    operational = _domain_health(
        settings,
        database_name=settings.operational_database_name,
        expected_revision=settings.operational_revision,
        include_metrics=True,
    )
    corpus = _domain_health(
        settings,
        database_name=settings.corpus_database_name,
        expected_revision=settings.corpus_revision,
        include_metrics=False,
    )
    domain_statuses = (operational["readiness"], corpus["readiness"])
    if all(status == "READY" for status in domain_statuses):
        overall = "OK"
    elif any(status == "BLOCKED" for status in domain_statuses):
        overall = "ERROR"
    else:
        overall = "WARNING"
    op_database = operational["database"]
    op_migration = operational["migration"]
    corpus_database = corpus["database"]
    corpus_migration = corpus["migration"]
    checks: list[dict[str, str]] = [
        {"name": "PADOS runtime", "status": "OK", "detail": settings.runtime_mode},
        {"name": "Operational database connectivity", "status": op_database["status"], "detail": op_database["name"]},
        {"name": "Operational migration status", "status": op_migration["status"], "detail": str(op_migration.get("current_revision"))},
        {"name": "Corpus database connectivity", "status": corpus_database["status"], "detail": corpus_database["name"]},
        {"name": "Corpus migration status", "status": corpus_migration["status"], "detail": str(corpus_migration.get("current_revision"))},
        {"name": "Operational pgvector", "status": "OK" if op_database.get("pgvector") is True else "ERROR" if op_database["status"] == "OK" else "UNKNOWN"},
        {"name": "Corpus pgvector", "status": "OK" if corpus_database.get("pgvector") is True else "ERROR" if corpus_database["status"] == "OK" else "UNKNOWN"},
        {"name": "CLI health", "status": "OK" if overall == "OK" else "WARNING"},
    ]
    return {
        "status": overall,
        "version": __version__,
        "web_version": WEB_VERSION,
        "environment": settings.env,
        "database": op_database,
        "migration": op_migration,
        "metrics": operational["metrics"],
        "checks": checks,
        "operational": operational,
        "corpus": corpus,
        "runtime_contract": {
            "operational_database": settings.operational_database_name,
            "corpus_database": settings.corpus_database_name,
            "operational_revision": settings.operational_revision,
            "corpus_revision": settings.corpus_revision,
            "excluded_revision": settings.excluded_revision,
            "excluded_revision_applied": bool(
                op_migration.get("excluded_revision_applied") or corpus_migration.get("excluded_revision_applied")
            ),
        },
    }


def system_info(settings: Settings) -> dict[str, Any]:
    root = Path.cwd().resolve()
    usage = shutil.disk_usage(str(root))
    health_payload = health(settings)
    migration = health_payload["migration"]
    return {
        "status": "ok",
        "pados_version": __version__,
        "web_version": WEB_VERSION,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "executable": sys.executable,
        "repository_path": str(root),
        "active_database": settings.db_name,
        "database_host": settings.db_host,
        "database_port": settings.db_port,
        "migration": migration,
        "pgvector": health_payload["database"].get("pgvector"),
        "environment_readiness": "OK",
        "configured_providers": {
            "chat": settings.chat_provider,
            "embedding": settings.embed_provider,
        },
        "local_model_availability": "UNKNOWN",
        "disk_usage": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        },
        "logs_location": str(root / "runtime" / "logs"),
        "data_directory": str(root / "runtime"),
    }


def list_projects(session: Session, engine: Engine, *, query: str, page: int, page_size: int) -> dict[str, Any]:
    rows = [safe_row(row) for row in control_service.list_projects(session)]
    query_lower = query.strip().casefold()
    if query_lower:
        rows = [row for row in rows if query_lower in str(row.get("name", "")).casefold()]
    total = len(rows)
    memory_counts = _project_counts(session, engine, ("memory_records",))
    task_counts = _project_counts(session, engine, ("tasks", "project_tasks", "task_goals"))
    run_counts = _project_counts(session, engine, ("context_runs", "agent_runs", "task_loops", "project_goal_loops"))
    start = (page - 1) * page_size
    items = []
    for row in rows[start : start + page_size]:
        project_id = str(row["id"])
        row.update(
            {
                "memory_count": memory_counts.get(project_id, 0),
                "task_count": task_counts.get(project_id, 0),
                "run_count": run_counts.get(project_id, 0),
            }
        )
        items.append(row)
    return {"status": "ok", "projects": items, "pagination": _pagination(page, page_size, total)}


def get_project(session: Session, engine: Engine, project_id: str) -> dict[str, Any]:
    project = safe_row(control_service.get_project(session, project_id))
    project.update(
        {
            "memory_count": _project_counts(session, engine, ("memory_records",)).get(project_id, 0),
            "task_count": _project_counts(session, engine, ("tasks", "project_tasks", "task_goals")).get(project_id, 0),
            "run_count": _project_counts(session, engine, ("context_runs", "agent_runs", "task_loops", "project_goal_loops")).get(project_id, 0),
        }
    )
    return {"status": "ok", "project": project}


def _memory_item(candidate: Any) -> dict[str, Any]:
    return {
        key: _safe_value(getattr(candidate, key), field_name=key)
        for key in (
            "memory_id",
            "project_id",
            "memory_type",
            "title",
            "content_summary",
            "source_type",
            "source_uri",
            "source_hash",
            "source_trust_level",
            "verification_status",
            "freshness_status",
            "importance",
            "retrieval_priority",
            "estimated_tokens",
            "score",
            "retrieval_reason",
            "authority_event_id",
            "authority_state",
            "authority_version",
            "context_eligible",
            "origin_type",
            "origin_actor_type",
            "origin_actor_id",
            "actor_type",
            "actor_id",
            "promotion_gate",
        )
    }


def list_memories(
    session: Session,
    engine: Engine,
    *,
    project_id: str | None,
    query: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if query.strip():
        request = RetrievalRequest(
            project_id=project_id or _ZERO_PROJECT,
            query=query,
            purpose="web-memory-search",
            max_tokens=4000,
            include_cross_project=project_id is None,
        )
        candidates = [_memory_item(item) for item in search_memory(session, request)]
        total = len(candidates)
        start = (page - 1) * page_size
        return {
            "status": "ok",
            "memories": candidates[start : start + page_size],
            "pagination": _pagination(page, page_size, total),
        }

    table = _reflect_table(engine, PUBLIC_SCHEMA, "memory_records")
    statement = text(
        """
        SELECT mr.*,
               authority.id AS authority_event_id,
               COALESCE(authority.authority_state, 'LEGACY_UNKNOWN') AS authority_state,
               authority.version AS authority_version,
               CASE WHEN authority.authority_state IN ('PROMOTED', 'HUMAN_CONFIRMED')
                    THEN TRUE ELSE FALSE END AS context_eligible,
               COALESCE(authority.origin_type, 'UNKNOWN') AS origin_type,
               authority.origin_actor_type, authority.origin_actor_id,
               authority.actor_type, authority.actor_id, authority.promotion_gate
        FROM memory_records mr
        LEFT JOIN LATERAL (
            SELECT mae.id, mae.authority_state, mae.version, mae.origin_type,
                   mae.origin_actor_type, mae.origin_actor_id, mae.actor_type,
                   mae.actor_id, mae.promotion_gate
            FROM memory_authority_events mae
            WHERE mae.memory_id = mr.id
            ORDER BY mae.version DESC
            LIMIT 1
        ) authority ON TRUE
        WHERE (CAST(:project_id AS uuid) IS NULL OR mr.project_id = CAST(:project_id AS uuid))
        ORDER BY mr.updated_at DESC
        LIMIT :page_size OFFSET :offset
        """
    )
    with_count = select(func.count()).select_from(table)
    if project_id:
        with_count = with_count.where(table.c.project_id == project_id)
    rows = session.execute(
        statement,
        {"project_id": project_id, "page_size": page_size, "offset": (page - 1) * page_size},
    ).mappings().all()
    total = int(session.execute(with_count).scalar_one())
    memories = []
    for row in rows:
        value = safe_row(row)
        summary = str(value.get("content_summary") or "")
        title = str(value.get("title") or "")
        memories.append(
            {
                "memory_id": str(value.get("id")),
                "project_id": str(value.get("project_id")),
                "memory_type": str(value.get("memory_type")),
                "title": title,
                "content_summary": summary,
                "source_type": str(value.get("source_type")),
                "source_uri": str(value.get("source_uri")),
                "source_hash": value.get("source_hash"),
                "source_trust_level": str(value.get("source_trust_level")),
                "verification_status": str(value.get("verification_status")),
                "freshness_status": str(value.get("freshness_status")),
                "importance": int(value.get("importance") or 0),
                "retrieval_priority": int(value.get("retrieval_priority") or 0),
                "estimated_tokens": max(1, (len(summary) + len(title) + 3) // 4),
                "score": 0,
                "retrieval_reason": "recent",
                "authority_event_id": value.get("authority_event_id"),
                "authority_state": str(value.get("authority_state") or "LEGACY_UNKNOWN"),
                "authority_version": value.get("authority_version"),
                "context_eligible": bool(value.get("context_eligible")),
                "origin_type": str(value.get("origin_type") or "UNKNOWN"),
                "origin_actor_type": value.get("origin_actor_type"),
                "origin_actor_id": value.get("origin_actor_id"),
                "actor_type": value.get("actor_type"),
                "actor_id": value.get("actor_id"),
                "promotion_gate": value.get("promotion_gate"),
            }
        )
    return {"status": "ok", "memories": memories, "pagination": _pagination(page, page_size, total)}


def get_memory(session: Session, engine: Engine, memory_id: str) -> dict[str, Any]:
    _reflect_table(engine, PUBLIC_SCHEMA, "memory_records")
    row = session.execute(
        text(
            """
            SELECT mr.*,
                   authority.id AS authority_event_id,
                   COALESCE(authority.authority_state, 'LEGACY_UNKNOWN') AS authority_state,
                   authority.version AS authority_version,
                   CASE WHEN authority.authority_state IN ('PROMOTED', 'HUMAN_CONFIRMED')
                        THEN TRUE ELSE FALSE END AS context_eligible,
                   COALESCE(authority.origin_type, 'UNKNOWN') AS origin_type,
                   authority.origin_actor_type, authority.origin_actor_id,
                   authority.actor_type, authority.actor_id, authority.promotion_gate
            FROM memory_records mr
            LEFT JOIN LATERAL (
                SELECT mae.id, mae.authority_state, mae.version, mae.origin_type,
                       mae.origin_actor_type, mae.origin_actor_id, mae.actor_type,
                       mae.actor_id, mae.promotion_gate
                FROM memory_authority_events mae
                WHERE mae.memory_id = mr.id
                ORDER BY mae.version DESC
                LIMIT 1
            ) authority ON TRUE
            WHERE mr.id = CAST(:memory_id AS uuid)
            """
        ),
        {"memory_id": memory_id},
    ).mappings().first()
    if row is None:
        raise LookupError("memory not found")
    return {"status": "ok", "memory": safe_row(row)}


def _table_metadata(engine: Engine, schema: str, table_name: str) -> dict[str, Any]:
    _reflect_table(engine, schema, table_name)
    inspector = inspect(engine)
    pk = set(inspector.get_pk_constraint(table_name, schema=schema).get("constrained_columns") or [])
    foreign_keys = inspector.get_foreign_keys(table_name, schema=schema)
    fk_columns = {column for fk in foreign_keys for column in fk.get("constrained_columns", [])}
    indexed = {column for index in inspector.get_indexes(table_name, schema=schema) for column in index.get("column_names", [])}
    columns = []
    for column in inspector.get_columns(table_name, schema=schema):
        columns.append(
            {
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": bool(column.get("nullable", True)),
                "primary_key": column["name"] in pk,
                "foreign_key": column["name"] in fk_columns,
                "indexed": column["name"] in indexed,
            }
        )
    return {
        "schema": schema,
        "name": table_name,
        "columns": columns,
        "primary_keys": sorted(pk),
        "foreign_keys": foreign_keys,
        "indexes": inspector.get_indexes(table_name, schema=schema),
    }


def database_tables(engine: Engine) -> list[dict[str, Any]]:
    inspector = inspect(engine)
    tables = []
    with engine.connect() as connection:
        for name in sorted(inspector.get_table_names(schema=PUBLIC_SCHEMA)):
            estimate = connection.execute(
                text(
                    "SELECT GREATEST(COALESCE(c.reltuples, 0), 0)::bigint "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname = :table"
                ),
                {"schema": PUBLIC_SCHEMA, "table": name},
            ).scalar_one_or_none()
            tables.append(
                {
                    "schema": PUBLIC_SCHEMA,
                    "name": name,
                    "kind": "table",
                    "estimated_row_count": int(estimate or 0),
                }
            )
        for name in sorted(inspector.get_view_names(schema=PUBLIC_SCHEMA)):
            tables.append({"schema": PUBLIC_SCHEMA, "name": name, "kind": "view", "estimated_row_count": None})
    return tables


def database_overview(settings: Settings) -> dict[str, Any]:
    engine = get_engine(settings)
    try:
        with Session(engine) as session:
            current_metrics = metrics(session, engine)
        tables = database_tables(engine)
        names = {item["name"] for item in tables if item["kind"] == "table"}
        data_map_candidates = [
            ("Project", ("projects",)),
            ("Memory", ("memory_records",)),
            ("Task", ("tasks", "project_tasks", "task_goals")),
            ("Run", ("context_runs", "agent_runs", "task_loops", "project_goal_loops")),
            ("Event", ("run_events", "audit_events", "control_sync_events", "task_loop_events")),
            ("Artifact", ("memory_artifacts", "artifacts", "worker_job_artifacts")),
            ("Relationship", ("memory_relationships", "task_dependencies", "project_task_dependencies")),
        ]
        data_map = [
            {"entity": entity, "tables": [name for name in candidates if name in names]}
            for entity, candidates in data_map_candidates
            if any(name in names for name in candidates)
        ]
        return {
            "status": "ok",
            "database": settings.db_name,
            "schema": PUBLIC_SCHEMA,
            "postgresql_version": check_database(settings).get("version"),
            "pgvector": check_database(settings).get("pgvector") == "true",
            "migration": _migration_status(settings, engine, _expected_revision_for_database(settings)),
            "metrics": current_metrics,
            "tables": tables,
            "schemas": [{"name": PUBLIC_SCHEMA, "kind": "application"}],
            "data_map": data_map,
        }
    finally:
        engine.dispose()


def table_rows(
    session: Session,
    engine: Engine,
    *,
    schema: str,
    table_name: str,
    page: int,
    page_size: int,
    query: str,
    filter_column: str | None,
    filter_value: str | None,
    sort: str | None,
    descending: bool,
    exact_count: bool,
) -> dict[str, Any]:
    table = _reflect_table(engine, schema, table_name)
    statement = select(table)
    if query.strip():
        text_columns = [column for column in table.columns if "CHAR" in str(column.type).upper() or "TEXT" in str(column.type).upper()]
        if text_columns:
            statement = statement.where(or_(*(column.ilike(f"%{query.strip()[:200]}%") for column in text_columns)))
    if filter_column:
        _validate_identifier(filter_column, "filter column")
        if filter_column not in table.c:
            raise UnknownTableError("filter column is not available")
        statement = statement.where(table.c[filter_column].ilike(f"%{(filter_value or '')[:200]}%"))
    if sort:
        _validate_identifier(sort, "sort column")
        if sort not in table.c:
            raise UnknownTableError("sort column is not available")
        statement = statement.order_by(table.c[sort].desc() if descending else table.c[sort].asc())
    statement = statement.limit(page_size).offset((page - 1) * page_size)
    rows = [safe_row(row) for row in session.execute(statement).mappings().all()]
    count_statement = select(func.count()).select_from(table)
    if query.strip() or filter_column:
        filtered = statement.limit(None).offset(None)
        # Rebuild a count from the validated SELECT's WHERE criteria without exposing SQL text.
        where_criteria = list(filtered._where_criteria)
        if where_criteria:
            count_statement = count_statement.where(*where_criteria)
    total = int(session.execute(count_statement).scalar_one()) if exact_count or page == 1 else max(len(rows), page * page_size)
    return {
        "status": "ok",
        "table": _table_metadata(engine, schema, table_name),
        "rows": rows,
        "pagination": _pagination(page, page_size, total),
        "exact_count": exact_count,
    }


def relationships(engine: Engine) -> list[dict[str, Any]]:
    inspector = inspect(engine)
    result = []
    for table_name in sorted(inspector.get_table_names(schema=PUBLIC_SCHEMA)):
        for fk in inspector.get_foreign_keys(table_name, schema=PUBLIC_SCHEMA):
            result.append(
                {
                    "from_table": table_name,
                    "from_columns": fk.get("constrained_columns", []),
                    "to_table": fk.get("referred_table"),
                    "to_schema": fk.get("referred_schema") or PUBLIC_SCHEMA,
                    "to_columns": fk.get("referred_columns", []),
                }
            )
    return result


def _unified_rows(engine: Engine, table_names: tuple[str, ...], *, source: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    available = _public_tables(engine)
    with Session(engine) as session:
        for table_name in table_names:
            if table_name not in available:
                continue
            table = _reflect_table(engine, PUBLIC_SCHEMA, table_name)
            timestamp_column = next((table.c[name] for name in ("event_time", "created_at", "queued_at", "started_at", "updated_at") if name in table.c), None)
            statement = select(table)
            if timestamp_column is not None:
                statement = statement.order_by(timestamp_column.desc())
            statement = statement.limit(100)
            for row in session.execute(statement).mappings().all():
                value = safe_row(row)
                result.append(
                    {
                        "id": str(value.get("id") or value.get("sequence_number") or ""),
                        "project_id": value.get("project_id"),
                        "status": value.get("status") or value.get("level") or "UNKNOWN",
                        "name": value.get("event_type") or value.get("run_type") or value.get("command") or table_name,
                        "timestamp": value.get("event_time") or value.get("created_at") or value.get("queued_at") or value.get("started_at") or value.get("updated_at"),
                        "source": source or table_name,
                        "payload": value,
                    }
                )
    result.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return result[:100]


def list_runs(engine: Engine, *, page: int, page_size: int) -> dict[str, Any]:
    rows = _unified_rows(engine, ("context_runs", "agent_runs", "task_loops", "project_goal_loops"), source="run")
    start = (page - 1) * page_size
    return {"status": "ok", "runs": rows[start : start + page_size], "pagination": _pagination(page, page_size, len(rows))}


def list_events(engine: Engine, *, page: int, page_size: int) -> dict[str, Any]:
    rows = _unified_rows(engine, ("run_events", "audit_events", "control_sync_events", "task_loop_events"), source="event")
    start = (page - 1) * page_size
    return {"status": "ok", "events": rows[start : start + page_size], "pagination": _pagination(page, page_size, len(rows))}
