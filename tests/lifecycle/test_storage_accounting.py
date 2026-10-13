import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from pados.config import Settings
from pados.lifecycle.accounting import (
    CategoryStatus,
    LocationStatus,
    StorageLocation,
    account_database,
    account_storage,
    build_locations,
    storage_report,
)
from pados.lifecycle.retention import DataKind, RetentionClass, StorageCategory
from tests.windows_links import create_link_or_skip

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _write(path: Path, size: int, *, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _configs(tmp_path: Path) -> dict[str, str]:
    remote = tmp_path / "remote.json"
    remote.write_text(json.dumps({
        "bind_host": "100.64.0.10", "staging_root": str(tmp_path / "staging"),
        "ledger_path": str(tmp_path / "ledger" / "intents.jsonl"),
        "actors": [{"actor_id": "owner", "credential_hash": hashlib.sha256(b"x").hexdigest(), "projects": ["p"]}],
    }), encoding="utf-8")
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"state_dir": str(tmp_path / "node-state"), "nodes": []}), encoding="utf-8")
    connectivity = tmp_path / "connectivity.json"
    connectivity.write_text(json.dumps({"snapshot_path": str(tmp_path / "conn" / "connectivity.json")}),
                            encoding="utf-8")
    return {
        "PADOS_PRODUCT_OBJECT_ROOT": str(tmp_path / "objects"),
        "PADOS_REMOTE_CONFIG": str(remote),
        "PADOS_COMPUTE_NODES_CONFIG": str(nodes),
        "PADOS_CONNECTIVITY_CONFIG": str(connectivity),
    }


@pytest.fixture
def populated(tmp_path: Path) -> tuple[Settings, Path]:
    runtime = tmp_path / "runtime"
    _write(runtime / "logs" / "old.log", 100, age=timedelta(days=20))
    _write(runtime / "logs" / "new.log", 10, age=timedelta(days=1))
    _write(runtime / "cache" / "c.bin", 50, age=timedelta(days=2))
    _write(runtime / "artifacts" / "reports" / "acceptance.md", 70, age=timedelta(days=400))
    _write(tmp_path / "objects" / "ab" / "cd" / ("ab" + "cd" + "0" * 60), 40, age=timedelta(days=90))
    _write(tmp_path / "staging" / "stg-0123456789abcdef0123" / "content.bin", 30, age=timedelta(days=20))
    _write(tmp_path / "ledger" / "intents.jsonl", 20, age=timedelta(days=60))
    _write(tmp_path / "node-state" / "probes" / "lcn-x.json", 5, age=timedelta(days=3))
    _write(tmp_path / "conn" / "connectivity.json", 7, age=timedelta(minutes=5))
    return _settings(**_configs(tmp_path)), runtime


def test_locations_from_settings_cover_every_configured_store(populated: tuple[Settings, Path]) -> None:
    settings, runtime = populated
    ids = {loc.location_id: loc for loc in build_locations(settings, runtime_root=runtime)}
    assert set(ids) == {"product_objects", "remote_staging", "remote_intent_ledger", "compute_node_state",
                        "connectivity_snapshot", "runtime_logs", "runtime_cache", "release_test_evidence"}
    assert ids["product_objects"].content_addressed
    assert ids["remote_intent_ledger"].category is StorageCategory.AUTHORITY_METADATA


def test_unset_config_is_absent_and_herdr_reports_not_configured(tmp_path: Path) -> None:
    locations = build_locations(_settings(), runtime_root=tmp_path / "runtime")
    assert {loc.location_id for loc in locations} == {"runtime_logs", "runtime_cache", "release_test_evidence"}
    report = account_storage(locations, now=NOW)
    herdr = report.category(StorageCategory.HERDR_TELEMETRY)
    assert herdr.status is CategoryStatus.NOT_CONFIGURED and herdr.bytes == 0
    assert herdr.retention_classes == (RetentionClass.R1_OPERATIONAL,)
    assert report.category(StorageCategory.OPERATIONAL_LOGS).status is CategoryStatus.MEASURED
    assert {loc.status for loc in report.locations} == {LocationStatus.MISSING}

    _write(tmp_path / "herdr" / "t.jsonl", 9, age=timedelta(days=40))
    injected = build_locations(_settings(), runtime_root=tmp_path / "runtime", herdr_root=tmp_path / "herdr")
    herdr = account_storage(injected, now=NOW).category(StorageCategory.HERDR_TELEMETRY)
    assert herdr.status is CategoryStatus.MEASURED and herdr.bytes == 9 and herdr.eligible_for_purge.items == 1


def test_invalid_config_degrades_without_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    locations = build_locations(_settings(PADOS_REMOTE_CONFIG=str(bad), PADOS_COMPUTE_NODES_CONFIG=str(bad),
                                          PADOS_CONNECTIVITY_CONFIG=str(tmp_path / "missing.json")),
                                runtime_root=tmp_path)
    invalid = {loc.location_id for loc in locations if loc.status is LocationStatus.CONFIG_INVALID}
    assert invalid == {"remote_staging", "remote_intent_ledger", "compute_node_state", "connectivity_snapshot"}
    report = account_storage(locations, now=NOW).to_dict()
    assert all(loc["bytes"] == 0 for loc in report["locations"] if loc["status"] == "CONFIG_INVALID")


def test_category_totals_and_purge_estimates(populated: tuple[Settings, Path]) -> None:
    settings, runtime = populated
    report = account_storage(build_locations(settings, runtime_root=runtime), now=NOW)
    logs = report.category(StorageCategory.OPERATIONAL_LOGS)
    # runtime logs 110 + node probe 5 + connectivity snapshot 7
    assert (logs.bytes, logs.file_count) == (122, 4)
    assert (logs.eligible_for_purge.bytes, logs.eligible_for_purge.items) == (100, 1)
    cache = report.category(StorageCategory.CACHE)
    assert (cache.eligible_for_purge.bytes, cache.eligible_for_purge.items) == (50, 1)  # R0 24h elapsed
    uploads = report.category(StorageCategory.REMOTE_UPLOADS)
    assert uploads.bytes == 30 and uploads.eligible_for_purge.available is False
    for category in (StorageCategory.RELEASE_TEST_EVIDENCE, StorageCategory.ARTIFACTS,
                     StorageCategory.AUTHORITY_METADATA):
        usage = report.category(category)
        assert usage.bytes > 0
        assert (usage.eligible_for_purge.bytes, usage.eligible_for_purge.items) == (0, 0)
    assert "NOT_ELIGIBLE_R2_HUMAN_DECISION_ONLY" in report.category(StorageCategory.RELEASE_TEST_EVIDENCE) \
        .eligible_for_purge.basis
    artifact_basis = report.category(StorageCategory.ARTIFACTS).eligible_for_purge.basis
    assert "R3_PAYLOAD_PURGE_REQUIRES_EXPLICIT_REJECTION_PROOF" in artifact_basis
    assert "NOT_ELIGIBLE_R3_AUTHORITY_METADATA" in report.category(StorageCategory.AUTHORITY_METADATA) \
        .eligible_for_purge.basis
    assert logs.oldest_mtime == (NOW - timedelta(days=20)).isoformat()
    assert logs.newest_mtime == (NOW - timedelta(minutes=5)).isoformat()

    data = report.to_dict()
    assert data["recommendation_only"] is True and data["autonomous_deletion"] is False
    assert set(data["categories"]) == {c.value for c in StorageCategory}
    assert all(c["eligible_for_purge"]["estimate_only"] for c in data["categories"].values())


def test_directory_junctions_are_never_followed_out_of_a_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "big.bin", 10_000, age=timedelta(days=90))
    logs = tmp_path / "runtime" / "logs"
    _write(logs / "own.log", 10, age=timedelta(days=90))
    create_link_or_skip(outside, logs / "escape-dir", directory=True)
    location = StorageLocation("runtime_logs", StorageCategory.OPERATIONAL_LOGS, (DataKind.OPERATIONAL_LOG,), logs)
    usage = account_storage([location], now=NOW).locations[0]
    assert (usage.bytes, usage.file_count, usage.symlinks_skipped) == (10, 1, 1)
    assert usage.eligible_for_purge.bytes == 10


def test_file_symlinks_are_never_followed_out_of_a_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside / "big.bin", 10_000, age=timedelta(days=90))
    logs = tmp_path / "runtime" / "logs"
    _write(logs / "own.log", 10, age=timedelta(days=90))
    create_link_or_skip(outside / "big.bin", logs / "escape-file", directory=False)
    location = StorageLocation("runtime_logs", StorageCategory.OPERATIONAL_LOGS, (DataKind.OPERATIONAL_LOG,), logs)
    usage = account_storage([location], now=NOW).locations[0]
    assert (usage.bytes, usage.file_count, usage.symlinks_skipped) == (10, 1, 1)
    assert usage.eligible_for_purge.bytes == 10


def test_walk_is_bounded_and_reports_truncation(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    for index in range(10):
        _write(root / f"f{index}.bin", 1, age=timedelta(days=3))
    location = StorageLocation("runtime_cache", StorageCategory.CACHE, (DataKind.CACHE_ENTRY,), root)
    usage = account_storage([location], now=NOW, max_entries=3).locations[0]
    assert usage.truncated and usage.file_count == 3
    assert account_storage([location], now=NOW).category(StorageCategory.CACHE).truncated is False


def test_report_never_leaks_file_names_or_paths(populated: tuple[Settings, Path], tmp_path: Path) -> None:
    settings, runtime = populated
    _write(runtime / "cache" / "customer-acme-merger-plan.docx", 3, age=timedelta(days=1))
    _write(tmp_path / "staging" / "stg-0123456789abcdef0123" / "Quarterly Payroll.xlsx", 3, age=timedelta(days=1))
    text = json.dumps(storage_report(settings, runtime_root=runtime, now=NOW))
    for leaked in ("customer-acme", "Payroll", "acceptance.md", "intents.jsonl", "stg-0123", str(tmp_path)):
        assert leaked not in text


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(self.rows)


class _Engine:
    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.connection = _Connection(rows or [])
        self.fail = fail

    def connect(self) -> _Connection:
        if self.fail:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return self.connection


def test_database_accounting_maps_tables_and_degrades() -> None:
    engine = _Engine([
        {"table_name": "audit_events", "total_bytes": 8192, "row_estimate": 10},
        {"table_name": "state_proof_commits", "total_bytes": 4096, "row_estimate": 3},
        {"table_name": "run_events", "total_bytes": 1000, "row_estimate": 50},
        {"table_name": "corpus_documents", "total_bytes": 2000, "row_estimate": 5},
        {"table_name": "someone_elses_private_table", "total_bytes": 7, "row_estimate": 1},
    ])
    result = account_database(engine)  # type: ignore[arg-type]
    assert "pg_total_relation_size" in engine.connection.statements[0]
    assert result["status"] == "DB_MEASURED" and result["recommendation_only"] is True
    authority = result["categories"]["AUTHORITY_METADATA"]
    assert (authority["bytes"], authority["row_estimate"], authority["tables"]) == (12288, 13, 2)
    assert result["categories"]["OPERATIONAL_LOGS"]["bytes"] == 1000
    assert result["categories"]["PROJECT_SOURCES"]["bytes"] == 2000
    assert result["unmapped"] == {"bytes": 7, "row_estimate": 1, "tables": 1}
    assert "someone_elses_private_table" not in json.dumps(result)
    assert all(c["eligible_for_purge"]["items"] == 0 for c in result["categories"].values())
    assert account_database(None)["status"] == "DB_NOT_AVAILABLE"
    assert account_database(_Engine(fail=True))["status"] == "DB_NOT_AVAILABLE"  # type: ignore[arg-type]


def test_storage_report_embeds_database_only_on_request(populated: tuple[Settings, Path]) -> None:
    settings, runtime = populated
    assert storage_report(settings, runtime_root=runtime, now=NOW)["database"]["status"] == "DB_NOT_REQUESTED"
    report = storage_report(settings, runtime_root=runtime, now=NOW, include_database=True, engine=None)
    assert report["database"]["status"] == "DB_NOT_AVAILABLE"


def test_mixed_class_location_uses_the_longest_window(tmp_path: Path) -> None:
    root = tmp_path / "sbx"
    _write(root / "a.bin", 5, age=timedelta(days=3))
    location = StorageLocation("mixed", StorageCategory.SANDBOX_DATA,
                               (DataKind.SANDBOX_WORKING_DATA, DataKind.NOTIFICATION_DELIVERY), root)
    usage = account_storage([location], now=NOW).locations[0]
    assert usage.eligible_for_purge.items == 0  # R1 14d governs, not R0 24h
    assert any("CONSERVATIVE_LONGEST_WINDOW" in b for b in usage.eligible_for_purge.basis)


def test_cr02_f12_staging_eligibility_is_reported_unavailable(populated: tuple[Settings, Path],
                                                             tmp_path: Path) -> None:
    settings, runtime = populated
    record = tmp_path / "staging" / "stg-0123456789abcdef0123" / "record.json"
    _write(record, 12, age=timedelta(minutes=1))  # fresh record next to an old content.bin
    report = account_storage(build_locations(settings, runtime_root=runtime), now=NOW).to_dict()
    staging = next(loc for loc in report["locations"] if loc["location_id"] == "remote_staging")
    assert staging["eligible_for_purge"] == {"bytes": 0, "items": 0, "estimate_only": True, "available": False,
                                             "basis": ["ESTIMATE_UNAVAILABLE_STAGED_RECORDS_USE_PLAN_PURGE"]}
    assert report["categories"]["REMOTE_UPLOADS"]["eligible_for_purge"]["available"] is False
    assert report["categories"]["CACHE"]["eligible_for_purge"]["available"] is True


def test_cr02_f02_linked_root_is_refused_and_swapped_subdirectory_not_followed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    _write(outside / "big.bin", 10_000, age=timedelta(days=90))
    real = tmp_path / "real-logs"
    _write(real / "own.log", 10, age=timedelta(days=90))
    create_link_or_skip(real, tmp_path / "linked-logs", directory=True)
    linked = StorageLocation("runtime_logs", StorageCategory.OPERATIONAL_LOGS, (DataKind.OPERATIONAL_LOG,),
                             tmp_path / "linked-logs")
    usage = account_storage([linked], now=NOW).locations[0]
    assert usage.status is LocationStatus.ROOT_SYMLINK_REFUSED and usage.bytes == 0

    _write(real / "sub" / "inner.log", 7, age=timedelta(days=90))
    import pados.lifecycle.fswalk as fswalk

    with fswalk.open_root(real) as pinned:
        directory_type = type(pinned)
    original = directory_type.scan

    def swapping_scan(self: Any, stats: fswalk.WalkStats) -> list[tuple[str, os.stat_result]]:
        listed = original(self, stats)
        if (real / "sub").is_dir() and not (real / "sub").is_symlink():
            (real / "sub").rename(tmp_path / "sub-moved")  # listed as a directory, then swapped
            create_link_or_skip(outside, real / "sub", directory=True)
        return listed

    monkeypatch.setattr(directory_type, "scan", swapping_scan)
    location = StorageLocation("runtime_logs", StorageCategory.OPERATIONAL_LOGS, (DataKind.OPERATIONAL_LOG,), real)
    usage = account_storage([location], now=NOW).locations[0]
    assert (usage.bytes, usage.file_count) == (10, 1) and usage.symlinks_skipped == 1


def test_cr02_f11_walk_budget_is_enforced_while_enumerating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cache"
    for index in range(200):
        _write(root / f"f{index:03d}.bin", 1, age=timedelta(days=3))
    import pados.lifecycle.fswalk as fswalk

    seen = {"stat": 0}
    real_stat = os.stat

    def counting_stat(*args: object, **kwargs: object) -> os.stat_result:
        seen["stat"] += "dir_fd" in kwargs  # per-entry stats of the descriptor walk
        return real_stat(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fswalk.os, "stat", counting_stat)
    location = StorageLocation("runtime_cache", StorageCategory.CACHE, (DataKind.CACHE_ENTRY,), root)
    usage = account_storage([location], now=NOW, max_entries=10).locations[0]
    assert usage.truncated and usage.file_count == 10
    assert seen["stat"] <= 10  # entries beyond the budget are never even stat'ed
