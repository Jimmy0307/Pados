from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from pytest import MonkeyPatch
from typer.testing import CliRunner

import pados.cli.main as cli_main
from pados.cli.main import app


def test_uuid_boundary_rejects_invalid_project_id_without_traceback() -> None:
    result = CliRunner().invoke(
        app,
        ["memory", "search", "--project-id", "not-a-uuid", "--query", "runtime"],
    )

    assert result.exit_code != 0
    assert "UUID" in result.output
    assert "Traceback" not in result.output
    assert "SQLAlchemy" not in result.output


def test_event_help_lists_allowlist_and_payload_file() -> None:
    result = CliRunner().invoke(app, ["memory", "event-add", "--help"])

    assert result.exit_code == 0
    assert "ARTIFACT_CREATED" in result.output
    assert "WORKER_STARTED" in result.output
    assert "--payload-file" in result.output


def test_event_payload_file_is_a_stable_cli_boundary(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    @contextmanager
    def fake_session_scope() -> object:
        yield object()

    def fake_append_run_event(session: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "event-id"

    monkeypatch.setattr(cli_main, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli_main.memory_service, "append_run_event", fake_append_run_event)
    payload_file = tmp_path / "payload.json"
    payload_file.write_text('{"command":"pytest","log_path":"run.log"}', encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "memory",
            "event-add",
            "--project-id",
            "00000000-0000-0000-0000-000000000000",
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--event-type",
            "TEST_EXECUTED",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["payload_json"] == {"command": "pytest", "log_path": "run.log"}
    assert captured["project_id"] == str(UUID("00000000-0000-0000-0000-000000000000"))


def test_event_payload_json_and_file_are_mutually_exclusive(tmp_path: Path) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "memory",
            "event-add",
            "--project-id",
            "00000000-0000-0000-0000-000000000000",
            "--run-id",
            "00000000-0000-0000-0000-000000000001",
            "--event-type",
            "TEST_EXECUTED",
            "--payload-json",
            "{}",
            "--payload-file",
            str(payload_file),
        ],
    )

    assert result.exit_code != 0
    assert "only one" in result.output
    assert "Traceback" not in result.output
