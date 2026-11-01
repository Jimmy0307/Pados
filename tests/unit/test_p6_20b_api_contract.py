import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pados.api import app as app_module

CONTROL_VALUE = "pados-test-control-" + "value"
HEADERS = {"X-PADOS-Control-Token": CONTROL_VALUE}


@pytest.fixture(autouse=True)
def _unique_control_hash(monkeypatch: pytest.MonkeyPatch) -> Any:
    from pados import config

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


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
        headers=HEADERS,
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
        headers=HEADERS,
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


@pytest.mark.parametrize(("method", "path", "body"), [
    ("get", "/memory/search?project_id=p&query=q", None),
    ("post", "/memory/facts", {"project_id": "p", "title": "t", "summary": "s", "source_uri": "source:operator"}),
    ("post", "/context/assemble", {"project_id": "p", "query": "q"}),
])
def test_memory_and_context_routes_require_the_control_token(method: str, path: str, body: Any) -> None:
    """RC2 ARCH-5: a local process without the control token can neither read nor write memory or assemble
    context (refused before any database access)."""
    client = TestClient(app_module.create_app())
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    assert getattr(client, method)(path, **kwargs).status_code in (401, 403)
    assert getattr(client, method)(path, headers={"X-PADOS-Control-Token": "wrong"}, **kwargs).status_code in (401, 403)
