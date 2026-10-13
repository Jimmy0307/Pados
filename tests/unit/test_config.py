from pados.config import Settings


def test_safe_database_url_redacts_password() -> None:
    settings = Settings()
    assert "***" in settings.safe_database_url
    assert settings.db_password not in settings.safe_database_url


def test_runtime_mode_defaults_to_native() -> None:
    settings = Settings()
    assert settings.runtime_mode == "native"

