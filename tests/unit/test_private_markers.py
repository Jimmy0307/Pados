"""The leak-marker helper loads private patterns only from the untracked marker file and never echoes them."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.private_markers import leaked_markers, private_marker_patterns


def test_generic_tokens_are_checked_without_a_marker_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADOS_PRIVATE_MARKERS_FILE", raising=False)
    assert private_marker_patterns() == ()
    assert leaked_markers("orchestrated by Gas City") == ["generic:0"]
    assert leaked_markers("a neutral sentence") == []


def test_private_patterns_come_from_the_configured_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    markers = tmp_path / "markers.json"
    markers.write_text(json.dumps({"example_org": r"\bexample-org\b"}), encoding="utf-8")
    monkeypatch.setenv("PADOS_PRIVATE_MARKERS_FILE", str(markers))
    assert leaked_markers("built for EXAMPLE-ORG") == ["private:0"]  # case-insensitive; the match is not echoed
    monkeypatch.setenv("PADOS_PRIVATE_MARKERS_FILE", str(tmp_path / "missing.json"))
    assert private_marker_patterns() == ()
