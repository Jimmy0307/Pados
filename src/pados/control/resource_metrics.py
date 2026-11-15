# SPDX-License-Identifier: AGPL-3.0-only
"""Collectors for the Control Center's 資源與儲存 (Resources) page. READ ONLY, PROJECTION ONLY.

Every metric states its ``source``, its ``unit``, when it was measured (``measured_at``) and whether it was measured at
all (``measured``). A number PADOS could not measure is ``UNKNOWN`` with a closed ``reason`` code and no value; an
estimate is flagged ``estimate=true``. Nothing here writes, deletes, schedules or authorizes anything.

Sources reused, not duplicated:

- RAM: ``psutil`` (already a declared runtime dependency) for the host and this PADOS process;
- model resident memory: the runtime's own ``/api/ps`` report, fetched with the local-intelligence loopback fetch
  (``pados.local_intelligence.runtimes.loopback_fetch``) and validated as ``LoadedModel``. It is called ONLY when an
  Ollama provider is configured (``PADOS_CHAT_PROVIDER`` / ``PADOS_FAST_PROVIDER`` / ``PADOS_EXECUTOR_PROVIDER`` /
  ``PADOS_EMBED_PROVIDER`` = ``ollama``) and the configured endpoint is loopback; otherwise UNKNOWN, no call is made;
- PostgreSQL: ``pg_database_size`` / ``pg_indexes_size`` (catalog reads only);
- execution growth, token/context usage and model execution time: the existing rows' own timestamps and the measured
  execution evidence the worker already records in ``worker_jobs.usage_metadata`` (no new table, no new samples).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pados.local_intelligence.runtimes import (
    FetchError,
    LoadedModel,
    loopback_endpoint,
    loopback_fetch,
    probe_id_path,
)

OLLAMA = "ollama"
PROVIDER_FIELDS = ("chat_provider", "fast_provider", "executor_provider", "embed_provider")
TREND_DAYS = 14
USAGE_WINDOW_DAYS = 30
MAX_MODELS_SHOWN = 20

# Execution records whose growth is shown: (table, timestamp column). Fixed identifiers, never caller input.
EXECUTION_TABLES: tuple[tuple[str, str], ...] = (
    ("worker_jobs", "queued_at"),
    ("task_loop_iterations", "started_at"),
    ("worker_job_events", "event_time"),
    ("worker_job_artifacts", "created_at"),
    ("control_sync_events", "event_time"),
)


@dataclass(frozen=True)
class Metric:
    metric_id: str
    unit: str
    source: str
    value: float | int | None = None
    measured: bool = False
    measured_at: str | None = None
    reason: str | None = None
    estimate: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.metric_id, "value": self.value, "unit": self.unit, "source": self.source,
                "measured": self.measured, "measured_at": self.measured_at,
                "status": "MEASURED" if self.measured else "UNKNOWN", "reason": self.reason,
                "estimate": self.estimate, "detail": dict(self.detail)}


def measured(metric_id: str, value: float | int, unit: str, source: str, now: datetime, *,
             detail: Mapping[str, Any] | None = None, estimate: bool = False) -> Metric:
    return Metric(metric_id, unit, source, value, True, now.isoformat(), None, estimate, dict(detail or {}))


def unknown(metric_id: str, unit: str, source: str, reason: str,
            detail: Mapping[str, Any] | None = None) -> Metric:
    return Metric(metric_id, unit, source, None, False, None, reason, False, dict(detail or {}))


# --- RAM ----------------------------------------------------------------------------------------------------------------
MemoryProbe = Callable[[], tuple[int, int, int]]  # (used, total, available) bytes of the host
ProcessProbe = Callable[[], int]  # resident bytes of this process


def _psutil_memory() -> tuple[int, int, int]:
    import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

    memory = psutil.virtual_memory()
    return int(memory.total - memory.available), int(memory.total), int(memory.available)


def _psutil_process_rss() -> int:
    import psutil

    return int(psutil.Process().memory_info().rss)


def ram_metrics(now: datetime, *, host: MemoryProbe | None = None,
                process: ProcessProbe | None = None) -> list[Metric]:
    source = "psutil.virtual_memory (OS)"
    try:
        used, total, available = (host or _psutil_memory)()
        ram = measured("ram_in_use", used, "bytes", source, now,
                       detail={"total_bytes": total, "available_bytes": available,
                               "used_fraction": round(used / total, 4) if total else None})
    except Exception as exc:  # noqa: BLE001 - any probe failure is one closed UNKNOWN, never a crash
        ram = unknown("ram_in_use", "bytes", source, f"RAM_PROBE_FAILED:{type(exc).__name__}")
    source = "psutil.Process.memory_info (this pados serve process)"
    try:
        rss = measured("pados_process_rss", (process or _psutil_process_rss)(), "bytes", source, now)
    except Exception as exc:  # noqa: BLE001
        rss = unknown("pados_process_rss", "bytes", source, f"PROCESS_PROBE_FAILED:{type(exc).__name__}")
    return [ram, rss]


# --- model resident memory ----------------------------------------------------------------------------------------------
Fetch = Callable[[str], bytes]
FetchFactory = Callable[[str, int], Fetch]


def configured_ollama_providers(settings: Any) -> list[str]:
    return [name for name in PROVIDER_FIELDS if str(getattr(settings, name, "") or "").strip().lower() == OLLAMA]


def _loaded_models(body: bytes) -> list[LoadedModel]:
    payload = json.loads(body)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("MODELS_NOT_LIST")
    loaded: list[LoadedModel] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        try:
            loaded.append(LoadedModel(
                name=str(item.get("name")),
                size_bytes=item.get("size") if isinstance(item.get("size"), int) else None,
                size_vram_bytes=item.get("size_vram") if isinstance(item.get("size_vram"), int) else None,
                context_length=item.get("context_length") or None if isinstance(item.get("context_length"), int)
                else None))
        except ValidationError:
            continue
    return loaded


def model_memory_metric(settings: Any, now: datetime, *, fetch_factory: FetchFactory | None = None) -> Metric:
    """What the configured local model runtime reports as resident (``/api/ps``); UNKNOWN when nothing is configured."""
    source = "Ollama /api/ps (configured local runtime)"
    providers = configured_ollama_providers(settings)
    if not providers:
        return unknown("model_resident_memory", "bytes", source, "MODEL_RUNTIME_NOT_CONFIGURED")
    endpoint = loopback_endpoint(str(getattr(settings, "ollama_base_url", "") or ""))
    if endpoint is None:
        return unknown("model_resident_memory", "bytes", source, "MODEL_RUNTIME_NOT_LOOPBACK",
                       {"providers": providers})
    fetch = (fetch_factory or loopback_fetch)(*endpoint)
    try:
        models = _loaded_models(fetch(probe_id_path("ollama.ps")))
    except FetchError as exc:
        return unknown("model_resident_memory", "bytes", source, f"MODEL_RUNTIME_{exc.code}", {"providers": providers})
    except (ValueError, UnicodeDecodeError):
        return unknown("model_resident_memory", "bytes", source, "MODEL_RUNTIME_RESPONSE_INVALID",
                       {"providers": providers})
    sizes = [m.size_bytes for m in models]
    if any(size is None for size in sizes):
        return unknown("model_resident_memory", "bytes", source, "MODEL_SIZE_NOT_REPORTED",
                       {"providers": providers, "loaded_models": len(models)})
    return measured("model_resident_memory", sum(s for s in sizes if s is not None), "bytes", source, now, detail={
        "providers": providers, "loaded_models": len(models),
        "vram_bytes": sum(m.size_vram_bytes or 0 for m in models),
        "models": [{"name": m.name, "size_bytes": m.size_bytes, "size_vram_bytes": m.size_vram_bytes,
                    "context_length": m.context_length} for m in models[:MAX_MODELS_SHOWN]]})


# --- PostgreSQL ---------------------------------------------------------------------------------------------------------
def database_names(settings: Any) -> list[str]:
    names = [str(getattr(settings, attr, "") or "") for attr in
             ("db_name", "operational_database_name", "corpus_database_name", "product_database_name")]
    return sorted({name for name in names if name})


def database_metrics(session: Session, settings: Any, now: datetime) -> list[Metric]:
    """Sizes of the PADOS databases on this server and of the current database's indexes (catalog reads only)."""
    source = "PostgreSQL pg_database_size"
    try:
        current = str(session.execute(text("SELECT current_database()")).scalar_one())
        rows = session.execute(text(
            "SELECT datname, pg_database_size(datname) AS bytes FROM pg_database "
            "WHERE datname = ANY(:names) AND has_database_privilege(datname, 'CONNECT') ORDER BY datname"),
            {"names": sorted({current, *database_names(settings)})}).mappings().all()
        indexes = int(session.execute(text(
            "SELECT COALESCE(sum(pg_indexes_size(c.oid)), 0) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'm') AND n.nspname = current_schema()")).scalar_one())
    except SQLAlchemyError as exc:
        reason = f"DB_NOT_AVAILABLE:{type(exc).__name__}"
        return [unknown("database_size", "bytes", source, reason),
                unknown("database_index_size", "bytes", "PostgreSQL pg_indexes_size", reason)]
    databases = [{"name": str(row["datname"]), "bytes": int(row["bytes"]), "current": row["datname"] == current}
                 for row in rows]
    return [
        measured("database_size", sum(d["bytes"] for d in databases), "bytes", source, now,
                 detail={"databases": databases}),
        measured("database_index_size", indexes, "bytes", "PostgreSQL pg_indexes_size (current database)", now,
                 detail={"database": current}),
    ]


# --- execution growth, tokens and execution time ------------------------------------------------------------------------
def _visible(session: Session, table: str) -> bool:
    return session.execute(text("SELECT to_regclass(CAST(:name AS text))"), {"name": table}).scalar() is not None


def daily_series(rows: Iterable[Mapping[str, Any]], now: datetime, days: int = TREND_DAYS) -> list[dict[str, Any]]:
    """Zero-filled per-day counts for the last ``days`` days (UTC), oldest first."""
    counts = {str(row["day"]): int(row["count"]) for row in rows}
    start = (now.astimezone(UTC) - timedelta(days=days - 1)).date()
    return [{"day": (start + timedelta(days=offset)).isoformat(),
             "count": counts.get((start + timedelta(days=offset)).isoformat(), 0)} for offset in range(days)]


def execution_growth_metric(session: Session, now: datetime) -> Metric:
    source = "row timestamps of worker_jobs, task_loop_iterations, worker_job_events, worker_job_artifacts, " \
             "control_sync_events"
    since = now - timedelta(days=TREND_DAYS - 1)
    tables: list[dict[str, Any]] = []
    total_rows = 0
    merged: dict[str, int] = {}
    try:
        for table, column in EXECUTION_TABLES:
            if not _visible(session, table):
                tables.append({"table": table, "status": "NOT_PRESENT"})
                continue
            total = int(session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
            rows = session.execute(text(
                f"SELECT to_char(date_trunc('day', {column} AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day, "  # noqa: S608
                f"count(*) AS count FROM {table} WHERE {column} >= :since GROUP BY 1"),
                {"since": since.replace(hour=0, minute=0, second=0, microsecond=0)}).mappings().all()
            series = daily_series([dict(row) for row in rows], now)
            for point in series:
                merged[point["day"]] = merged.get(point["day"], 0) + point["count"]
            total_rows += total
            tables.append({"table": table, "status": "MEASURED", "rows": total, "series": series})
    except SQLAlchemyError as exc:
        return unknown("execution_records", "rows", source, f"DB_NOT_AVAILABLE:{type(exc).__name__}")
    combined: list[dict[str, Any]] = [{"day": day, "count": count} for day, count in sorted(merged.items())]
    last7 = [count for _, count in sorted(merged.items())[-7:]]
    return measured("execution_records", total_rows, "rows", source, now, detail={
        "tables": tables, "series": combined, "trend_days": TREND_DAYS,
        "rows_per_day_last_7_days": round(sum(last7) / 7, 2) if last7 else 0})


_USAGE_SQL = """
WITH r AS (
  SELECT usage_metadata->'measured_execution'->'runtime' AS runtime FROM worker_jobs
  WHERE worker_type = 'OLLAMA' AND queued_at >= :since
)
SELECT count(*) AS jobs,
  count(*) FILTER (WHERE jsonb_typeof(runtime->'eval_count') = 'number') AS measured_jobs,
  COALESCE(sum(CASE WHEN jsonb_typeof(runtime->'prompt_eval_count') = 'number'
                    THEN (runtime->>'prompt_eval_count')::numeric END), 0) AS prompt_tokens,
  COALESCE(sum(CASE WHEN jsonb_typeof(runtime->'eval_count') = 'number'
                    THEN (runtime->>'eval_count')::numeric END), 0) AS output_tokens,
  count(*) FILTER (WHERE jsonb_typeof(runtime->'total_duration') = 'number') AS timed_jobs,
  COALESCE(sum(CASE WHEN jsonb_typeof(runtime->'total_duration') = 'number'
                    THEN (runtime->>'total_duration')::numeric END), 0) AS model_ns
FROM r
"""

_JOB_TIME_SQL = """
SELECT count(*) AS jobs, COALESCE(sum(EXTRACT(EPOCH FROM (completed_at - started_at))), 0) AS seconds
FROM worker_jobs WHERE started_at IS NOT NULL AND completed_at IS NOT NULL AND queued_at >= :since
"""


def usage_metrics(session: Session, now: datetime) -> list[Metric]:
    """Token/context usage and model time from measured execution evidence; job wall time from job timestamps."""
    since = now - timedelta(days=USAGE_WINDOW_DAYS)
    token_source = "worker_jobs.usage_metadata.measured_execution.runtime (prompt_eval_count, eval_count)"
    time_source = "worker_jobs.usage_metadata.measured_execution.runtime.total_duration"
    wall_source = "worker_jobs.started_at / completed_at"
    window = {"window_days": USAGE_WINDOW_DAYS}
    try:
        usage = session.execute(text(_USAGE_SQL), {"since": since}).mappings().one()
        wall = session.execute(text(_JOB_TIME_SQL), {"since": since}).mappings().one()
    except SQLAlchemyError as exc:
        reason = f"DB_NOT_AVAILABLE:{type(exc).__name__}"
        return [unknown("token_usage", "tokens", token_source, reason),
                unknown("model_execution_time", "seconds", time_source, reason),
                unknown("job_execution_time", "seconds", wall_source, reason)]
    model_jobs = int(usage["jobs"])
    if int(usage["measured_jobs"]):
        prompt, output = int(usage["prompt_tokens"]), int(usage["output_tokens"])
        tokens = measured("token_usage", prompt + output, "tokens", token_source, now, detail={
            **window, "prompt_tokens": prompt, "output_tokens": output, "measured_jobs": int(usage["measured_jobs"]),
            "model_jobs": model_jobs})
    else:
        tokens = unknown("token_usage", "tokens", token_source, "NO_MEASURED_MODEL_EXECUTION",
                         {**window, "model_jobs": model_jobs})
    if int(usage["timed_jobs"]):
        model_time = measured("model_execution_time", round(float(usage["model_ns"]) / 1e9, 3), "seconds",
                              time_source, now, detail={**window, "timed_jobs": int(usage["timed_jobs"])})
    else:
        model_time = unknown("model_execution_time", "seconds", time_source, "NO_MEASURED_MODEL_EXECUTION",
                             {**window, "model_jobs": model_jobs})
    jobs = int(wall["jobs"])
    if jobs:
        seconds = float(wall["seconds"])
        job_time = measured("job_execution_time", round(seconds, 3), "seconds", wall_source, now, detail={
            **window, "jobs": jobs, "average_seconds": round(seconds / jobs, 3)})
    else:
        job_time = unknown("job_execution_time", "seconds", wall_source, "NO_FINISHED_JOBS_IN_WINDOW", window)
    return [tokens, model_time, job_time]
