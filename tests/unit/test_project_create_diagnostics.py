import json

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from typer import Exit

import pados.cli.main as cli_main


class _Diagnostic:
    column_name = "status"
    constraint_name = "projects_status_not_null"
    sqlstate = "23502"


class _OriginalIntegrityError(Exception):
    diag = _Diagnostic()
    sqlstate = "23502"


class _OriginalDatabaseError(Exception):
    diag = None
    sqlstate = "28P01"


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))


def test_integrity_diagnostics_preserve_sqlstate_and_constraint_without_parameters(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = _Logger()
    monkeypatch.setattr(cli_main, "get_logger", lambda name: logger)

    @cli_main.operator_error_boundary
    def fail() -> None:
        raise IntegrityError(
            "INSERT INTO projects ...",
            {"status": None, "password": "must-not-log"},
            _OriginalIntegrityError(),
        )

    with pytest.raises(Exit) as caught:
        fail()

    assert caught.value.exit_code == 2
    assert "the requested operation was rejected" in capsys.readouterr().err
    assert logger.events == [
        (
            "error",
            "database_operation_failed",
            {
                "sqlstate": "23502",
                "constraint_name": "projects_status_not_null",
                "column_name": "status",
                "failure_category": "not_null_violation",
            },
        )
    ]
    assert "must-not-log" not in json.dumps(logger.events)


def test_non_integrity_dbapi_error_is_not_reported_as_constraint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = _Logger()
    monkeypatch.setattr(cli_main, "get_logger", lambda name: logger)

    @cli_main.operator_error_boundary
    def fail() -> None:
        raise DBAPIError(
            "connect",
            {"password": "must-not-log"},
            _OriginalDatabaseError(),
        )

    with pytest.raises(Exit) as caught:
        fail()

    assert caught.value.exit_code == 2
    assert "Database error: the database operation could not be completed" in capsys.readouterr().err
    assert logger.events[0][2] == {
        "sqlstate": "28P01",
        "constraint_name": "not_applicable",
        "column_name": "not_applicable",
        "failure_category": "database_error",
    }
