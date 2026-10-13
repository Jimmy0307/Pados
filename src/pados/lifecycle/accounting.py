# SPDX-License-Identifier: AGPL-3.0-only
"""Storage accounting by category: a read-only, recommendation-only report.

Accounting measures where PADOS keeps bytes and how much of it a purge *could*
reclaim under the retention policy. It reads sizes and modification times only
(never file contents), walks through descriptor-pinned no-follow directories
(``pados.lifecycle.fswalk``), refuses a configured root that is itself a link
(``ROOT_SYMLINK_REFUSED``), bounds every walk while enumerating, and reports
counts and bytes per category without any file name. Remote staging records
are purged per record by state, which accounting cannot see without reading
records, so their eligibility is ``ESTIMATE_UNAVAILABLE`` (use ``plan_purge``).
``eligible_for_purge`` is an estimate: it deletes nothing and authorizes
nothing (``recommendation_only=true``, ``autonomous_deletion=false``). R2 and R3
data is never estimated as eligible; a rejected R3 payload can only be purged
through ``pados.lifecycle.purge`` with explicit rejection proof.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from pados.config import Settings
from pados.lifecycle.fswalk import DEFAULT_MAX_ENTRIES, FsPolicyError, WalkStats, walk_files
from pados.lifecycle.retention import (
    CATALOG,
    DataKind,
    PurgeRule,
    RetentionClass,
    RetentionPolicy,
    StorageCategory,
    policy_for,
)
from pados.lifecycle.tables import TABLE_CATALOG

REPORT_SCHEMA = "pados.lifecycle.storage-accounting.v1"


class LocationStatus(StrEnum):
    MEASURED = "MEASURED"
    MISSING = "MISSING"  # configured, but nothing exists there yet
    ROOT_SYMLINK_REFUSED = "ROOT_SYMLINK_REFUSED"
    CONFIG_INVALID = "CONFIG_INVALID"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class CategoryStatus(StrEnum):
    MEASURED = "MEASURED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class PurgeUnit(StrEnum):
    FILE = "FILE"  # each regular file is one item
    STAGED_RECORD = "STAGED_RECORD"  # each remote staging record directory is one item


@dataclass(frozen=True)
class StorageLocation:
    location_id: str
    category: StorageCategory
    kinds: tuple[DataKind, ...]
    root: Path | None
    status: LocationStatus = LocationStatus.MEASURED
    purge_unit: PurgeUnit = PurgeUnit.FILE
    content_addressed: bool = False
    single_file: bool = False  # a ledger or snapshot file rather than a directory


ESTIMATE_UNAVAILABLE_STAGED = "ESTIMATE_UNAVAILABLE_STAGED_RECORDS_USE_PLAN_PURGE"


@dataclass(frozen=True)
class PurgeEstimate:
    bytes: int
    items: int
    basis: tuple[str, ...]
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"bytes": self.bytes, "items": self.items, "basis": list(self.basis),
                "estimate_only": True, "available": self.available}


@dataclass(frozen=True)
class LocationUsage:
    location_id: str
    category: StorageCategory
    status: LocationStatus
    bytes: int
    file_count: int
    retention_classes: tuple[RetentionClass, ...]
    oldest_mtime: str | None
    newest_mtime: str | None
    eligible_for_purge: PurgeEstimate
    truncated: bool
    symlinks_skipped: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "category": self.category.value,
            "status": self.status.value,
            "bytes": self.bytes,
            "file_count": self.file_count,
            "retention_classes": [c.value for c in self.retention_classes],
            "oldest_mtime": self.oldest_mtime,
            "newest_mtime": self.newest_mtime,
            "eligible_for_purge": self.eligible_for_purge.to_dict(),
            "truncated": self.truncated,
            "symlinks_skipped": self.symlinks_skipped,
        }


@dataclass(frozen=True)
class CategoryUsage:
    category: StorageCategory
    status: CategoryStatus
    bytes: int
    file_count: int
    retention_classes: tuple[RetentionClass, ...]
    oldest_mtime: str | None
    newest_mtime: str | None
    eligible_for_purge: PurgeEstimate
    truncated: bool
    location_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "bytes": self.bytes,
            "file_count": self.file_count,
            "retention_classes": [c.value for c in self.retention_classes],
            "oldest_mtime": self.oldest_mtime,
            "newest_mtime": self.newest_mtime,
            "eligible_for_purge": self.eligible_for_purge.to_dict(),
            "truncated": self.truncated,
            "locations": list(self.location_ids),
        }


@dataclass(frozen=True)
class StorageReport:
    generated_at: str
    policy: RetentionPolicy
    categories: tuple[CategoryUsage, ...]
    locations: tuple[LocationUsage, ...]
    database: Mapping[str, Any] = field(default_factory=lambda: {"status": "DB_NOT_REQUESTED"})
    recommendation_only: bool = True
    autonomous_deletion: bool = False

    def category(self, category: StorageCategory) -> CategoryUsage:
        return next(c for c in self.categories if c.category is category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "generated_at": self.generated_at,
            "recommendation_only": self.recommendation_only,
            "autonomous_deletion": self.autonomous_deletion,
            "policy": {"r0_seconds": int(self.policy.r0.total_seconds()),
                       "r1_seconds": int(self.policy.r1.total_seconds()),
                       "explicit_override": self.policy.explicit_override},
            "categories": {c.category.value: c.to_dict() for c in self.categories},
            "locations": [loc.to_dict() for loc in self.locations],
            "database": dict(self.database),
        }


# --- location registry -------------------------------------------------------------

_K = DataKind
_C = StorageCategory


def _location(location_id: str, category: StorageCategory, kinds: tuple[DataKind, ...], root: str | Path,
              **kwargs: Any) -> StorageLocation:
    path = Path(root).expanduser()
    return StorageLocation(location_id, category, kinds, path,
                           LocationStatus.MEASURED if path.exists() else LocationStatus.MISSING, **kwargs)


def _invalid(location_id: str, category: StorageCategory, kinds: tuple[DataKind, ...]) -> StorageLocation:
    return StorageLocation(location_id, category, kinds, None, LocationStatus.CONFIG_INVALID)


def _remote_locations(config_path: str) -> list[StorageLocation]:
    from pados.remote.config import RemoteConfig

    staging_kinds = (_K.TEMPORARY_UPLOAD, _K.QUARANTINED_UPLOAD)
    try:
        config = RemoteConfig.load(config_path)
    except (OSError, ValueError, KeyError, TypeError):
        return [_invalid("remote_staging", _C.REMOTE_UPLOADS, staging_kinds),
                _invalid("remote_intent_ledger", _C.AUTHORITY_METADATA, (_K.REMOTE_INTENT_LEDGER,))]
    return [
        _location("remote_staging", _C.REMOTE_UPLOADS, staging_kinds, config.staging_root,
                  purge_unit=PurgeUnit.STAGED_RECORD),
        _location("remote_intent_ledger", _C.AUTHORITY_METADATA, (_K.REMOTE_INTENT_LEDGER,), config.ledger_path,
                  single_file=True),
    ]


def _compute_node_locations(config_path: str) -> list[StorageLocation]:
    from pados.compute_node.registry import NodeRegistry

    kinds = (_K.MACHINE_CONNECTION_HISTORY,)
    try:
        registry = NodeRegistry.load(config_path)
    except (OSError, ValueError, KeyError, TypeError):
        return [_invalid("compute_node_state", _C.OPERATIONAL_LOGS, kinds)]
    return [_location("compute_node_state", _C.OPERATIONAL_LOGS, kinds, registry.state_dir)]


def _connectivity_locations(config_path: str) -> list[StorageLocation]:
    from pados.connectivity.config import ConnectivityConfig

    kinds = (_K.CONNECTIVITY_HISTORY,)
    try:
        config = ConnectivityConfig.load(config_path)
    except (OSError, ValueError, KeyError, TypeError):
        return [_invalid("connectivity_snapshot", _C.OPERATIONAL_LOGS, kinds)]
    if config.snapshot_path is None:
        return []
    return [_location("connectivity_snapshot", _C.OPERATIONAL_LOGS, kinds, config.snapshot_path, single_file=True)]


def build_locations(
    settings: Settings,
    *,
    runtime_root: Path,
    herdr_root: Path | None = None,
    sandbox_roots: Sequence[Path] = (),
    tombstone_ledger: Path | None = None,
    extra: Iterable[StorageLocation] = (),
) -> tuple[StorageLocation, ...]:
    """Every PADOS storage location derivable from configuration.

    Unset configuration means the location is absent (never an error); a config
    file that cannot be parsed yields a ``CONFIG_INVALID`` location with no root.
    Herdr telemetry and sandbox working roots have no configured store yet and
    are accepted as injected roots.
    """
    locations: list[StorageLocation] = []
    if settings.product_object_root:
        locations.append(_location("product_objects", _C.ARTIFACTS,
                                   (_K.ACTIVE_ARTIFACT, _K.REJECTED_ARTIFACT_PAYLOAD),
                                   settings.product_object_root, content_addressed=True))
    if settings.product_workspace_root:
        locations.append(_location("product_workspace", _C.PROJECT_SOURCES, (_K.GOVERNED_PROJECT_CONTENT,),
                                   settings.product_workspace_root))
    if settings.remote_config:
        locations.extend(_remote_locations(settings.remote_config))
    if settings.compute_nodes_config:
        locations.extend(_compute_node_locations(settings.compute_nodes_config))
    if settings.connectivity_config:
        locations.extend(_connectivity_locations(settings.connectivity_config))
    runtime = Path(runtime_root)
    locations.append(_location("runtime_logs", _C.OPERATIONAL_LOGS, (_K.OPERATIONAL_LOG,), runtime / "logs"))
    locations.append(_location("runtime_cache", _C.CACHE, (_K.CACHE_ENTRY,), runtime / "cache"))
    locations.append(_location("release_test_evidence", _C.RELEASE_TEST_EVIDENCE, (_K.RELEASE_TEST_EVIDENCE,),
                               runtime / "artifacts" / "reports"))
    if herdr_root is not None:
        locations.append(_location("herdr_telemetry", _C.HERDR_TELEMETRY, (_K.HERDR_TELEMETRY,), herdr_root))
    for index, root in enumerate(sandbox_roots):
        locations.append(_location(f"sandbox_{index}", _C.SANDBOX_DATA,
                                   (_K.SANDBOX_WORKING_DATA, _K.RAW_INTERMEDIATE_OUTPUT), root))
    if tombstone_ledger is not None:
        locations.append(_location("payload_tombstones", _C.AUTHORITY_METADATA, (_K.PAYLOAD_TOMBSTONE,),
                                   tombstone_ledger, single_file=True))
    locations.extend(extra)
    ids = [loc.location_id for loc in locations]
    if len(ids) != len(set(ids)):
        raise ValueError("STORAGE_LOCATION_DUPLICATE")
    return tuple(locations)


# --- eligibility -------------------------------------------------------------------


def purge_window(kinds: Iterable[DataKind], policy: RetentionPolicy) -> tuple[timedelta | None, tuple[str, ...]]:
    """Age after which every item of a location may be deleted, or ``None``.

    Mixed R0/R1 locations use the longest window (conservative). Any R2/R3 kind
    makes the whole location ineligible for age-based deletion.
    """
    policies = [policy_for(kind) for kind in kinds]
    basis: list[str] = []
    if any(p.retention is RetentionClass.R3_AUTHORITY and p.purge_rule is PurgeRule.NEVER for p in policies):
        basis.append("NOT_ELIGIBLE_R3_AUTHORITY_METADATA")
    if any(p.retention is RetentionClass.R2_PROJECT for p in policies):
        basis.append("NOT_ELIGIBLE_R2_HUMAN_DECISION_ONLY")
    if any(p.purge_rule is PurgeRule.PURGE_PAYLOAD_KEEP_METADATA for p in policies):
        basis.append("R3_PAYLOAD_PURGE_REQUIRES_EXPLICIT_REJECTION_PROOF")
    if basis or not policies:
        return None, tuple(basis) or ("NO_DATA_KIND",)
    windows = [(policy.window(p.retention), p.retention) for p in policies]
    window, retention = max(((w, r) for w, r in windows if w is not None), key=lambda pair: pair[0])
    note = "" if len({r for _, r in windows}) == 1 else ":CONSERVATIVE_LONGEST_WINDOW"
    return window, (f"AGE_EXCEEDS_{retention.value}_WINDOW_{int(window.total_seconds())}S{note}",)


def _iso(mtime_ns: int | None) -> str | None:
    return None if mtime_ns is None else datetime.fromtimestamp(mtime_ns / 1e9, UTC).isoformat()


def _classes(kinds: Iterable[DataKind]) -> tuple[RetentionClass, ...]:
    found = {policy_for(kind).retention for kind in kinds}
    return tuple(c for c in RetentionClass if c in found)


def measure_location(location: StorageLocation, *, policy: RetentionPolicy, now: datetime,
                     max_entries: int = DEFAULT_MAX_ENTRIES) -> LocationUsage:
    window, basis = purge_window(location.kinds, policy)
    if location.root is None or location.status in {LocationStatus.CONFIG_INVALID, LocationStatus.NOT_CONFIGURED}:
        return LocationUsage(location.location_id, location.category, location.status, 0, 0,
                             _classes(location.kinds), None, None,
                             PurgeEstimate(0, 0, basis, location.purge_unit is not PurgeUnit.STAGED_RECORD),
                             False, 0)
    staged = location.purge_unit is PurgeUnit.STAGED_RECORD
    if staged:
        window, basis = None, (ESTIMATE_UNAVAILABLE_STAGED,)
    stats = WalkStats(max_entries=max_entries)
    total = count = eligible_bytes = eligible_items = 0
    oldest: int | None = None
    newest: int | None = None
    cutoff_ns = None if window is None else int((now - window).timestamp() * 1e9)
    try:
        for entry in walk_files(location.root, max_entries=max_entries, stats=stats):
            total += entry.size_bytes
            count += 1
            oldest = entry.mtime_ns if oldest is None else min(oldest, entry.mtime_ns)
            newest = entry.mtime_ns if newest is None else max(newest, entry.mtime_ns)
            if cutoff_ns is not None and entry.mtime_ns < cutoff_ns:
                eligible_bytes += entry.size_bytes
                eligible_items += 1
    except FsPolicyError:
        return LocationUsage(location.location_id, location.category, LocationStatus.ROOT_SYMLINK_REFUSED, 0, 0,
                             _classes(location.kinds), None, None,
                             PurgeEstimate(0, 0, ("ROOT_SYMLINK_REFUSED",), not staged), False, 1)
    status = LocationStatus.MEASURED if location.root.exists() else LocationStatus.MISSING
    return LocationUsage(location.location_id, location.category, status, total, count,
                         _classes(location.kinds), _iso(oldest), _iso(newest),
                         PurgeEstimate(eligible_bytes, eligible_items, basis, not staged), stats.truncated,
                         stats.symlinks_skipped)


def _aggregate(category: StorageCategory, usages: list[LocationUsage]) -> CategoryUsage:
    catalog_kinds = [k for k, p in CATALOG.items() if p.category is category]
    classes = set(_classes(catalog_kinds)) | {c for u in usages for c in u.retention_classes}
    measured = [u for u in usages if u.status in {LocationStatus.MEASURED, LocationStatus.MISSING,
                                                   LocationStatus.ROOT_SYMLINK_REFUSED}]
    oldest = [u.oldest_mtime for u in usages if u.oldest_mtime]
    newest = [u.newest_mtime for u in usages if u.newest_mtime]
    basis = sorted({b for u in usages for b in u.eligible_for_purge.basis}) or ["NOT_CONFIGURED"]
    return CategoryUsage(
        category=category,
        status=CategoryStatus.MEASURED if measured else CategoryStatus.NOT_CONFIGURED,
        bytes=sum(u.bytes for u in usages),
        file_count=sum(u.file_count for u in usages),
        retention_classes=tuple(c for c in RetentionClass if c in classes),
        oldest_mtime=min(oldest) if oldest else None,
        newest_mtime=max(newest) if newest else None,
        eligible_for_purge=PurgeEstimate(sum(u.eligible_for_purge.bytes for u in usages),
                                         sum(u.eligible_for_purge.items for u in usages), tuple(basis),
                                         all(u.eligible_for_purge.available for u in usages)),
        truncated=any(u.truncated for u in usages),
        location_ids=tuple(u.location_id for u in usages),
    )


def account_storage(
    locations: Iterable[StorageLocation],
    *,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    database: Mapping[str, Any] | None = None,
) -> StorageReport:
    """Measure every location and aggregate per ``StorageCategory`` (all categories listed)."""
    active = policy or RetentionPolicy()
    moment = now or datetime.now(UTC)
    usages = [measure_location(loc, policy=active, now=moment, max_entries=max_entries) for loc in locations]
    categories = tuple(_aggregate(cat, [u for u in usages if u.category is cat]) for cat in StorageCategory)
    return StorageReport(
        generated_at=moment.isoformat(),
        policy=active,
        categories=categories,
        locations=tuple(usages),
        database=dict(database) if database is not None else {"status": "DB_NOT_REQUESTED"},
    )


# --- optional database accounting --------------------------------------------------

_SIZE_QUERY = text(
    "SELECT c.relname AS table_name, pg_total_relation_size(c.oid) AS total_bytes, "
    "GREATEST(c.reltuples, 0)::bigint AS row_estimate "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p') AND n.nspname = current_schema()"
)


def account_database(engine: Engine | None) -> dict[str, Any]:
    """Per-category table sizes via ``pg_total_relation_size``; degrades to DB_NOT_AVAILABLE.

    Only aggregate numbers leave this function: unmapped tables are counted, not named.
    """
    if engine is None:
        return {"status": "DB_NOT_AVAILABLE", "recommendation_only": True}
    try:
        with engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(_SIZE_QUERY).mappings()]
    except (SQLAlchemyError, OSError):
        return {"status": "DB_NOT_AVAILABLE", "recommendation_only": True}
    categories: dict[str, dict[str, Any]] = {
        cat.value: {"bytes": 0, "row_estimate": 0, "tables": 0} for cat in StorageCategory
    }
    unmapped = {"bytes": 0, "row_estimate": 0, "tables": 0}
    seen: set[str] = set()
    for row in rows:
        name = str(row["table_name"])
        target = unmapped
        if name in TABLE_CATALOG:
            target = categories[TABLE_CATALOG[name][0].value]
            seen.add(name)
        target["bytes"] += int(row["total_bytes"] or 0)
        target["row_estimate"] += int(row["row_estimate"] or 0)
        target["tables"] += 1
    for usage in categories.values():
        usage["eligible_for_purge"] = {"bytes": 0, "items": 0, "basis": ["DB_ROW_PURGE_NOT_SUPPORTED_V1"],
                                       "estimate_only": True}
    return {
        "status": "DB_MEASURED",
        "recommendation_only": True,
        "autonomous_deletion": False,
        "categories": categories,
        "unmapped": unmapped,
        "known_tables_missing": len(set(TABLE_CATALOG) - seen),
    }


def storage_report(
    settings: Settings,
    *,
    runtime_root: Path,
    engine: Engine | None = None,
    include_database: bool = False,
    herdr_root: Path | None = None,
    sandbox_roots: Sequence[Path] = (),
    tombstone_ledger: Path | None = None,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict[str, Any]:
    """Convenience entry point for CLI/API wiring: configuration -> report dict."""
    locations = build_locations(settings, runtime_root=runtime_root, herdr_root=herdr_root,
                                sandbox_roots=sandbox_roots, tombstone_ledger=tombstone_ledger)
    database = account_database(engine) if include_database else None
    return account_storage(locations, policy=policy, now=now, max_entries=max_entries,
                           database=database).to_dict()
