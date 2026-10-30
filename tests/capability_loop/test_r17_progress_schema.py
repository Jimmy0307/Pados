# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [19][38]: persisted progress rows have one semantic schema, enforced on BOTH write and read.

A row of ``{}`` or ``session_status="anything"`` used to pass the flat-scalar check, so a tampered store loaded
as healthy and a caller could append a row the reader would later reject (permanently corrupting the store).
The schema accepts exactly the rows PADOS writes: the legacy main-branch row (no work contract/classification)
and the current row (both present), with assessment fields coherent (both or neither).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pados.capability_loop.receipts import OperationKind, OperationResult
from pados.capability_loop.store import STORE_SCHEMA, LoopStore, LoopStoreError

SESSION = "job-1"


def _current_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "project_id": "proj-a", "work_contract_id": "wc-1", "capability_gap_ref": "gap:cad_edit",
        "classification": "INTERNAL", "provider_id": "openmaic.adaptive_learning",
        "session_status": "IN_PROGRESS", "model_routing": "EXTERNAL_PROVIDER_CONTROLLED", "progress_percent": 20}
    row.update(overrides)
    return row


def _legacy_row(**overrides: Any) -> dict[str, Any]:
    row = _current_row(**overrides)
    row.pop("work_contract_id")
    row.pop("classification")
    return row


def _write_store(path: Path, progress: dict[str, Any]) -> None:
    path.write_text(json.dumps({"schema_version": STORE_SCHEMA, "receipts": [], "candidates": {},
                                "progress": progress}), encoding="utf-8")


def _receipt_fields() -> dict[str, Any]:
    return {"operation": OperationKind.LEARN_SESSION_STATE, "project_id": "proj-a", "request_digest": "a" * 64,
            "provider_id": "openmaic.adaptive_learning", "capability_id": "CAD_EDIT", "policy_code": "ALLOWED",
            "result": OperationResult.RECORDED, "accepted_into_loop": True}


VALID_ROWS = {
    "current": _current_row(),
    "legacy_main_row": _legacy_row(),
    "current_with_assessment": _current_row(human_mastery="IN_PROGRESS", grants_capability=False),
    "legacy_with_assessment": _legacy_row(human_mastery="PASSED", grants_capability=False),
}

INVALID_ROWS = {
    "empty": {},
    "unknown_status": _current_row(session_status="anything"),
    "unknown_routing": _current_row(model_routing="SOMEONE"),
    "unknown_classification": _current_row(classification="TOP_SECRET_ISH"),
    "unknown_mastery": _current_row(human_mastery="GENIUS", grants_capability=False),
    "missing_provider": {k: v for k, v in _current_row().items() if k != "provider_id"},
    "missing_status": {k: v for k, v in _current_row().items() if k != "session_status"},
    "malformed_project": _current_row(project_id="../../etc"),
    "malformed_provider": _current_row(provider_id="X"),
    "malformed_gap": _current_row(capability_gap_ref="gap:ééé"),
    "malformed_work_contract": _current_row(work_contract_id=""),
    "contract_without_classification": {k: v for k, v in _current_row().items() if k != "classification"},
    "classification_without_contract": {k: v for k, v in _current_row().items() if k != "work_contract_id"},
    "mastery_without_grant_flag": _current_row(human_mastery="PASSED"),
    "grant_flag_without_mastery": _current_row(grants_capability=False),
    "learning_grants_capability": _current_row(human_mastery="PASSED", grants_capability=True),
    "percent_out_of_range": _current_row(progress_percent=101),
    "percent_negative": _current_row(progress_percent=-1),
    "percent_bool": _current_row(progress_percent=True),
    "percent_float": _current_row(progress_percent=20.0),
    "percent_missing": {k: v for k, v in _current_row().items() if k != "progress_percent"},
    "extra_key": _current_row(free_text="provider prose leaks here"),
    "status_not_string": _current_row(session_status=3),
}


@pytest.mark.parametrize("name", sorted(VALID_ROWS))
def test_valid_rows_load_and_append(tmp_path: Path, name: str) -> None:
    path = tmp_path / "loop.json"
    _write_store(path, {SESSION: VALID_ROWS[name]})
    assert LoopStore(path).progress() == {SESSION: VALID_ROWS[name]}
    LoopStore(tmp_path / "fresh.json").append(_receipt_fields(), progress=(SESSION, VALID_ROWS[name]))


@pytest.mark.parametrize("name", sorted(INVALID_ROWS))
def test_tampered_or_malformed_rows_fail_the_load_closed(tmp_path: Path, name: str) -> None:
    path = tmp_path / "loop.json"
    _write_store(path, {SESSION: INVALID_ROWS[name]})
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CORRUPT"):
        LoopStore(path).progress()


@pytest.mark.parametrize("name", sorted(INVALID_ROWS))
def test_invalid_rows_are_refused_before_anything_is_written(tmp_path: Path, name: str) -> None:
    store = LoopStore(tmp_path / "loop.json")
    store.append(_receipt_fields(), progress=(SESSION, _current_row()))
    before = (tmp_path / "loop.json").read_bytes()
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CORRUPT"):
        store.append(_receipt_fields(), progress=("job-2", INVALID_ROWS[name]))
    assert (tmp_path / "loop.json").read_bytes() == before  # nothing persisted; store stays loadable
    assert len(store.receipts()) == 1


@pytest.mark.parametrize("key", ["", "../job", "job/1", "job?x", "x" * 200])
def test_non_canonical_session_keys_are_refused(tmp_path: Path, key: str) -> None:
    path = tmp_path / "loop.json"
    _write_store(path, {key: _current_row()})
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CORRUPT"):
        LoopStore(path).progress()
