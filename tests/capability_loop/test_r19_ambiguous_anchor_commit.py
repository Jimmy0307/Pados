# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 Codex review of 77f2c87 [binding.py:382]: an ambiguous anchor commit never deletes the log record.

``record()`` can advance the anchor and then raise. If the follow-up ``latest()`` also fails, nothing establishes
whether the anchor moved. Rolling the log back then risks an anchor AHEAD of the log, which no recovery can repair
(every binding disabled). The appended record is therefore kept and the operation reports unavailability; the next
operation that can read the anchor sees one of two consistent states: the anchor advanced (log matches) or it did
not (the single unanchored tail is discarded by recovery, preserved in the ``.discarded`` sidecar).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import AnchoredSessionBindingAuthority
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import Checkpoint, FileAnchor

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str) -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id="proj-a", work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


class _AmbiguousAnchor:
    """A real FileAnchor whose next record() may land and then raise, with the next latest() read failing."""

    def __init__(self, directory: Path) -> None:
        self.real = FileAnchor(directory)
        self.ambiguous = False
        self.commit_lands = True
        self.reads_failing = 0

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        if not self.ambiguous:
            self.real.record(previous, new, marker=marker)
            return
        self.ambiguous = False
        if self.commit_lands:
            self.real.record(previous, new, marker=marker)
        self.reads_failing = 1
        raise OSError("directory fsync failed after the replace")

    def latest(self) -> Checkpoint | None:
        if self.reads_failing:
            self.reads_failing -= 1
            raise OSError("anchor temporarily unreadable")
        return self.real.latest()

    def markers(self) -> tuple[Checkpoint, ...]:
        return self.real.markers()


def _authority(tmp_path: Path) -> tuple[AnchoredSessionBindingAuthority, _AmbiguousAnchor, Path]:
    anchor = _AmbiguousAnchor(tmp_path / "anchor")
    log = tmp_path / "log" / "binding.jsonl"
    authority = AnchoredSessionBindingAuthority(log, anchor)
    authority.stage(_binding("job-a"))
    authority.commit(_binding("job-a"), evidence_ref="a" * 64)
    return authority, anchor, log


def test_landed_commit_with_unreadable_anchor_keeps_the_record(tmp_path: Path) -> None:
    authority, anchor, log = _authority(tmp_path)
    lines_before = len(log.read_text(encoding="utf-8").splitlines())
    anchor.ambiguous, anchor.commit_lands = True, True
    with pytest.raises(BindingAuthorityError, match="BINDING_ANCHOR_UNAVAILABLE"):
        authority.stage(_binding("job-b"))
    assert len(log.read_text(encoding="utf-8").splitlines()) == lines_before + 1  # never deleted while ambiguous
    # Once the anchor is readable it is exactly at the kept record: consistent, nothing lost, nothing bricked.
    assert authority.resolve(PROVIDER, "job-a") is not None
    authority.commit(_binding("job-b"), evidence_ref="b" * 64)
    assert authority.resolve(PROVIDER, "job-b") is not None


def test_unlanded_commit_with_unreadable_anchor_is_recovered_later(tmp_path: Path) -> None:
    authority, anchor, log = _authority(tmp_path)
    anchor.ambiguous, anchor.commit_lands = True, False
    with pytest.raises(BindingAuthorityError, match="BINDING_ANCHOR_UNAVAILABLE"):
        authority.stage(_binding("job-b"))
    # The anchor did not move: the next readable operation discards (and preserves) the single unanchored tail.
    assert authority.resolve(PROVIDER, "job-a") is not None
    assert authority.resolve(PROVIDER, "job-b") is None
    assert log.with_name(log.name + ".discarded").exists()
    authority.stage(_binding("job-b"))
    authority.commit(_binding("job-b"), evidence_ref="b" * 64)
    assert authority.resolve(PROVIDER, "job-b") is not None
