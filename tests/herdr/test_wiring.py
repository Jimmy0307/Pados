"""Integrator wiring: PADOS_HERDR_CONFIG setting, doctor/Mission Control block, storage accounting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pados.cli.main import app
from pados.config import Settings, get_settings
from pados.herdr.config import HERDR_NOT_CONFIGURED
from pados.herdr.visibility import herdr_visibility
from tests.herdr.herdr_support import write_config


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.delenv("PADOS_HERDR_CONFIG", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_herdr_is_off_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.herdr_config == ""
    assert herdr_visibility(settings.herdr_config)["status"] == HERDR_NOT_CONFIGURED


def test_storage_report_measures_the_configured_herdr_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_config(tmp_path)
    store = Path(json.loads(config.read_text(encoding="utf-8"))["store_path"])
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PADOS_HERDR_CONFIG", str(config))
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["storage", "report", "--runtime-root", str(tmp_path / "rt"), "--json"])
    assert result.exit_code == 0, result.output
    herdr = json.loads(result.output)["categories"]["HERDR_TELEMETRY"]
    assert herdr["bytes"] >= 2 and herdr["status"] != "NOT_CONFIGURED"
