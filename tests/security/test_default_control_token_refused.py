"""The shipped default control-token hash is publicly known: no control surface may accept it.

RC2's dogfood boundary had to forbid "starting PADOS through the developer Python ``pados`` command, which accepts
the default control-token hash". The fix is fail-closed authentication, not a rule: while the configured hash is
the shipped default, every control request is refused with a closed reason pointing at ``pados init``, whatever
token it carries. A unique hash (as written by ``pados init``) keeps working unchanged.
"""
from __future__ import annotations

import hashlib

import pytest

from pados.config import Settings
from pados.control.security import authorize_control_request, authorize_token
from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH


def _settings(token_hash: str) -> Settings:
    return Settings(PADOS_CONTROL_API_TOKEN_HASH=token_hash, PADOS_API_HOST="127.0.0.1")


@pytest.mark.parametrize("presented", ["anything", "test", "x" * 64])
def test_the_shipped_default_hash_refuses_every_token(presented: str) -> None:
    with pytest.raises(PermissionError, match="DEFAULT_CONTROL_TOKEN_REFUSED"):
        authorize_token(_settings(DEFAULT_CONTROL_TOKEN_HASH), presented)


def test_the_default_is_refused_case_insensitively_and_through_the_shared_gate() -> None:
    with pytest.raises(PermissionError, match="DEFAULT_CONTROL_TOKEN_REFUSED"):
        authorize_control_request(_settings(DEFAULT_CONTROL_TOKEN_HASH.lower()), "anything")


def test_a_unique_hash_from_pados_init_still_authenticates() -> None:
    value = "unique-local-control-value-for-test"
    settings = _settings(hashlib.sha256(value.encode()).hexdigest().upper())
    authorize_control_request(settings, value)
    with pytest.raises(PermissionError, match="invalid control token"):
        authorize_token(settings, "wrong")


def test_the_settings_default_is_the_refused_hash() -> None:
    assert Settings.model_fields["control_api_token_hash"].default.upper() == DEFAULT_CONTROL_TOKEN_HASH


def test_pados_serve_refuses_to_start_on_the_default_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from pados.cli import main as cli
    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(DEFAULT_CONTROL_TOKEN_HASH))
    result = CliRunner().invoke(cli.app, ["serve"])
    assert result.exit_code != 0 and started == []
    assert "DEFAULT_CONTROL_TOKEN_REFUSED" in (result.output + str(result.exception))


def test_pados_serve_starts_with_a_unique_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from pados.cli import main as cli
    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(hashlib.sha256(b"unique").hexdigest().upper()))
    assert CliRunner().invoke(cli.app, ["serve"]).exit_code == 0 and len(started) == 1
