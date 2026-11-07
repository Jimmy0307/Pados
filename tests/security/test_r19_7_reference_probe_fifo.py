# SPDX-License-Identifier: AGPL-3.0-only
"""R19-7 candidate: the Markdown reference probe must not block on a FIFO (or any non-regular file) named ``*.md``.

Opening a FIFO read-only with no writer blocks on POSIX. The probe opens each Markdown file with
``O_NOFOLLOW | O_NONBLOCK`` and reads it only when ``fstat`` says it is a regular file; anything else is reported as
unreadable and the outcome is UNRESOLVED. The probe runs in a child process under a hard timeout, so a regression
fails this test instead of hanging the suite. POSIX only; Windows has no FIFOs in the file system.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[2] / "src" / "pados" / "worker" / "probes" / "markdown_references.py"

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")


def _probe(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    try:
        done = subprocess.run([sys.executable, "-I", str(PROBE)], cwd=root, capture_output=True, text=True,
                              timeout=20, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("the reference probe blocked on a non-regular *.md file")
    return json.loads(done.stdout), json.loads(done.stderr)


def test_a_fifo_named_md_is_reported_unreadable_and_never_blocks(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("See [guide](guide.md).\n", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Back to [readme](README.md).\n", encoding="utf-8")
    os.mkfifo(tmp_path / "pipe.md")
    outcome, report = _probe(tmp_path)
    assert outcome["outcome"] == "UNRESOLVED"
    assert {"file": "pipe.md", "target": "<unreadable>"} in report["broken"]  # type: ignore[operator]
    assert report["references_checked"] == 2


def test_regular_files_still_read_normally(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("See [guide](guide.md).\n", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Back to [readme](README.md).\n", encoding="utf-8")
    outcome, report = _probe(tmp_path)
    assert outcome["outcome"] == "COMPLETE" and report["broken"] == []
