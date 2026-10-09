from pathlib import Path

from pytest import MonkeyPatch

from pados.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_native_postgres_defaults_use_canonical_local_port(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("PADOS_DB_HOST", raising=False)
    monkeypatch.delenv("PADOS_DB_PORT", raising=False)
    monkeypatch.delenv("PADOS_DB_NAME", raising=False)

    settings = Settings(_env_file=None)

    assert settings.db_host == "127.0.0.1"
    assert settings.db_port == 5432
    assert settings.db_name == "pados_p6_7_validation"
    assert ":5432/" in settings.safe_database_url


def test_native_postgres_port_can_be_overridden(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_DB_PORT", "55432")

    settings = Settings()

    assert settings.db_port == 55432


def test_native_defaults_do_not_depend_on_docker_port_mapping(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("PADOS_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("PADOS_DB_PORT", raising=False)

    settings = Settings()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert settings.runtime_mode == "native"
    assert settings.db_port == 5432
    assert '"55432:5432"' in compose


def test_runtime_scripts_do_not_default_to_stale_native_port() -> None:
    backup = (ROOT / "scripts" / "backup.ps1").read_text(encoding="utf-8")
    restore = (ROOT / "scripts" / "restore.ps1").read_text(encoding="utf-8")

    assert '} else { "5432" }' in backup
    assert '} else { "5432" }' in restore
    assert '} else { "55432" }' not in backup
    assert '} else { "55432" }' not in restore


def test_alembic_runtime_url_is_sourced_from_settings() -> None:
    env = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    assert 'config.set_main_option("sqlalchemy.url", get_settings().database_url)' in env
