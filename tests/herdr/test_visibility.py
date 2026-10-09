"""herdr_visibility: read-only Mission Control / doctor block; never collects or executes Herdr."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pados.herdr.config import HerdrConfig, load_config
from pados.herdr.visibility import herdr_visibility
from tests.herdr.herdr_support import fresh_fixture, write_config

_COMMON = {"semantic_authority": "NONE", "projection_only": True, "meaning_scope": "TELEMETRY_ONLY",
           "terminal_content": "NOT_PERSISTED", "herdr_live_uat": "PENDING"}


def _config(path: Path) -> HerdrConfig:
    config = load_config(str(path))
    assert isinstance(config, HerdrConfig)
    return config


@pytest.fixture(autouse=True)
def _never_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("visibility must never execute anything")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def test_not_configured_when_unset() -> None:
    view = herdr_visibility("")
    assert view["status"] == "HERDR_NOT_CONFIGURED" and view["agents"] == []
    assert {k: view[k] for k in _COMMON} == _COMMON


def test_invalid_config_is_reported_not_raised(tmp_path: Path) -> None:
    view = herdr_visibility(str(write_config(tmp_path, machine_ref="workstation-01")))
    assert view["status"] == "HERDR_CONFIG_INVALID" and view["reason"] == "MACHINE_REF_INVALID"
    assert view["semantic_authority"] == "NONE" and view["agents"] == []


def test_disabled_config_reports_disabled(tmp_path: Path) -> None:
    view = herdr_visibility(str(write_config(tmp_path, enabled=False)))
    assert view["status"] == "HERDR_DISABLED" and view["agents"] == []


def test_configured_but_not_yet_collected_and_read_only(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    view = herdr_visibility(str(path))
    assert view["status"] == "HERDR_NOT_OBSERVED" and view["source_installed"] is True
    assert view["source_runtime"] == "NOT_COLLECTED" and view["latest_collection"] is None
    assert not (tmp_path / "herdr").exists()  # never creates the store or the digest key


def test_observed_agents_are_projected_as_telemetry(tmp_path: Path) -> None:
    path = write_config(tmp_path, fixture_path=str(fresh_fixture(tmp_path, "multi_session.json")))
    assert _config(path).collect().value == "COLLECTED"
    store_file = tmp_path / "herdr" / "telemetry.json"
    before = store_file.read_bytes()
    view = herdr_visibility(str(path))
    assert view["status"] == "HERDR_OBSERVED" and view["agents_total"] == 4 and view["agents_live"] == 4
    assert view["source_runtime"] == "REACHABLE" and view["latest_collection"]["outcome"] == "COLLECTED"
    assert sorted(agent["state"] for agent in view["agents"]) == ["BLOCKED", "EXITED", "IDLE", "WORKING"]
    assert all(agent["semantic_authority"] == "NONE" for agent in view["agents"])
    assert store_file.read_bytes() == before


def test_old_source_snapshot_is_never_live(tmp_path: Path) -> None:
    old = datetime.now(UTC) - timedelta(minutes=10)
    path = write_config(tmp_path, stale_after_seconds=60,
                        fixture_path=str(fresh_fixture(tmp_path, "multi_session.json", old)))
    _config(path).collect()
    view = herdr_visibility(str(path))
    assert view["agents_live"] == 0 and view["agents_stale"] == 4
    assert {agent["state"] for agent in view["agents"]} == {"UNKNOWN"}
    assert {agent["stale_reason"] for agent in view["agents"]} == {"SOURCE_TIME_UNPROVEN"}
    assert view["latest_collection"]["code"] == "HERDR_SOURCE_TIME_TOO_OLD"


def test_removed_fixture_source_is_unavailable_and_everything_stale(tmp_path: Path) -> None:
    source = fresh_fixture(tmp_path, "multi_session.json")
    path = write_config(tmp_path, fixture_path=str(source))
    _config(path).collect()
    source.unlink()
    view = herdr_visibility(str(path))
    assert view["status"] == "HERDR_UNAVAILABLE" and view["source_installed"] is False
    assert view["agents_total"] == 4 and view["agents_live"] == 0
    assert {agent["stale_reason"] for agent in view["agents"]} == {"SOURCE_NOT_INSTALLED"}


def test_command_source_without_herdr_installed_is_unavailable(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    path = write_config(tmp_path, source="command", fixture_path=None)
    view = herdr_visibility(str(path))
    assert view["status"] == "HERDR_UNAVAILABLE" and view["source"] == "command"
    assert view["source_installed"] is False and view["agents"] == []
