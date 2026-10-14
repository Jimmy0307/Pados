# SPDX-License-Identifier: AGPL-3.0-only
"""Round-6: binding record sizing, normalized write errors, store-row validation, anchor reconciliation."""
from __future__ import annotations

from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import (
    AnchoredSessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.capability_loop.receipts import OperationKind, OperationResult
from pados.capability_loop.store import LoopStore, LoopStoreError
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import AnchorError, Checkpoint, continues

MAX_ID = "a" + "b" * 95        # 96 chars, the _ID maximum
MAX_SESSION = "a" + "c" * 127  # 128 chars, the session-ref maximum


def _binding(provider: str = MAX_ID, session: str = MAX_SESSION) -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=provider, external_session_ref=session, project_id=MAX_ID, work_contract_id=MAX_ID,
        classification=DataClassification.RESTRICTED, capability_gap_ref=MAX_ID,
        created_at="2026-10-14T00:00:00+00:00")


# -- R6-F1: record sizing + normalized write errors ------------------------
def test_contract_maximum_binding_is_accepted(tmp_path: Path) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    auth.bind(_binding())  # must not raise DataMinimizationViolation
    assert auth.resolve(MAX_ID, MAX_SESSION) is not None


def test_persistence_error_is_normalized_to_authority_error(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auth._file, "append_line", boom)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.bind(_binding(provider="openmaic.adaptive_learning", session="job-1"))
    assert excinfo.value.code == "BINDING_LOG_WRITE_FAILED"


# -- R6-F2: the store refuses an unloadable progress row -------------------
def _receipt_fields(**over: object) -> dict:
    base = {"operation": OperationKind.LEARN_SESSION_STATE, "project_id": "proj-a",
            "request_digest": "a" * 64, "provider_id": "openmaic.adaptive_learning",
            "capability_id": "CAD_EDIT", "policy_code": "ALLOWED", "policy_decision_hash": None,
            "result": OperationResult.RECORDED, "failure": None, "evidence_digests": (), "limitations": (),
            "accepted_into_loop": True, "state_before": None, "state_after": None}
    base.update(over)
    return base


def test_append_refuses_an_invalid_progress_row(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loop.json")
    store.append(_receipt_fields(), progress=("job-x", {"progress_percent": 50}))  # valid
    with pytest.raises(LoopStoreError):
        store.append(_receipt_fields(), progress=("job-y", {"progress_percent": 999}))  # out of range
    # the store was not corrupted and still loads the one valid row
    assert len(store.receipts()) == 1
    assert "job-y" not in store.progress()


# -- R6-F3: reconcile an ambiguous anchor commit ---------------------------
class _FlakyAnchor:
    """record() commits the checkpoint, then raises (os.replace succeeded but a following fsync failed)."""

    def __init__(self) -> None:
        self._latest: Checkpoint | None = None

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        if previous != self._latest:
            raise AnchorError("ANCHOR_CONFLICT")
        if not continues(previous, new):
            raise AnchorError("ANCHOR_NOT_CONTINUING")
        self._latest = new  # committed
        raise OSError("fsync failed after replace")  # then raised

    def latest(self) -> Checkpoint | None:
        return self._latest

    def markers(self) -> tuple[Checkpoint, ...]:
        return ()


class _FailingAnchor(_FlakyAnchor):
    """record() raises WITHOUT committing."""

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        raise OSError("anchor write failed")


def test_anchor_committed_then_raised_is_reconciled_as_success(tmp_path: Path) -> None:
    auth = AnchoredSessionBindingAuthority(tmp_path / "binding.jsonl", _FlakyAnchor())
    auth.bind(_binding(provider="openmaic.adaptive_learning", session="job-1"))  # reconciles to success
    # log and anchor are consistent: the binding is resolvable.
    assert auth.resolve("openmaic.adaptive_learning", "job-1") is not None


def test_anchor_raised_without_commit_rolls_back_and_fails_closed(tmp_path: Path) -> None:
    auth = AnchoredSessionBindingAuthority(tmp_path / "binding.jsonl", _FailingAnchor())
    with pytest.raises(BindingAuthorityError):
        auth.bind(_binding(provider="openmaic.adaptive_learning", session="job-1"))
    # the log was rolled back: nothing persisted, and the store still validates clean.
    assert auth.resolve("openmaic.adaptive_learning", "job-1") is None
