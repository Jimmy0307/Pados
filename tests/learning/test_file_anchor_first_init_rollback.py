# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [25] / R18: first anchor initialization can never leave a permanently headless anchor.

The sentinel's job is to make a head that disappears AFTER initialization fail closed (``ANCHOR_HEAD_LOST``). If
the sentinel is written first, any failure or crash before the head lands leaves a sentinel without a head --
indistinguishable from a lost head -- and every log anchored by it is disabled forever; an in-process rollback
cannot cover a hard kill, and the rollback unlink can itself fail (a Windows sharing violation).

Initialization therefore writes the head first (temp + fsync + atomic replace) and the sentinel second. A head
that landed without its sentinel is an interrupted first initialization: it holds only the first checkpoint, and
the next access completes it by writing the sentinel. Nothing but the head's own atomic replace is ever needed to
undo a failure, so no rollback step can strand the anchor.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pados.learning import anchor as anchor_module
from pados.learning.anchor import AnchorError, Checkpoint, FileAnchor

FIRST = Checkpoint(1, "a" * 64)
SECOND = Checkpoint(2, "b" * 64)


@pytest.mark.parametrize("failure", [OSError("simulated head write failure"), SystemExit("killed")])
def test_failure_before_the_head_lands_leaves_the_anchor_uninitialized(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    anchor = FileAnchor(tmp_path / "anchor")

    def failing_replace(_source: Path, _destination: Path) -> None:
        raise failure

    monkeypatch.setattr(anchor_module, "_replace_state", failing_replace)
    with pytest.raises(type(failure)):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    assert not (tmp_path / "anchor" / "anchor.initialized").exists()  # no sentinel without a head
    assert anchor.latest() is None  # uninitialized, NOT ANCHOR_HEAD_LOST
    anchor.record(None, FIRST)  # and a retry initializes normally
    assert anchor.latest() == FIRST


@pytest.mark.parametrize("failure", [OSError("simulated sentinel failure"), SystemExit("killed")])
def test_head_landed_but_sentinel_missing_is_completed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    anchor = FileAnchor(tmp_path / "anchor")

    def failing_sentinel(_self: FileAnchor) -> None:
        raise failure

    monkeypatch.setattr(FileAnchor, "_create_sentinel", failing_sentinel)
    with pytest.raises(type(failure)):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    reopened = FileAnchor(tmp_path / "anchor")  # a new process
    assert reopened.latest() == FIRST  # the landed first checkpoint is real and kept
    assert (tmp_path / "anchor" / "anchor.initialized").exists()  # initialization completed on access
    reopened.record(FIRST, SECOND)
    assert reopened.latest() == SECOND


def test_landed_head_with_failed_directory_fsync_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    assert anchor.latest() is None  # create the directory first
    real = anchor_module.fsync_directory
    calls = {"n": 0}

    def fail_first(path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the directory fsync right after the head replace
            raise OSError("simulated directory fsync failure")
        real(path)

    monkeypatch.setattr(anchor_module, "fsync_directory", fail_first)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    assert anchor.latest() == FIRST  # the replace landed: the commit is real, never un-done


def test_head_without_sentinel_beyond_the_first_checkpoint_fails_closed(tmp_path: Path) -> None:
    # Only an interrupted FIRST initialization (seq 1) is completed; a later head without its sentinel means the
    # sentinel was removed after initialization and is refused, never silently re-initialized.
    anchor = FileAnchor(tmp_path / "anchor")
    anchor.record(None, FIRST)
    anchor.record(FIRST, SECOND)
    (tmp_path / "anchor" / "anchor.initialized").unlink()
    with pytest.raises(AnchorError, match="ANCHOR_STATE_INVALID"):
        anchor.latest()


def test_externally_deleted_head_still_fails_closed(tmp_path: Path) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    anchor.record(None, FIRST)
    (tmp_path / "anchor" / "anchor.head.json").unlink()
    with pytest.raises(AnchorError, match="ANCHOR_HEAD_LOST"):
        anchor.latest()
