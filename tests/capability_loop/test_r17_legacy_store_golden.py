# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [5][11]: a LoopStore written by the PRE-UPGRADE code still verifies after the upgrade.

The earlier compatibility test built its "legacy" projection from the new model, so it could not catch a change
to the legacy hash. ``fixtures/legacy_main_464b385_loop_store.json`` was written by ``origin/main`` @ 464b385's
own ``LoopStore`` (before ``binding_digest`` / ``session_ref`` existed); the golden hashes below are what that
code computed. The upgraded code must reproduce them exactly, keep verifying the chain across mixed old/new
receipts and restarts, and still detect a tampered legacy row.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pados.capability_loop.receipts import OperationKind, OperationResult, verify_chain
from pados.capability_loop.store import LoopStore, LoopStoreError

FIXTURE = Path(__file__).with_name("fixtures") / "legacy_main_464b385_loop_store.json"
# receipt_hash of each legacy receipt as computed by origin/main @ 464b385 (full model_dump, old schema).
MAIN_HASHES = (
    "e89840e21acaaef82d52120f7fe338de6d98b91f1c9b0eba77f2a058e780bc64",
    "cfc5055c3b5fa89309c2e860c1469fb8411cacf0b7e53da634ad68c0ff070796",
    "13bd6d76df6b7fb956b939f974ca079a2a92acd6985668fcc2650c406cde8b86",
)


def _legacy_copy(tmp_path: Path) -> Path:
    target = tmp_path / "loop.json"
    shutil.copyfile(FIXTURE, target)
    return target


def test_fixture_is_genuinely_pre_upgrade() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["receipts"]
    assert all("binding_digest" not in row and "session_ref" not in row for row in rows)
    assert [row["previous_receipt_hash"] for row in rows[1:]] == list(MAIN_HASHES[:-1])


def test_every_legacy_receipt_hashes_exactly_as_main_computed(tmp_path: Path) -> None:
    receipts = LoopStore(_legacy_copy(tmp_path)).receipts()
    assert tuple(receipt.receipt_hash for receipt in receipts) == MAIN_HASHES  # single and chained
    assert verify_chain(receipts)


def test_legacy_progress_row_still_loads(tmp_path: Path) -> None:
    progress = LoopStore(_legacy_copy(tmp_path)).progress()
    assert progress["job-legacy"]["session_status"] == "REQUESTED"


def test_new_receipts_chain_onto_a_legacy_store_and_survive_restart(tmp_path: Path) -> None:
    path = _legacy_copy(tmp_path)
    new = LoopStore(path).append({
        "operation": OperationKind.LEARN_SESSION_STATE, "project_id": "proj-a", "request_digest": "4" * 64,
        "provider_id": "openmaic.adaptive_learning", "capability_id": "CAD_EDIT", "policy_code": "ALLOWED",
        "binding_digest": "b" * 64, "session_ref": "job-legacy", "result": OperationResult.RECORDED,
        "accepted_into_loop": True})
    assert new.previous_receipt_hash == MAIN_HASHES[-1]  # chained onto main's tail hash
    restarted = LoopStore(path).receipts()  # a fresh instance re-verifies the whole mixed chain
    assert len(restarted) == 4 and verify_chain(restarted)
    assert tuple(receipt.receipt_hash for receipt in restarted[:3]) == MAIN_HASHES
    assert restarted[-1].binding_digest == "b" * 64


@pytest.mark.parametrize("index", [0, 1])
def test_tampered_legacy_row_breaks_the_chain(tmp_path: Path, index: int) -> None:
    path = _legacy_copy(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["receipts"][index]["accepted_into_loop"] = not data["receipts"][index]["accepted_into_loop"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CHAIN_BROKEN"):
        LoopStore(path).receipts()


def test_injecting_a_new_field_into_a_legacy_row_breaks_the_chain(tmp_path: Path) -> None:
    # A non-null added field is part of the hash, so it cannot be smuggled into an already-chained legacy row.
    path = _legacy_copy(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["receipts"][0]["binding_digest"] = "f" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CHAIN_BROKEN"):
        LoopStore(path).receipts()
