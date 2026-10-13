from fastapi.testclient import TestClient

from pados.api.app import create_app


def test_health_returns_status() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert "database" in payload

