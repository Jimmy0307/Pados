# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [47]: a failed FIRST JSONL append closes its descriptor before the rollback unlink.

Windows refuses to delete a file that still has an open descriptor, so unlinking inside the ``try`` (before the
``finally`` closes it) raises ``PermissionError``, masks the original failure and leaves a visible partial or
uncommitted file. The order is asserted directly so the test is meaningful on POSIX too.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pados.learning import store as learning_store
from pados.learning.store import JsonlFile


@pytest.mark.parametrize("failing", ["write", "fsync"])
def test_failed_first_append_closes_descriptor_before_unlinking(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    target = tmp_path / "store" / "log.jsonl"
    log = JsonlFile(target)
    log.stat()  # create the owner-only directory first: on POSIX that fsyncs the parent, before the patch
    events: list[str] = []
    real_close, real_unlink = os.close, Path.unlink

    def boom(*_args: object) -> int:
        raise OSError(f"simulated {failing} failure")

    def recording_close(descriptor: int) -> None:
        events.append("close")
        real_close(descriptor)

    def recording_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == target:
            events.append("unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(learning_store.os, failing, boom)
    monkeypatch.setattr(learning_store.os, "close", recording_close)
    monkeypatch.setattr(Path, "unlink", recording_unlink)

    with pytest.raises(OSError, match=f"simulated {failing} failure"):
        log.append_line({"a": 1})

    assert "unlink" in events, "the uncommitted first file must be rolled back"
    assert events.index("close") < events.index("unlink"), "descriptor must be closed before unlink"
    assert not target.exists()


def test_existing_file_append_failure_is_left_to_the_owning_protocol(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "store" / "log.jsonl"
    log = JsonlFile(target)
    log.append_line({"a": 1})

    def boom(_descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(learning_store.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated fsync failure"):
        log.append_line({"a": 2})
    # Only a file THIS call created is unlinked; an existing log is reconciled by its owner.
    assert target.exists()
