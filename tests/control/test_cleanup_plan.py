# SPDX-License-Identifier: AGPL-3.0-only
"""Cleanup plan preview: it never selects evidence, decisions, audit records or unique copies, whatever their age, and
it never offers to execute anything. Files are real (old modification times on a temporary tree)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pados.control import cleanup_plan
from pados.lifecycle.accounting import (
    LocationStatus,
    PurgeUnit,
    StorageLocation,
    account_storage,
    build_locations,
)
from pados.lifecycle.retention import CATALOG, DataKind, PurgeRule, RetentionClass, StorageCategory

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
OLD = (NOW - timedelta(days=400)).timestamp()
PROTECTED_WORDS = ("EVIDENCE", "RECEIPT", "DECISION", "AUDIT", "LEDGER", "TOMBSTONE", "ACCEPTANCE")


def _old_tree(root: Path, files: int = 2) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        path = root / f"f{index}.bin"
        path.write_bytes(b"x" * 100)
        os.utime(path, (OLD, OLD))
    return root


def _plan(locations: list[StorageLocation]) -> dict[str, object]:
    report = account_storage(locations, now=NOW)
    return cleanup_plan.plan_cleanup(locations, report.locations)


def test_every_data_kind_old_enough_is_a_candidate_only_when_it_is_a_deletable_cleanup_class(tmp_path: Path) -> None:
    locations = [StorageLocation(f"kind_{kind.value.lower()}", policy.category, (kind,),
                                 _old_tree(tmp_path / kind.value), LocationStatus.MEASURED)
                 for kind, policy in CATALOG.items()]
    plan = _plan(locations)
    candidates = plan["candidates"]
    assert isinstance(candidates, list) and candidates  # the cleanup classes do produce candidates
    for candidate in candidates:
        (kind,) = [DataKind(value) for value in candidate["data_kinds"]]
        policy = CATALOG[kind]
        assert policy.category in cleanup_plan.CLEANUP_CATEGORIES, kind
        assert policy.retention in {RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL}, kind
        assert policy.purge_rule is PurgeRule.DELETE_RECORD and not policy.authoritative, kind
        assert not any(word in kind.value for word in PROTECTED_WORDS), kind
        assert candidate["reclaimable_bytes"] == 200 and candidate["estimate_only"] is True
    chosen = {DataKind(c["data_kinds"][0]) for c in candidates}
    for kind in (DataKind.AUDIT_CHAIN, DataKind.DECISION_IDENTITY, DataKind.EXECUTION_RECEIPT_SUMMARY,
                 DataKind.PROJECT_EVIDENCE, DataKind.RELEASE_TEST_EVIDENCE, DataKind.CAPABILITY_STAGE_EVIDENCE,
                 DataKind.ACTIVE_ARTIFACT, DataKind.PROJECT_SOURCE, DataKind.GOVERNED_PROJECT_CONTENT,
                 DataKind.QUARANTINED_UPLOAD, DataKind.TEMPORARY_UPLOAD, DataKind.PAYLOAD_TOMBSTONE,
                 DataKind.REMOTE_INTENT_LEDGER, DataKind.REJECTED_ARTIFACT_PAYLOAD, DataKind.MACHINE_INVENTORY):
        assert kind not in chosen, kind
    assert {DataKind.CACHE_ENTRY, DataKind.TEMPORARY_FILE, DataKind.SANDBOX_WORKING_DATA,
            DataKind.OPERATIONAL_LOG} <= chosen
    protected = {entry["location_id"]: entry["reason"] for entry in plan["protected"]}  # type: ignore[union-attr]
    assert protected["kind_audit_chain"] == "PROTECTED_DECISIONS_AND_AUDIT"
    assert protected["kind_project_evidence"] == "PROTECTED_EVIDENCE_AND_UNIQUE_COPIES"
    assert protected["kind_release_test_evidence"] == "PROTECTED_EVIDENCE"
    assert protected["kind_capability_stage_evidence"] == "PROTECTED_KIND_CAPABILITY_STAGE_EVIDENCE"
    assert protected["kind_project_source"] == "PROTECTED_UNIQUE_PROJECT_COPIES"


def test_a_mixed_location_with_any_protected_kind_is_protected(tmp_path: Path) -> None:
    mixed = StorageLocation("mixed_logs", StorageCategory.OPERATIONAL_LOGS,
                            (DataKind.OPERATIONAL_LOG, DataKind.CAPABILITY_STAGE_EVIDENCE),
                            _old_tree(tmp_path / "mixed"), LocationStatus.MEASURED)
    plan = _plan([mixed])
    assert plan["candidates"] == []
    assert plan["protected"] == [{"location_id": "mixed_logs", "category": "OPERATIONAL_LOGS",
                                  "data_kinds": ["OPERATIONAL_LOG", "CAPABILITY_STAGE_EVIDENCE"], "bytes": 200,
                                  "file_count": 2, "reason": "PROTECTED_KIND_CAPABILITY_STAGE_EVIDENCE"}]


@pytest.mark.parametrize(("location", "reason"), [
    (StorageLocation("cas", StorageCategory.CACHE, (DataKind.CACHE_ENTRY,), None, content_addressed=True),
     "PROTECTED_CONTENT_ADDRESSED_UNIQUE_COPY"),
    (StorageLocation("staged", StorageCategory.CACHE, (DataKind.CACHE_ENTRY,), None,
                     purge_unit=PurgeUnit.STAGED_RECORD), "PROTECTED_STAGED_RECORDS_NEED_PLAN_PURGE"),
    (StorageLocation("nothing", StorageCategory.CACHE, (), None), "PROTECTED_NO_DATA_KIND"),
    (StorageLocation("telemetry", StorageCategory.HERDR_TELEMETRY, (DataKind.HERDR_TELEMETRY,), None),
     "NOT_A_CLEANUP_CLASS"),
])
def test_unique_copies_and_unknown_classes_are_never_candidates(location: StorageLocation, reason: str) -> None:
    assert cleanup_plan.protected_reason(location) == reason


def test_the_configured_locations_keep_evidence_and_sources_protected(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    _old_tree(runtime / "cache")
    _old_tree(runtime / "logs")
    _old_tree(runtime / "artifacts" / "reports")
    _old_tree(tmp_path / "objects")
    _old_tree(tmp_path / "workspace" / "demo")
    settings = SimpleNamespace(product_object_root=str(tmp_path / "objects"),
                               product_workspace_root=str(tmp_path / "workspace"), remote_config="",
                               compute_nodes_config="", connectivity_config="")
    jobs = StorageLocation("job_artifacts", StorageCategory.ARTIFACTS, (DataKind.PROJECT_EVIDENCE,),
                           _old_tree(runtime / "artifacts" / "jobs"))
    locations = list(build_locations(settings, runtime_root=runtime, extra=(jobs,)))  # type: ignore[arg-type]
    plan = _plan(locations)
    assert sorted(c["location_id"] for c in plan["candidates"]) == ["runtime_cache", "runtime_logs"]  # type: ignore
    assert {p["location_id"] for p in plan["protected"]} == {  # type: ignore[union-attr]
        "product_objects", "product_workspace", "release_test_evidence", "job_artifacts"}
    assert plan["estimated_reclaimable_bytes"] == 400
    assert plan["deletes"] is False and plan["authority"] == "NONE" and plan["plan_only"] is True
    assert plan["execute"] == {"available": False, "reason": "NO_GOVERNED_REVERSIBLE_CLEANUP_PATH",
                               "guidance": "OWNER_AUTHORIZATION_REQUIRED"}
    for path in (runtime / "cache" / "f0.bin", runtime / "artifacts" / "jobs" / "f0.bin"):
        assert path.exists()  # planning deleted nothing


def test_recent_cache_has_nothing_due(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "fresh.bin").write_bytes(b"x")
    location = StorageLocation("runtime_cache", StorageCategory.CACHE, (DataKind.CACHE_ENTRY,), cache)
    plan = cleanup_plan.plan_cleanup([location], account_storage([location]).locations)
    assert plan["candidates"] == [] and plan["nothing_due"][0]["reason"] == "NOTHING_PAST_RETENTION_WINDOW"  # type: ignore
