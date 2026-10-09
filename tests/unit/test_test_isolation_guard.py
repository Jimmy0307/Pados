"""The tracked-evidence guard detects mutation of tracked history files."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import tracked_evidence_digests


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    evidence = tmp_path / "runtime/artifacts/report.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"status": "PASS"}')
    (tmp_path / "src.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def test_guard_detects_tracked_evidence_rewrite(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = tracked_evidence_digests(root)
    assert list(before) == ["runtime/artifacts/report.json"]
    (root / "runtime/artifacts/report.json").write_text('{"status": "REWRITTEN"}')
    assert tracked_evidence_digests(root) != before


def test_guard_ignores_source_edits_and_untracked_outputs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = tracked_evidence_digests(root)
    (root / "src.py").write_text("x = 2\n")
    (root / "runtime/artifacts/new-untracked.json").write_text("{}")
    assert tracked_evidence_digests(root) == before


def test_guard_exclusion_is_explicit_and_narrow(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("PADOS_TEST_EVIDENCE_GUARD_EXCLUDE", "runtime/artifacts/gates/")
    assert "runtime/artifacts/report.json" in tracked_evidence_digests(root)
    monkeypatch.setenv("PADOS_TEST_EVIDENCE_GUARD_EXCLUDE", "runtime/artifacts/")
    assert tracked_evidence_digests(root) == {}
