# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pados.cli.main as cli_main
from pados.config import Settings


def test_project_create_initializes_scope_in_the_separate_local_proof_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        PADOS_DB_HOST="127.0.0.1",
        PADOS_OPERATIONAL_DATABASE_NAME="pados_ops",
        PADOS_PRODUCT_DATABASE_NAME="pados_proof",
        PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "proof-objects"),
    )
    calls: list[tuple[str, str]] = []

    class Engine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    from pados.state_proof import object_store, service

    monkeypatch.setattr(service, "build_proof_engine", lambda url: (calls.append(("engine", url)), engine)[1])
    monkeypatch.setattr(object_store, "ObjectStore", lambda root: ("object-store", root))

    class ProofService:
        def __init__(self, passed_engine: Any, store: Any) -> None:
            assert passed_engine is engine
            assert store[0] == "object-store"

        def ensure_scope(self, scope_id: str) -> dict[str, Any]:
            calls.append(("scope", scope_id))
            return {"current_version": 0}

    monkeypatch.setattr(service, "StateProofService", ProofService)

    assert cli_main._ensure_local_proof_scope("11111111-1111-4111-8111-111111111111") == 0
    assert calls[0][0] == "engine"
    assert calls[0][1].endswith("/pados_proof")
    assert calls[1] == ("scope", "11111111-1111-4111-8111-111111111111")
    assert engine.disposed is True


def test_project_scope_initialization_refuses_nonlocal_or_shared_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pados.state_proof import service

    settings = Settings(
        PADOS_DB_HOST="db.example.invalid",
        PADOS_OPERATIONAL_DATABASE_NAME="pados_ops",
        PADOS_PRODUCT_DATABASE_NAME="pados_proof",
        PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "proof-objects"),
    )
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "build_proof_engine", lambda _url: pytest.fail("must not connect"))

    with pytest.raises(PermissionError, match="PROJECT_PROOF_DATABASE_MUST_BE_LOCAL"):
        cli_main._ensure_local_proof_scope("11111111-1111-4111-8111-111111111111")

    shared = settings.model_copy(update={"product_database_name": "pados_ops", "db_host": "127.0.0.1"})
    monkeypatch.setattr(cli_main, "get_settings", lambda: shared)
    with pytest.raises(PermissionError, match="PROJECT_PROOF_DATABASE_MUST_BE_SEPARATE"):
        cli_main._ensure_local_proof_scope("11111111-1111-4111-8111-111111111111")


def test_initialize_proof_scope_command_is_an_idempotent_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QueryResult:
        def scalar_one_or_none(self) -> int:
            return 1

    class Session:
        def execute(self, *_args: Any, **_kwargs: Any) -> QueryResult:
            return QueryResult()

    class SessionContext:
        def __enter__(self) -> Session:
            return Session()

        def __exit__(self, *_args: Any) -> None:
            return None

    calls: list[str] = []
    monkeypatch.setattr(cli_main, "session_scope", lambda: SessionContext())
    monkeypatch.setattr(
        cli_main,
        "_ensure_local_proof_scope",
        lambda project_id: (calls.append(project_id), 0)[1],
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "project",
            "initialize-proof-scope",
            "--project-id",
            "11111111-1111-4111-8111-111111111111",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"scope_version": 0' in result.stdout
    assert calls == ["11111111-1111-4111-8111-111111111111"]
