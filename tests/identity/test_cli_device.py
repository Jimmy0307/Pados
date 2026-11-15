# SPDX-License-Identifier: AGPL-3.0-only
"""``pados device pair|list|revoke`` against an in-process transport, a live loopback server, and from Windows
PowerShell 5.1 (design §3.6)."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.identity.identity_support import CONTROL_VALUE, ORIGIN, Harness

CODE_SHAPE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$")


@pytest.fixture
def credential_file(tmp_path: Path) -> Path:
    path = tmp_path / "runtime" / "local" / "control.token"
    path.parent.mkdir(parents=True)
    path.write_text(CONTROL_VALUE + "\n", encoding="utf-8")
    return path


@pytest.fixture
def in_process(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    from pados.identity import cli as device_cli

    monkeypatch.setattr(device_cli, "client_factory", lambda base: TestClient(harness.app, base_url=base))
    return harness


def _run(*args: str) -> tuple[int, str, str]:
    """Invoke the CLI. The in-process server's own log events are captured apart, so stdout is the CLI's alone
    (in production the server is another process)."""
    import structlog

    from pados.cli.main import app

    with structlog.testing.capture_logs():
        result = CliRunner().invoke(app, ["device", *args])
    return result.exit_code, result.stdout, result.stderr


def test_pair_json_prints_only_the_code_and_expiry(in_process: Harness, credential_file: Path) -> None:
    code, out, err = _run("pair", "--json", "--label", "Desk", "--token-file", str(credential_file))
    assert code == 0, err
    payload = json.loads(out)
    assert set(payload) == {"pairing_code", "expires_at"} and CODE_SHAPE.fullmatch(payload["pairing_code"])
    assert CONTROL_VALUE not in out + err and out.isascii()
    client = in_process.client()
    paired = in_process.pair(client, payload["pairing_code"], label=None)
    assert paired.status_code == 200 and paired.json()["device_label"] == "Desk"


def test_pair_human_output(in_process: Harness, credential_file: Path) -> None:
    code, out, _ = _run("pair", "--token-file", str(credential_file))
    assert code == 0 and "Pairing code: " in out and CONTROL_VALUE not in out and out.isascii()


def test_list_and_revoke(in_process: Harness, credential_file: Path) -> None:
    client = in_process.client()
    device_id = in_process.paired(client)["device_id"]
    code, out, _ = _run("list", "--json", "--token-file", str(credential_file))
    assert code == 0
    devices = json.loads(out)["devices"]
    assert [d["device_id"] for d in devices] == [device_id] and devices[0]["revoked_at"] is None
    code, out, _ = _run("revoke", device_id, "--token-file", str(credential_file))
    assert code == 0 and f"device_id={device_id}" in out
    assert client.get("/identity/v1/session").json()["authenticated"] is False
    code, _, err = _run("revoke", "dev_0123456789abcdef", "--token-file", str(credential_file))
    assert code == 1 and err.strip() == "DEVICE_NOT_FOUND"
    code, _, err = _run("revoke", "../../control/projects", "--token-file", str(credential_file))
    assert code == 1 and err.strip() == "DEVICE_NOT_FOUND"


def test_the_token_file_beside_the_identity_store_is_found(in_process: Harness, credential_file: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from pados import config

    monkeypatch.setenv("PADOS_IDENTITY_STORE_DIR", str(credential_file.parent / "identity"))
    config.get_settings.cache_clear()
    code, out, err = _run("pair", "--json")
    assert code == 0, err
    assert CODE_SHAPE.fullmatch(json.loads(out)["pairing_code"])


def test_refusals_are_closed_codes(in_process: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrong = tmp_path / "wrong.token"
    wrong.write_text("not-the-control-value\n", encoding="utf-8")
    code, out, err = _run("pair", "--json", "--token-file", str(wrong))
    assert code == 1 and out == "" and err.strip() == "FORBIDDEN"
    code, _, err = _run("pair", "--token-file", str(tmp_path / "missing.token"))
    assert code == 1 and err.strip() == "CONTROL_TOKEN_FILE_UNREADABLE"
    empty = tmp_path / "empty.token"
    empty.write_text("\n", encoding="utf-8")
    code, _, err = _run("pair", "--token-file", str(empty))
    assert code == 1 and err.strip() == "CONTROL_TOKEN_FILE_EMPTY"
    code, _, err = _run("pair", "--token-file", str(wrong), "--base-url", "http://192.0.2.10:8088")
    assert code == 1 and err.strip() == "BASE_URL_NOT_LOOPBACK"
    monkeypatch.setenv("PADOS_IDENTITY_STORE_DIR", str(tmp_path / "nowhere" / "identity"))
    monkeypatch.setenv("PADOS_HOME", str(tmp_path / "home"))
    from pados import config

    config.get_settings.cache_clear()
    code, _, err = _run("pair")
    assert code == 1 and err.strip() == "CONTROL_TOKEN_FILE_NOT_FOUND"


def test_disabled_identity_is_reported(make_harness: Callable[..., Harness], credential_file: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.identity import cli as device_cli

    harness = make_harness(enabled=False)
    monkeypatch.setattr(device_cli, "client_factory", lambda base: TestClient(harness.app, base_url=base))
    code, _, err = _run("pair", "--token-file", str(credential_file))
    assert code == 1 and err.strip() == "IDENTITY_DISABLED"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def live_server(harness: Harness) -> Iterator[str]:
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(harness.app, host="127.0.0.1", port=port, log_level="warning",
                                           proxy_headers=False, lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("live uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_pair_against_a_live_loopback_server(live_server: str, credential_file: Path) -> None:
    code, out, err = _run("pair", "--json", "--base-url", live_server, "--token-file", str(credential_file))
    assert code == 0, err
    pairing_code = json.loads(out)["pairing_code"]
    with httpx.Client(base_url=live_server, trust_env=False) as browser:
        paired = browser.post("/identity/v1/pair", json={"pairing_code": pairing_code}, headers={"Origin": ORIGIN})
        assert paired.status_code == 200, paired.text
        assert browser.get("/identity/v1/session").json()["authenticated"] is True


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows PowerShell 5.1 (powershell.exe) exists only on Windows; the --json contract is "
                           "covered on every platform by test_pair_json_prints_only_the_code_and_expiry")
def test_windows_powershell_51_parses_the_json(live_server: str, credential_file: Path, tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("powershell.exe is not on PATH on this Windows host")
    python = sys.executable.replace("'", "''")
    base = f"--base-url '{live_server}' --token-file '{str(credential_file).replace(chr(39), chr(39) * 2)}'"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$pair = & '{python}' -m pados.cli.main device pair --json {base} | ConvertFrom-Json; "
        "if ($LASTEXITCODE -ne 0) { exit 3 }; "
        f"$list = & '{python}' -m pados.cli.main device list --json {base} | ConvertFrom-Json; "
        "if ($LASTEXITCODE -ne 0) { exit 4 }; "
        "Write-Output ('PSVERSION=' + $PSVersionTable.PSVersion.Major); "
        "Write-Output ('CODE=' + $pair.pairing_code); "
        "Write-Output ('EXPIRES=' + $pair.expires_at); "
        "Write-Output ('DEVICES=' + @($list.devices).Count)"
    )
    completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script], cwd=tmp_path,
                               capture_output=True, text=True, timeout=180, check=False)
    assert completed.returncode == 0, completed.stderr
    lines = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    assert lines["PSVERSION"] == "5"
    assert CODE_SHAPE.fullmatch(lines["CODE"]) and lines["EXPIRES"]
    assert lines["DEVICES"] == "0"
    assert CONTROL_VALUE not in completed.stdout + completed.stderr
