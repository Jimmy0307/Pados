from typing import Any

from fastapi.testclient import TestClient

from pados.api import app as app_module


class _SessionScope:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        return None


def test_memory_facts_route_defaults_to_candidate_and_keeps_compatibility(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_scope(_settings: object = None) -> _SessionScope:
        return _SessionScope()

    def fake_add(_session: object, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "memory-candidate-1"

    monkeypatch.setattr(app_module, "session_scope", fake_scope)
    monkeypatch.setattr(app_module.memory_service, "add_memory_record", fake_add)

    response = TestClient(app_module.create_app()).post(
        "/memory/facts",
        json={
            "project_id": "project-1",
            "title": "candidate fact",
            "summary": "candidate summary",
            "source_uri": "source:operator",
        },
    )

    assert response.status_code == 200
    assert response.json()["authority_state"] == "CANDIDATE"
    assert response.json()["context_eligible"] is False
    assert captured["verification_status"] == "UNVERIFIED"
    assert captured["source_trust_level"] == "OBSERVED"


def test_memory_facts_route_rejects_authority_override(monkeypatch: Any) -> None:
    monkeypatch.setattr(app_module, "session_scope", lambda _settings=None: _SessionScope())
    response = TestClient(app_module.create_app()).post(
        "/memory/facts",
        json={
            "project_id": "project-1",
            "title": "attempted fact",
            "summary": "must remain candidate",
            "source_uri": "source:operator",
            "authority_state": "HUMAN_CONFIRMED",
        },
    )
    assert response.status_code == 409
    assert "use promotion" in response.json()["detail"]
