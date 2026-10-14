# SPDX-License-Identifier: AGPL-3.0-only
"""File-anchored binding authority: every log/anchor inconsistency fails closed (R4-F4 crux).

A whole-file rewrite, tail or interior rewrite, anchor mismatch, truncated log, a log advanced past the
anchor (or an anchor advanced past the log, i.e. a process interrupted between the log append and the anchor
update), a corrupt log and a missing anchor are each detected and refused -- the independent anchor keeps the
trusted checkpoint so a substituted chain is never laundered. Security level is
INDEPENDENTLY_ANCHORED_TAMPER_EVIDENT, not cryptographic tamper-proof.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import (
    GENESIS,
    AnchoredSessionBindingAuthority,
    _BindingRecord,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import ANCHOR_SCHEMA

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str = "job-1", project: str = "proj-a") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id=project, work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _auth(tmp_path: Path) -> tuple[AnchoredSessionBindingAuthority, Path, Path]:
    log, anchor = tmp_path / "log" / "binding.jsonl", tmp_path / "anchor"
    return anchored_session_binding_authority(log, anchor), log, anchor


def _lines(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(log: Path, rows: list[dict]) -> None:
    log.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _tampered_row(session: str, project: str, *, seq: int, prev_hash: str) -> dict:
    return _BindingRecord.seal(_binding(session, project), seq=seq, prev_hash=prev_hash).model_dump(mode="json")


# -- baseline --------------------------------------------------------------
def test_valid_bind_resolve_and_reopen(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding())
    assert auth.resolve(PROVIDER, "job-1").project_id == "proj-a"  # type: ignore[union-attr]
    reopened = anchored_session_binding_authority(log, anchor)  # survives reopen
    assert reopened.resolve(PROVIDER, "job-1") is not None


# -- tamper detection ------------------------------------------------------
def test_whole_file_rewrite_with_recomputed_chain_is_detected(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding(project="proj-a"))
    # Rewrite the log with a self-consistent but DIFFERENT chain (proj-evil); the anchor is untouched.
    _write(log, [_tampered_row("job-1", "proj-evil", seq=1, prev_hash=GENESIS)])
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"


def test_tail_rewrite_is_detected(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    auth.bind(_binding("job-2"))
    rows = _lines(log)
    rows[-1] = _tampered_row("job-2", "proj-evil", seq=2, prev_hash=rows[0]["entry_hash"])
    _write(log, rows)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.bind(_binding("job-3"))
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"


def test_interior_record_tamper_breaks_the_chain(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    auth.bind(_binding("job-2"))
    rows = _lines(log)
    rows[0]["project_id"] = "proj-evil"  # interior field changed without resealing
    _write(log, rows)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-2")
    assert excinfo.value.code == "BINDING_LOG_CORRUPT"


def test_anchor_checkpoint_mismatch_is_detected(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    head = anchor / "anchor.head.json"
    head.write_text(json.dumps({"schema_version": ANCHOR_SCHEMA,
                                "latest": {"seq": 1, "entry_hash": "b" * 64}, "markers": []}), encoding="utf-8")
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"


def test_truncated_log_is_detected(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    auth.bind(_binding("job-2"))
    _write(log, _lines(log)[:-1])  # drop the anchored tail
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-2")
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"


# -- crash consistency / process interruption ------------------------------
def test_log_advanced_but_anchor_stale_is_detected(tmp_path: Path) -> None:
    # Simulates a crash AFTER the log append but BEFORE the anchor compare-and-set.
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    rows = _lines(log)
    rows.append(_tampered_row("job-2", "proj-a", seq=2, prev_hash=rows[0]["entry_hash"]))
    _write(log, rows)  # anchor still at seq 1
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"


def test_anchor_ahead_of_log_is_detected(tmp_path: Path) -> None:
    # Simulates the log record absent while the anchor still holds a checkpoint.
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    log.unlink()
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_ANCHOR_AHEAD_OF_LOG"


def test_missing_anchor_after_initialization_fails_closed(tmp_path: Path) -> None:
    auth, log, anchor = _auth(tmp_path)
    auth.bind(_binding("job-1"))
    (anchor / "anchor.head.json").unlink()  # head lost, sentinel remains
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_ANCHOR_UNAVAILABLE"
