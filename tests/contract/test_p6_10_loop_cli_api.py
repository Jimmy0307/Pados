import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from pados.api.app import create_app
from pados.cli.main import app


def test_loop_cli_diagnose_json_contract() -> None:
    result = CliRunner().invoke(app, ["loop", "diagnose", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["bounded"] is True
    assert payload["deterministic"] is True
    assert payload["fail_closed"] is True
    assert "step" in payload["commands"]


def test_loop_control_api_requires_token_before_database() -> None:
    client = TestClient(create_app())

    assert client.get("/control/loops").status_code == 403
    assert client.post("/control/loops/11111111-1111-4111-8111-111111111111/step", json={}).status_code == 403
    assert client.post("/control/task-goals", json={}).status_code == 403
