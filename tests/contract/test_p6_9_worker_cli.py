import json

from typer.testing import CliRunner

from pados.cli.main import app


def test_worker_capabilities_cli_json() -> None:
    result = CliRunner().invoke(app, ["worker", "capabilities", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert any(item["capability_id"] == "RUN_TESTS" for item in payload["capabilities"])


def test_worker_diagnose_cli_json() -> None:
    result = CliRunner().invoke(app, ["worker", "diagnose", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["capability_count"] >= 1
    assert "workers" in payload
