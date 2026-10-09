from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_shared_runtime_helper_enforces_project_python_and_import_path() -> None:
    content = (ROOT / "scripts" / "lib" / "Initialize-PadosRuntime.ps1").read_text(
        encoding="utf-8"
    )
    assert '.venv\\Scripts\\python.exe' in content
    assert "WORKTREE_IMPORT_PATH_MISMATCH" in content
    assert "PYTHONPATH" in content
    assert "OrdinalIgnoreCase" in content


def test_mutable_and_release_scripts_use_shared_runtime_guard() -> None:
    for name in ("start.ps1", "healthcheck.ps1", "backup.ps1", "restore.ps1", "package_release.ps1"):
        content = _script(name)
        assert "Initialize-PadosRuntime.ps1" in content
        assert "Assert-PadosImportPath" in content


def test_start_script_defaults_to_native_runtime() -> None:
    content = _script("start.ps1")
    assert "PADOS_RUNTIME_MODE" in content
    assert '} else { "native" }' in content
    assert "Start-Process" in content
    assert "docker --context default compose up -d" in content


def test_healthcheck_script_checks_api_and_doctor() -> None:
    content = _script("healthcheck.ps1")
    assert "/health" in content
    assert "doctor --json" in content
    assert "PADOS_RUNTIME_MODE" in content


def test_backup_script_uses_postgres_native_tools_for_native_mode() -> None:
    content = _script("backup.ps1")
    assert 'Get-Command "pg_dump"' in content
    assert "PGPASSWORD" in content
    assert "docker --context default compose exec" in content


def test_restore_script_uses_postgres_native_tools_for_native_mode() -> None:
    content = _script("restore.ps1")
    assert 'Get-Command "dropdb"' in content
    assert 'Get-Command "createdb"' in content
    assert 'Get-Command "pg_restore"' in content
    assert "Refusing to restore over production database" in content
