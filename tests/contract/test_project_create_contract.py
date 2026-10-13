import json
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

import pados.cli.main as cli_main
from pados.memory import service as memory_service


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


def test_project_create_json_output_and_root_validation(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(cli_main, "session_scope", lambda: _SessionContext())
    monkeypatch.setattr(
        memory_service,
        "bootstrap_project",
        lambda session, **kwargs: "11111111-1111-4111-8111-111111111111",
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "project",
            "create",
            "--name",
            "PADOS Demo Project",
            "--root-path",
            str(root),
            "--classification",
            "internal",
            "--default-branch",
            "main",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "canonical_root": str(root.resolve()),
        "classification": "internal",
        "default_branch": "main",
        "project_id": "11111111-1111-4111-8111-111111111111",
        "status": "ok",
    }
