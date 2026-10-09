from __future__ import annotations

from pathlib import Path

import pytest

from pados.config import Settings
from pados.product_loop.routes import build_product_loop_service


def _settings(workspace: Path, objects: Path, database_name: str = "p6_21j_test") -> Settings:
    return Settings(
        _env_file=None,
        PADOS_PRODUCT_WORKSPACE_ROOT=str(workspace),
        PADOS_PRODUCT_OBJECT_ROOT=str(objects),
        PADOS_PRODUCT_DATABASE_NAME=database_name,
        PADOS_DB_NAME=database_name,
    )


def test_product_service_fails_closed_without_explicit_generic_roots() -> None:
    with pytest.raises(PermissionError, match="PRODUCT_WORKSPACE_ROOT_REQUIRED"):
        build_product_loop_service(Settings(_env_file=None))


def test_product_service_uses_explicit_roots_without_cwd_or_worktree_assumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated_cwd = tmp_path / "unrelated-cwd"
    workspace = tmp_path / "approved" / "workspace"
    objects = tmp_path / "approved" / "objects"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    service = build_product_loop_service(_settings(workspace, objects))

    assert service.maker.approved_root == workspace.resolve()
    assert workspace.exists()


def test_product_service_requires_explicit_database_binding(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        PADOS_PRODUCT_WORKSPACE_ROOT=str(tmp_path / "workspace"),
        PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "objects"),
        PADOS_DB_NAME="p6_21j_test",
    )

    with pytest.raises(PermissionError, match="PRODUCT_DATABASE_NAME_REQUIRED"):
        build_product_loop_service(settings)


def test_p6_19b_migration_wrapper_declares_formal_heads() -> None:
    env_path = Path(__file__).parents[2] / "migrations" / "p6_19b" / "env.py"
    source = env_path.read_text(encoding="utf-8")

    assert '"20260730_0011"' in source
    assert '"20260808_0013"' in source
    assert "P6_19B_MAIN_SCHEMA_PRECONDITION_FAILED" in source
