# SPDX-License-Identifier: AGPL-3.0-only
"""``pados init`` writes the identity settings (flag off); ``pados serve`` never trusts X-Forwarded-* with it on."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner


def test_init_writes_an_absolute_store_dir_and_the_flag_off(tmp_path: Path) -> None:
    from pados.cli.main import app

    result = CliRunner().invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    store = next(line.split("=", 1)[1] for line in lines if line.startswith("PADOS_IDENTITY_STORE_DIR="))
    assert Path(store).is_absolute() and Path(store) == (tmp_path / "runtime" / "local" / "identity").resolve()
    assert "PADOS_IDENTITY_SESSIONS_ENABLED=false" in lines


@pytest.mark.parametrize("enabled", [False, True])
def test_serve_disables_proxy_headers_only_with_identity_on(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    import uvicorn

    from pados.cli import main as cli
    from pados.config import Settings

    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(kwargs))
    settings = Settings(PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(b"serve-unique").hexdigest().upper(),
                        PADOS_IDENTITY_SESSIONS_ENABLED=enabled)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert CliRunner().invoke(cli.app, ["serve"]).exit_code == 0
    assert len(started) == 1
    if enabled:
        assert started[0]["proxy_headers"] is False
    else:
        assert "proxy_headers" not in started[0]  # unchanged invocation with the flag off
