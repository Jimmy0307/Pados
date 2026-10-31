"""`pados init` must write a configuration that `pados doctor` can accept, with a unique control token.

The Quickstart runs `pados init` then `pados doctor`. init used to write the database names doctor reserves for the
protected canonical runtime (``PROTECTED_DATABASE_TARGET``), so the documented first run could never pass.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from dotenv import dotenv_values
from typer.testing import CliRunner

from pados.cli import main as cli
from pados.config import Settings
from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH


def _init(tmp_path: Path) -> tuple[dict[str, str | None], str]:
    result = CliRunner().invoke(cli.app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    token = (tmp_path / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    return dotenv_values(tmp_path / ".env"), token


def test_init_writes_database_lanes_that_pass_the_doctor_target_guard(tmp_path: Path) -> None:
    values, _ = _init(tmp_path)
    settings = Settings(**{key: value for key, value in values.items() if value is not None})
    assert cli._doctor_database_target_error(settings) is None


def test_init_writes_a_unique_token_whose_hash_is_configured(tmp_path: Path) -> None:
    values, token = _init(tmp_path)
    configured = values["PADOS_CONTROL_API_TOKEN_HASH"]
    assert configured == hashlib.sha256(token.encode("utf-8")).hexdigest().upper()
    assert configured != DEFAULT_CONTROL_TOKEN_HASH
    other = tmp_path / "second"
    other.mkdir()
    assert _init(other)[1] != token
