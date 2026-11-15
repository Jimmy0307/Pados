# SPDX-License-Identifier: AGPL-3.0-only
"""The Resources (資源與儲存) summary: RAM, model memory, storage, execution growth, running cost, budget and a cleanup
plan preview. READ ONLY and PROJECTION ONLY: it measures, it never writes, deletes, schedules or authorizes.

Storage comes from the existing accounting (``pados.lifecycle.accounting.build_locations`` / ``account_storage``, sizes
and modification times only, bounded no-follow walks) plus one extra measured location, the worker job artifact root
(evidence: measured, always protected). The budget and its warning thresholds come from the existing storage policy
(``pados.lifecycle.storage_policy``, file named by ``PADOS_STORAGE_POLICY``) and the existing pressure rule
(``pados.lifecycle.storage_flexibility.pressure_of``). Defaults when no policy is configured: no local byte budget
(``local_budget_bytes`` unset, so the budget is NOT_CONFIGURED), disk warning at 15 % free (``pressured_free_fraction``
0.15) and critical at 5 % free (``critical_free_fraction`` 0.05); with a budget, the same fractions apply to its headroom
(warning at 85 % used, critical at 95 % used).

Trends: execution-record growth is computed from the rows' own timestamps. Storage byte trends need size samples over
time, which PADOS does not record (no new table by design), so ``trends.storage_bytes`` says HISTORY_NOT_RECORDED.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pados.config import artifact_root
from pados.control import cleanup_plan
from pados.control import resource_metrics as rm
from pados.lifecycle.accounting import (
    LocationStatus,
    LocationUsage,
    StorageLocation,
    account_storage,
    build_locations,
)
from pados.lifecycle.retention import DataKind, StorageCategory
from pados.lifecycle.storage_flexibility import (
    StorageObservation,
    pressure_advice,
    pressure_of,
)
from pados.lifecycle.storage_policy import StoragePolicy, StoragePolicyError, load_storage_policy

SCHEMA = "pados.control.resources.v1"
SessionFactory = Callable[[Any], AbstractContextManager[Session]]
DiskProbe = Callable[[Path], tuple[int, int]]  # (total, free) bytes of the volume holding the path

# Filesystem metrics shown on the page: metric id -> accounting location ids it sums.
FILESYSTEM_METRICS: dict[str, tuple[str, ...]] = {
    "project_documents": ("product_workspace",),
    "cache_size": ("runtime_cache",),
    "evidence_files": ("job_artifacts", "product_objects", "release_test_evidence"),
    "operational_logs": ("runtime_logs", "compute_node_state", "connectivity_snapshot"),
}


def runtime_root(settings: Any) -> Path:
    """The runtime directory: the parent of the configured artifact root (``runtime/artifacts`` by default)."""
    return artifact_root(settings).parent


def storage_locations(settings: Any) -> tuple[StorageLocation, ...]:
    root = artifact_root(settings)
    jobs = StorageLocation("job_artifacts", StorageCategory.ARTIFACTS, (DataKind.PROJECT_EVIDENCE,), root,
                           LocationStatus.MEASURED if root.exists() else LocationStatus.MISSING)
    return build_locations(settings, runtime_root=runtime_root(settings), extra=(jobs,))


def load_policy(settings: Any) -> tuple[StoragePolicy, str]:
    path = str(getattr(settings, "storage_policy_path", "") or "")
    if not path:
        return StoragePolicy(), "DEFAULTS"
    try:
        return load_storage_policy(path), "CONFIGURED"
    except StoragePolicyError as exc:
        return StoragePolicy(), f"POLICY_INVALID:{str(exc)[:80]}"


def _distinct_bytes(locations: Sequence[StorageLocation], usages: Sequence[LocationUsage]) -> int:
    """Bytes of measured locations, counting a location nested inside another measured one only once."""
    roots = {loc.location_id: loc.root.resolve() for loc in locations if loc.root is not None}
    measured = [u for u in usages if u.status is LocationStatus.MEASURED and u.location_id in roots]
    total = 0
    for usage in measured:
        mine = roots[usage.location_id]
        nested = any(other.location_id != usage.location_id and roots[other.location_id] in mine.parents
                     for other in measured)
        total += 0 if nested else usage.bytes
    return total


def filesystem_metrics(usages: Sequence[LocationUsage], now: datetime) -> list[rm.Metric]:
    by_id = {usage.location_id: usage for usage in usages}
    metrics: list[rm.Metric] = []
    for metric_id, location_ids in FILESYSTEM_METRICS.items():
        source = "pados.lifecycle.accounting (file sizes, no-follow walk): " + ", ".join(location_ids)
        found = [by_id[i] for i in location_ids if i in by_id and by_id[i].status is LocationStatus.MEASURED]
        if not found:
            states = {i: (by_id[i].status.value if i in by_id else "NOT_CONFIGURED") for i in location_ids}
            metrics.append(rm.unknown(metric_id, "bytes", source, "LOCATION_NOT_MEASURED", {"locations": states}))
            continue
        metrics.append(rm.measured(metric_id, sum(u.bytes for u in found), "bytes", source, now, detail={
            "files": sum(u.file_count for u in found), "truncated": any(u.truncated for u in found),
            "locations": [{"id": u.location_id, "bytes": u.bytes, "files": u.file_count,
                           "newest_mtime": u.newest_mtime} for u in found]}))
    return metrics


def _usage(path: Path) -> tuple[int, int]:
    existing = next((candidate for candidate in (path, *path.parents) if candidate.exists()), path)
    usage = shutil.disk_usage(existing)
    return usage.total, usage.free


def _disk(path: Path, probe: DiskProbe | None) -> StorageObservation | None:
    try:
        total, free = (probe or _usage)(path)
        return StorageObservation(total_bytes=total, free_bytes=free)
    except (OSError, ValueError):
        return None


def budget_status(policy: StoragePolicy, policy_status: str, disk: StorageObservation | None,
                  local_bytes: int | None, now: datetime) -> dict[str, Any]:
    budget = policy.budget
    pressure = pressure_of(disk, budget, local_bytes)
    configured = budget.local_budget_bytes is not None
    used_fraction = (round(local_bytes / budget.local_budget_bytes, 4)
                     if configured and local_bytes is not None and budget.local_budget_bytes else None)
    return {
        "source": "pados.lifecycle.storage_policy (PADOS_STORAGE_POLICY) + storage_flexibility.pressure_of",
        "policy_status": policy_status,
        "measured_at": now.isoformat(),
        "budget_configured": configured,
        "local_budget_bytes": budget.local_budget_bytes,
        "local_bytes": local_bytes,
        "used_fraction": used_fraction,
        "warning_at_used_fraction": round(1 - budget.pressured_free_fraction, 4),
        "critical_at_used_fraction": round(1 - budget.critical_free_fraction, 4),
        "min_free_bytes": budget.min_free_bytes,
        "disk": {"measured": disk is not None, "total_bytes": disk.total_bytes if disk else None,
                 "free_bytes": disk.free_bytes if disk else None,
                 "warning_at_free_fraction": budget.pressured_free_fraction,
                 "critical_at_free_fraction": budget.critical_free_fraction},
        "pressure": pressure.value,
        "pressure_basis": "DISK_UNKNOWN_FAILS_SAFE_TO_PRESSURED" if disk is None else "MEASURED",
        "advice": [advice.value for advice in pressure_advice(pressure)],
        "recommendation_only": True,
    }


def _database_section(settings: Any, session_scope: SessionFactory, now: datetime) -> list[rm.Metric]:
    try:
        with session_scope(settings) as session:
            return [*rm.database_metrics(session, settings, now), rm.execution_growth_metric(session, now),
                    *rm.usage_metrics(session, now)]
    except (SQLAlchemyError, OSError) as exc:
        reason = f"DB_NOT_AVAILABLE:{type(exc).__name__}"
        return [rm.unknown("database_size", "bytes", "PostgreSQL pg_database_size", reason),
                rm.unknown("database_index_size", "bytes", "PostgreSQL pg_indexes_size", reason),
                rm.unknown("execution_records", "rows", "row timestamps", reason),
                rm.unknown("token_usage", "tokens", "worker_jobs.usage_metadata", reason),
                rm.unknown("model_execution_time", "seconds", "worker_jobs.usage_metadata", reason),
                rm.unknown("job_execution_time", "seconds", "worker_jobs timestamps", reason)]


def resources_summary(settings: Any, session_scope: SessionFactory, *, now: datetime | None = None,
                      fetch_factory: rm.FetchFactory | None = None, disk_probe: DiskProbe | None = None,
                      host_memory: rm.MemoryProbe | None = None, process_memory: rm.ProcessProbe | None = None,
                      ) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    policy, policy_status = load_policy(settings)
    locations = storage_locations(settings)
    report = account_storage(locations, policy=policy.retention.policy(), now=moment)
    usages = list(report.locations)
    cleanup = cleanup_plan.plan_cleanup(locations, usages)
    database = _database_section(settings, session_scope, moment)
    by_id = {metric.metric_id: metric for metric in database}
    files_bytes = _distinct_bytes(locations, usages)
    db_bytes = by_id["database_size"].value if by_id["database_size"].measured else None
    local_bytes = files_bytes + int(db_bytes) if db_bytes is not None else None
    reclaimable = rm.measured("reclaimable_estimate", cleanup["estimated_reclaimable_bytes"], "bytes",
                              "cleanup plan preview over pados.lifecycle.accounting eligible_for_purge", moment,
                              estimate=True, detail={"candidates": len(cleanup["candidates"])})
    metrics = [*rm.ram_metrics(moment, host=host_memory, process=process_memory),
               rm.model_memory_metric(settings, moment, fetch_factory=fetch_factory),
               *database, *filesystem_metrics(usages, moment), reclaimable]
    growth = by_id["execution_records"]
    return {
        "schema": SCHEMA,
        "authority": "PROJECTION_ONLY",
        "generated_at": moment.isoformat(),
        "metrics": [metric.to_dict() for metric in metrics],
        "budget": budget_status(policy, policy_status, _disk(runtime_root(settings), disk_probe),
                                local_bytes, moment),
        "trends": {
            "execution_records": {"status": "MEASURED" if growth.measured else "UNKNOWN",
                                  "source": growth.source, "reason": growth.reason,
                                  "series": growth.detail.get("series", []),
                                  "rows_per_day_last_7_days": growth.detail.get("rows_per_day_last_7_days")},
            "storage_bytes": {"status": "UNKNOWN", "reason": "HISTORY_NOT_RECORDED",
                              "detail": "PADOS records no storage size samples over time (no new table by design); "
                                        "only the current size is measured."},
        },
        "storage": {"filesystem_bytes_distinct": files_bytes, "database_bytes": db_bytes,
                    "local_bytes": local_bytes, "categories": report.to_dict()["categories"]},
        "cleanup_plan": cleanup,
    }
