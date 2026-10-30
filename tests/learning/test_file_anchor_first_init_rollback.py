# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [25]: a failed FIRST anchor initialization is rolled back, not left headless forever.

``FileAnchor.record()`` creates ``anchor.initialized`` before writing ``anchor.head.json``. If that first head
write fails before the replace lands, the sentinel without a head reads as ``ANCHOR_HEAD_LOST`` on every later
call, permanently disabling every log anchored by it. The incomplete initialization must be undone so the anchor
is uninitialized again (and a retry can succeed). A head that DID land (replace succeeded, directory fsync then
failed) is kept: that commit is real and the caller reconciles against it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pados.learning import anchor as anchor_module
from pados.learning.anchor import AnchorError, Checkpoint, FileAnchor

FIRST = Checkpoint(1, "a" * 64)


def test_failed_first_head_write_rolls_back_the_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = FileAnchor(tmp_path / "anchor")

    def failing_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated head write failure")

    monkeypatch.setattr(anchor_module, "_replace_state", failing_replace)
    with pytest.raises(OSError, match="simulated head write failure"):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    assert anchor.latest() is None  # uninitialized again, NOT ANCHOR_HEAD_LOST
    anchor.record(None, FIRST)  # and a retry initializes normally
    assert anchor.latest() == FIRST


def test_failed_sentinel_write_rolls_back_the_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    assert anchor.latest() is None  # creates the directory first, so the only fsyncs counted are record()'s
    real_fsync = anchor_module.os.fsync
    calls = {"n": 0}

    def fail_first_fsync(descriptor: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the sentinel's own fsync
            raise OSError("simulated sentinel fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(anchor_module.os, "fsync", fail_first_fsync)
    with pytest.raises(OSError, match="simulated sentinel fsync failure"):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    assert anchor.latest() is None
    anchor.record(None, FIRST)
    assert anchor.latest() == FIRST


def test_landed_first_head_with_failed_directory_fsync_is_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    assert anchor.latest() is None
    real = anchor_module.fsync_directory
    calls = {"n": 0}

    def fail_after_replace(path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1: sentinel directory fsync, 2: after the head replace
            raise OSError("simulated directory fsync failure")
        real(path)

    monkeypatch.setattr(anchor_module, "fsync_directory", fail_after_replace)
    with pytest.raises(OSError, match="simulated directory fsync failure"):
        anchor.record(None, FIRST)
    monkeypatch.undo()

    assert anchor.latest() == FIRST  # the replace landed: the commit is real, never un-done


def test_externally_deleted_head_still_fails_closed(tmp_path: Path) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    anchor.record(None, FIRST)
    (tmp_path / "anchor" / "anchor.head.json").unlink()
    with pytest.raises(AnchorError, match="ANCHOR_HEAD_LOST"):
        anchor.latest()
