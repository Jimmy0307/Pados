# SPDX-License-Identifier: AGPL-3.0-only
"""Storage Flexibility V1+: placement reporting and eviction recommendations on top of Lifecycle accounting.

Read-only and recommendation-only, like ``pados.lifecycle.accounting``:

* ``placement_location`` describes the persisted placement index as an accounting ``StorageLocation`` for
  ``build_locations(extra=...)``. Its kind is R2, so accounting never estimates it as purge-eligible and
  ``scopes_from_locations`` treats it as a protected root.
* ``placement_report`` aggregates the index by tier, artifact class and backend: logical references, physical
  artifacts, bytes held locally and bytes only referenced, and the bytes deduplication saved. It names no
  file, reference or location.
* ``recommend_eviction`` wraps ``plan_eviction`` and annotates every candidate with its ``DataKind``, retention
  class, purge rule and the only route by which it could ever be removed: ``PLAN_PURGE_R0_R1_SCOPE`` (an
  operator-approved ``plan_purge`` / ``apply_purge`` over a deletable scope) or ``HUMAN_DECISION_ONLY``. The
  recommendation deletes nothing (``deletes=False``, ``authority="NONE"``).
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pados.lifecycle.accounting import LocationStatus, StorageLocation
from pados.lifecycle.retention import CATALOG, DataKind, PurgeRule, RetentionClass, StorageCategory
from pados.lifecycle.storage_flexibility import (
    ArtifactClass,
    PlacementIndex,
    StorageBudget,
    StoragePressure,
    plan_eviction,
)
from pados.lifecycle.storage_policy import StoragePolicy

PLACEMENT_REPORT_SCHEMA = "pados.lifecycle.storage-placement-report.v1"
PLACEMENT_LOCATION_ID = "storage_placement_index"


def placement_location(index_path: str | Path) -> StorageLocation:
    """The persisted placement index as an accounting location (single file, R2, never purge-eligible)."""
    path = Path(index_path).expanduser()
    return StorageLocation(PLACEMENT_LOCATION_ID, StorageCategory.AUTHORITY_METADATA,
                           (DataKind.STORAGE_PLACEMENT_INDEX,), path,
                           LocationStatus.MEASURED if path.exists() else LocationStatus.MISSING, single_file=True)


def _bucket(table: dict[str, dict[str, int]], key: str) -> dict[str, int]:
    return table.setdefault(key, {"artifacts": 0, "references": 0, "bytes": 0, "local_bytes": 0,
                                  "referenced_bytes": 0})


def placement_report(index: PlacementIndex, *, local_backends: frozenset[str],
                     policy: StoragePolicy | None = None) -> dict[str, Any]:
    """Counts and bytes per tier, class and backend; no names, references or locations leave this function."""
    active = policy or StoragePolicy()
    by_tier: dict[str, dict[str, int]] = {}
    by_class: dict[str, dict[str, int]] = {}
    by_backend: dict[str, dict[str, int]] = {}
    by_retention: dict[str, dict[str, int]] = {}
    logical = physical_local = referenced = saved = 0
    for entry in index.entries.values():
        local = entry.local_bytes(local_backends)
        refs = len(entry.references)
        logical += refs
        physical_local += local
        referenced += entry.size_bytes if not local else 0
        saved += entry.size_bytes * max(refs - 1, 0)
        rows = [_bucket(by_class, entry.artifact_class.value),
                _bucket(by_retention, active.retention_class(entry.artifact_class).value)]
        for row in rows:
            row["artifacts"] += 1
            row["references"] += refs
            row["bytes"] += entry.size_bytes
            row["local_bytes"] += local
            row["referenced_bytes"] += entry.size_bytes if not local else 0
        for placement in entry.placements:
            is_local = placement.backend_id in local_backends
            for row in (_bucket(by_tier, placement.tier.value), _bucket(by_backend, placement.backend_id)):
                row["artifacts"] += 1
                row["references"] += refs
                row["bytes"] += entry.size_bytes
                row["local_bytes"] += entry.size_bytes if is_local else 0
                row["referenced_bytes"] += 0 if is_local else entry.size_bytes
    return {
        "schema": PLACEMENT_REPORT_SCHEMA,
        "recommendation_only": True,
        "autonomous_deletion": False,
        "authority": "NONE",
        "totals": {"artifacts": len(index.entries), "logical_references": logical, "local_bytes": physical_local,
                   "referenced_only_bytes": referenced, "deduplication_saved_bytes": saved},
        "by_tier": dict(sorted(by_tier.items())),
        "by_class": dict(sorted(by_class.items())),
        "by_retention": dict(sorted(by_retention.items())),
        "by_backend": dict(sorted(by_backend.items())),
    }


class EvictionRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    size_bytes: int
    reason: str
    data_kind: DataKind
    retention: RetentionClass
    purge_rule: PurgeRule
    route: Literal["PLAN_PURGE_R0_R1_SCOPE", "HUMAN_DECISION_ONLY"]


class EvictionRecommendation(BaseModel):
    """What could free local space and how an operator could act on it. Never acts itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pressure: StoragePressure
    routes: tuple[EvictionRoute, ...]
    reclaimable_bytes: int
    protected_bytes: int
    authority: Literal["NONE"] = "NONE"
    deletes: Literal[False] = False
    recommendation_only: Literal[True] = True


def _route(kind: DataKind) -> Literal["PLAN_PURGE_R0_R1_SCOPE", "HUMAN_DECISION_ONLY"]:
    policy = CATALOG[kind]
    deletable = policy.purge_rule is PurgeRule.DELETE_RECORD and not policy.authoritative \
        and policy.retention in {RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL}
    return "PLAN_PURGE_R0_R1_SCOPE" if deletable else "HUMAN_DECISION_ONLY"


def recommend_eviction(index: PlacementIndex, local_backend_id: str, pressure: StoragePressure, *,
                       policy: StoragePolicy | None = None, budget: StorageBudget | None = None,
                       cache_bytes_in_use: int | None = None) -> EvictionRecommendation:
    """``plan_eviction`` annotated with the retention vocabulary and the purge route of each candidate."""
    active = policy or StoragePolicy()
    plan = plan_eviction(index, local_backend_id, pressure, budget or active.budget,
                         cache_bytes_in_use=cache_bytes_in_use)
    routes = []
    for candidate in plan.candidates:
        entry = index.entries[candidate.digest]
        kind = active.data_kind(entry.artifact_class)
        kind_policy = CATALOG[kind]
        routes.append(EvictionRoute(digest=candidate.digest, size_bytes=candidate.size_bytes,
                                    reason=candidate.reason, data_kind=kind, retention=kind_policy.retention,
                                    purge_rule=kind_policy.purge_rule, route=_route(kind)))
    return EvictionRecommendation(pressure=plan.pressure, routes=tuple(routes),
                                  reclaimable_bytes=plan.reclaimable_bytes, protected_bytes=plan.protected_bytes)


def class_retention_table(policy: StoragePolicy | None = None) -> Mapping[str, str]:
    """ArtifactClass -> RetentionClass under ``policy`` (for documentation and reports)."""
    active = policy or StoragePolicy()
    return {cls.value: active.retention_class(cls).value for cls in ArtifactClass}
