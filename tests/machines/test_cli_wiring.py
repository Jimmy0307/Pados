"""Integrator wiring: PADOS_MACHINES_CONFIG and `pados machine add/list/show/test/remove` (fake probe only)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from machine_support import FakeProbe, host_key, lan, scan_tree_for
from typer.testing import CliRunner

import pados.machines.ssh as ssh_module
from pados.cli.main import app
from pados.config import Settings, get_settings
from pados.machines.inventory import InventoryStore
from pados.machines.visibility import machine_visibility


@pytest.fixture(autouse=True)
def _settings_cache():  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_machines_are_off_by_default() -> None:
    assert Settings(_env_file=None).machines_config == ""
    view = machine_visibility("")
    assert view["plaintext_password_persistence"] == "PROHIBITED"


def test_cli_requires_an_inventory_path() -> None:
    result = CliRunner().invoke(app, ["machine", "list"])
    assert result.exit_code == 2


def test_cli_lifecycle_with_pinned_host_key(store: InventoryStore, probe: FakeProbe, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_MACHINES_CONFIG", str(store.path))
    monkeypatch.setattr(ssh_module, "OpenSshProbe", lambda **_: probe)
    address, key = lan(20), host_key("mac")
    probe.place(address, key)
    runner = CliRunner()
    added = runner.invoke(app, ["machine", "add", "--name", "Mac", "--host", address, "--user", "operator",
                                "--auth", "ssh-agent", "--platform", "MACOS", "--role", "HERDR_MACHINE",
                                "--fingerprint", key.fingerprint])
    assert added.exit_code == 0, added.output
    rows = json.loads(runner.invoke(app, ["machine", "list", "--json"]).output)
    assert [row["display_name"] for row in rows] == ["Mac"]
    assert runner.invoke(app, ["machine", "show", "Mac"]).exit_code == 0
    tested = runner.invoke(app, ["machine", "test", "Mac"])
    assert tested.exit_code == 0 and "state=ONLINE" in tested.output
    wrong = runner.invoke(app, ["machine", "add", "--name", "Other", "--host", lan(21), "--user", "operator",
                                "--fingerprint", key.fingerprint])
    assert wrong.exit_code == 1 and lan(21) not in wrong.output  # codes only, never hosts
    assert runner.invoke(app, ["machine", "remove", "Mac"]).exit_code == 0
    assert scan_tree_for(tmp_path, "operator-password") == []
