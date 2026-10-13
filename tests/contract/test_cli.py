import shutil

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

import pados.cli.main as cli_main
from pados.cli.main import app
from pados.config import get_settings


def test_doctor_reports_without_traceback() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    assert "Traceback" not in result.output



@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Settings are cached process-wide; never leak a test's env overrides to later tests."""
    yield
    get_settings.cache_clear()


def _isolated_doctor_env(monkeypatch: MonkeyPatch) -> None:
    """Doctor tests use non-protected lane names; they never inherit the formal targets."""
    for key, value in {"PADOS_DB_NAME": "pados_cli_contract_operational",
                       "PADOS_OPERATIONAL_DATABASE_NAME": "pados_cli_contract_operational",
                       "PADOS_CORPUS_DATABASE_NAME": "pados_cli_contract_corpus"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli_main, "_system_health", lambda settings: {"ready": True, "dimensions": {}})

def test_native_doctor_does_not_require_docker(monkeypatch: MonkeyPatch) -> None:
    _isolated_doctor_env(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setenv("PADOS_RUNTIME_MODE", "native")
    monkeypatch.setattr(shutil, "which", lambda command: None)
    monkeypatch.setattr(cli_main, "check_database", lambda settings: {"status": "ok", "pgvector": "true"})
    monkeypatch.setattr(cli_main, "_check_migrations", lambda settings: {"status": "ok"})

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert '"mode": "native"' in result.output
    assert '"required": false' in result.output
    assert '"available": false' in result.output


def test_docker_doctor_requires_docker_when_docker_mode(monkeypatch: MonkeyPatch) -> None:
    _isolated_doctor_env(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setenv("PADOS_RUNTIME_MODE", "docker")
    monkeypatch.setattr(shutil, "which", lambda command: None)
    monkeypatch.setattr(cli_main, "check_database", lambda settings: {"status": "ok", "pgvector": "true"})
    monkeypatch.setattr(cli_main, "_check_migrations", lambda settings: {"status": "ok"})

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    assert '"mode": "docker"' in result.output
    assert '"required": true' in result.output
    assert '"available": false' in result.output


def test_doctor_fails_when_pgvector_missing(monkeypatch: MonkeyPatch) -> None:
    _isolated_doctor_env(monkeypatch)
    get_settings.cache_clear()
    monkeypatch.setenv("PADOS_RUNTIME_MODE", "native")
    monkeypatch.setattr(shutil, "which", lambda command: None)
    monkeypatch.setattr(cli_main, "check_database", lambda settings: {"status": "ok", "pgvector": "false"})
    monkeypatch.setattr(cli_main, "_check_migrations", lambda settings: {"status": "ok"})

    result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    assert '"pgvector": "false"' in result.output

