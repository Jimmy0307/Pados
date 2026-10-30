# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R18 (independent review): an interrupted commit is recovered, never a permanent brick.

The binding log is appended (and fsynced) before the independent anchor advances. A process killed between the
two, a ``BaseException`` escaping the anchor write, a torn (newline-less) trailing write, or a rollback that
itself fails all used to leave the log exactly one record ahead of the anchor: every later operation then failed
with ``BINDING_HEAD_NOT_ANCHORED`` / ``BINDING_LOG_CORRUPT`` forever, disabling every committed session.

Recovery is anchor-authenticated and only ever DISCARDS: when the anchored checkpoint is exactly the log's
penultimate record (or the head of the complete lines before a torn tail), the single unanchored tail is
truncated. It is never anchored after the fact -- that would let a log writer launder a forged transition (for
example a COMMITTED record finalizing a pending binding without evidence). Anything else still fails closed.
These tests crash INSIDE the authority (not by replacing its public methods), which is what the earlier R17
tests did not do.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import (
    AnchoredSessionBindingAuthority,
    _BindingRecord,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import AnchorError, FileAnchor

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str, project: str = "proj-a") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id=project, work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "binding-log" / "binding.jsonl", tmp_path / "binding-anchor"


def _open(tmp_path: Path) -> AnchoredSessionBindingAuthority:
    log, anchor = _paths(tmp_path)
    return anchored_session_binding_authority(log, anchor)


def _committed_a(tmp_path: Path) -> AnchoredSessionBindingAuthority:
    authority = _open(tmp_path)
    authority.stage(_binding("job-a"))
    authority.commit(_binding("job-a"), evidence_ref="a" * 64)
    return authority


def _assert_healthy(tmp_path: Path) -> None:
    restarted = _open(tmp_path)  # a new process over the same durable files
    assert restarted.resolve(PROVIDER, "job-a") is not None  # committed sessions keep authorizing
    assert restarted.resolve(PROVIDER, "job-b") is None  # the interrupted transition never became authority
    restarted.stage(_binding("job-c"))  # and the authority accepts new work
    restarted.commit(_binding("job-c"), evidence_ref="c" * 64)
    assert restarted.resolve(PROVIDER, "job-c") is not None


@pytest.mark.parametrize("killer", [SystemExit, KeyboardInterrupt])
def test_process_killed_between_log_append_and_anchor_advance_recovers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, killer: type[BaseException]) -> None:
    authority = _committed_a(tmp_path)

    def killed(*_args: Any, **_kwargs: Any) -> None:
        raise killer("process killed after the log append, before the anchor compare-and-set")

    monkeypatch.setattr(FileAnchor, "record", killed)
    with pytest.raises(killer):
        authority.stage(_binding("job-b"))
    monkeypatch.undo()
    _assert_healthy(tmp_path)


def test_interrupted_finalize_never_becomes_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The crashed transition is a COMMITTED record for a pending stage: it must be discarded, not anchored.
    authority = _committed_a(tmp_path)
    authority.stage(_binding("job-b"))

    def killed(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit("killed mid-finalize")

    monkeypatch.setattr(FileAnchor, "record", killed)
    with pytest.raises(SystemExit):
        authority.commit(_binding("job-b"), evidence_ref="b" * 64)
    monkeypatch.undo()
    _assert_healthy(tmp_path)
    assert _open(tmp_path).commit(_binding("job-b"), evidence_ref="b" * 64)  # still pending: can finalize again
    assert _open(tmp_path).resolve(PROVIDER, "job-b") is not None


def test_torn_trailing_write_recovers(tmp_path: Path) -> None:
    _committed_a(tmp_path)
    log, _ = _paths(tmp_path)
    before = log.read_bytes()
    with log.open("ab") as handle:
        handle.write(b'{"seq": 3, "state": "STA')  # a crash mid-write: no terminating newline
    _assert_healthy(tmp_path)
    assert log.read_bytes().startswith(before)


def test_failed_rollback_after_refused_anchor_recovers_on_next_operation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _committed_a(tmp_path)

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise AnchorError("ANCHOR_CONFLICT")

    def rollback_denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "sharing violation (scanner holds the file)")

    monkeypatch.setattr(FileAnchor, "record", refused)
    monkeypatch.setattr(authority._file, "rewrite", rollback_denied)
    with pytest.raises(BindingAuthorityError, match="BINDING_LOG_WRITE_FAILED"):
        authority.stage(_binding("job-b"))
    monkeypatch.undo()
    _assert_healthy(tmp_path)


def test_more_than_one_unanchored_record_still_fails_closed(tmp_path: Path) -> None:
    # A single crash can leave at most ONE unanchored record; two means tampering -> never auto-repaired.
    _committed_a(tmp_path)
    log, _ = _paths(tmp_path)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    for session in ("job-x", "job-y"):
        record = _BindingRecord.seal(_binding(session), seq=len(rows) + 1, prev_hash=rows[-1]["entry_hash"])
        rows.append(record.model_dump(mode="json"))
    log.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(BindingAuthorityError, match="BINDING_HEAD_NOT_ANCHORED"):
        _open(tmp_path).resolve(PROVIDER, "job-a")


def test_unanchored_tail_that_breaks_the_chain_still_fails_closed(tmp_path: Path) -> None:
    _committed_a(tmp_path)
    log, _ = _paths(tmp_path)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    forged = _BindingRecord.seal(_binding("job-x"), seq=len(rows) + 1, prev_hash="f" * 64)
    rows.append(forged.model_dump(mode="json"))
    log.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(BindingAuthorityError, match="BINDING_LOG_CHAIN_BROKEN"):
        _open(tmp_path).resolve(PROVIDER, "job-a")


def test_anchored_record_is_never_truncated(tmp_path: Path) -> None:
    # Substituting the ANCHORED head (same length chain) is tampering, not an interrupted commit.
    _committed_a(tmp_path)
    log, _ = _paths(tmp_path)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    rows[-1] = _BindingRecord.seal(_binding("job-evil"), seq=rows[-1]["seq"],
                                   prev_hash=rows[-1]["prev_hash"]).model_dump(mode="json")
    log.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(BindingAuthorityError, match="BINDING_HEAD_NOT_ANCHORED"):
        _open(tmp_path).resolve(PROVIDER, "job-a")
