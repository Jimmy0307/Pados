"""`pados storage report`: storage visibility by category, recommendation only."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pados.cli.main import app


def test_storage_report_cli_is_recommendation_only(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("x" * 100, encoding="utf-8")
    result = CliRunner().invoke(app, ["storage", "report", "--runtime-root", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["recommendation_only"] is True and report["autonomous_deletion"] is False
    assert {"AUTHORITY_METADATA", "OPERATIONAL_LOGS", "HERDR_TELEMETRY", "CACHE"} <= set(report["categories"])
    assert report["categories"]["OPERATIONAL_LOGS"]["bytes"] >= 100
    assert "a.log" not in result.output  # counts and bytes only, never file names
    assert (tmp_path / "logs" / "a.log").exists()


def test_storage_report_cli_text_summary(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["storage", "report", "--runtime-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "recommendation_only=True autonomous_deletion=False" in result.output
