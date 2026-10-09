"""Optional Herdr config: NOT_CONFIGURED when unset, CONFIG_INVALID as a whole, closed schema."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pados.herdr.adapter import CommandHerdrSource, FixtureHerdrSource, HerdrBinding
from pados.herdr.config import (
    HERDR_CONFIG_INVALID,
    HERDR_NOT_CONFIGURED,
    MAX_CONFIG_BYTES,
    HerdrConfig,
    HerdrSourceKind,
    load_config,
)
from tests.herdr.herdr_support import MACHINE, write_config


def test_unset_config_is_not_configured() -> None:
    assert load_config("") == {"status": HERDR_NOT_CONFIGURED}


def test_valid_fixture_config_builds_adapter_and_store(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    assert isinstance(config, HerdrConfig)
    assert config.enabled and config.source_kind is HerdrSourceKind.FIXTURE and config.machine_ref == MACHINE
    assert isinstance(config.source(), FixtureHerdrSource)
    assert config.adapter().machine_ref == MACHINE
    assert config.store().max_records == 100 and config.store_root == tmp_path / "herdr"
    assert config.store().key_path.is_file()  # the digest key is created on first adapter use


def test_bindings_are_validated_operator_refs(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, bindings={
        "billing-service": {"project_ref": "proj-billing", "work_package_ref": "wp-0042"},
        "docs": {"project_ref": "proj-docs"}})))
    assert isinstance(config, HerdrConfig)
    assert dict(config.bindings)["billing-service"] == HerdrBinding("proj-billing", "wp-0042")
    assert dict(config.bindings)["docs"].work_package_ref is None
    for bindings, reason in (
            ([], "BINDINGS_INVALID"),
            ({"t": "proj-x"}, "BINDINGS_INVALID"),
            ({"t": {"work_package_ref": "wp-1"}}, "BINDINGS_INVALID"),
            ({"t": {"project_ref": "proj-x", "label": "x"}}, "BINDINGS_INVALID"),
            ({"t": {"project_ref": "has space"}}, "HERDR_BINDING_REF_INVALID"),
            ({"": {"project_ref": "proj-x"}}, "BINDINGS_INVALID")):
        assert load_config(str(write_config(tmp_path, bindings=bindings))) == {
            "status": HERDR_CONFIG_INVALID, "reason": reason}


def test_command_config_defines_but_never_runs_the_command(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, source="command", fixture_path=None, command_timeout_seconds=2)))
    assert isinstance(config, HerdrConfig) and config.fixture_path is None
    source = config.source()
    assert isinstance(source, CommandHerdrSource) and source.timeout_seconds == 2.0


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"machine_ref": "workstation-01"}, "MACHINE_REF_INVALID"),
    ({"machine_ref": None}, "MACHINE_REF_INVALID"),
    ({"source": "ssh"}, "SOURCE_INVALID"),
    ({"enabled": "yes"}, "ENABLED_INVALID"),
    ({"store_path": None}, "STORE_PATH_INVALID"),
    ({"fixture_path": None}, "FIXTURE_PATH_INVALID"),
    ({"stale_after_seconds": 0}, "HERDR_STALE_AFTER_SECONDS_INVALID"),
    ({"stale_after_seconds": "60"}, "STALE_AFTER_SECONDS_INVALID"),
    ({"max_records": 10**6}, "HERDR_STORE_MAX_RECORDS_OUT_OF_RANGE"),
    ({"max_bytes": 1}, "HERDR_STORE_MAX_BYTES_OUT_OF_RANGE"),
    ({"stale_after_seconds": 86_401}, "HERDR_STALE_AFTER_SECONDS_INVALID"),
    ({"source": "command", "command_timeout_seconds": 600}, "HERDR_COMMAND_TIMEOUT_INVALID"),
    ({"command_timeout_seconds": "5"}, "COMMAND_TIMEOUT_SECONDS_INVALID"),
    ({"command": "herdr status --json; id"}, "CONFIG_UNKNOWN_KEY"),
    ({"pass" + "word": "hunter2-not-real"}, "CONFIG_UNKNOWN_KEY"),
])
def test_invalid_values_make_the_whole_config_invalid(tmp_path: Path, overrides: dict[str, object],
                                                      reason: str) -> None:
    result = load_config(str(write_config(tmp_path, **overrides)))
    assert result == {"status": HERDR_CONFIG_INVALID, "reason": reason}


@pytest.mark.parametrize(("content", "reason"), [
    (b"{not json", "CONFIG_NOT_JSON"),
    (b"[]", "CONFIG_NOT_AN_OBJECT"),
    (b" " * (MAX_CONFIG_BYTES + 1), "CONFIG_TOO_LARGE"),
], ids=["malformed-json", "non-object-json", "oversized-json"])
def test_unparseable_config_is_invalid(tmp_path: Path, content: bytes, reason: str) -> None:
    path = tmp_path / "herdr.json"
    path.write_bytes(content)
    assert load_config(str(path)) == {"status": HERDR_CONFIG_INVALID, "reason": reason}


def test_missing_config_file_is_invalid_not_silently_enabled(tmp_path: Path) -> None:
    assert load_config(str(tmp_path / "absent.json")) == {"status": HERDR_CONFIG_INVALID,
                                                          "reason": "CONFIG_UNREADABLE"}


def test_disabled_is_the_default(tmp_path: Path) -> None:
    path = tmp_path / "herdr.json"
    path.write_text(json.dumps({"store_path": str(tmp_path / "t.json"), "machine_ref": MACHINE,
                                "fixture_path": str(tmp_path / "f.json")}), encoding="utf-8")
    config = load_config(str(path))
    assert isinstance(config, HerdrConfig) and config.enabled is False
