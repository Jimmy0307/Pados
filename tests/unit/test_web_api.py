from collections import Counter

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from pados.api.app import create_app
from pados.config import get_settings
from pados.corpus import operator as corpus_operator
from pados.web.service import safe_row


def client() -> TestClient:
    return TestClient(create_app())


def test_integrated_app_has_one_route_for_each_method_path_pair() -> None:
    app = create_app()
    pairs = Counter(
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    )
    assert not {pair: count for pair, count in pairs.items() if count > 1}
    required_gets = ("/api/health", "/api/projects", "/api/memories", "/api/runs", "/api/events")
    assert all(("GET", path) in pairs for path in required_gets)
    assert ("POST", "/api/projects/{project_id}/corpus/batches") in pairs
    assert ("POST", "/api/corpus/batches/{batch_id}/authorize") in pairs


def test_web_health_exposes_safe_runtime_status() -> None:
    response = client().get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["database"]["name"] == get_settings().operational_database_name
    assert payload["database"]["pgvector"] is True
    assert "password" not in response.text.lower()
    assert "***" not in response.text


def test_project_list_maps_authority_unavailable_to_service_unavailable(
    monkeypatch,
) -> None:
    def unavailable(*, include_corpus_status: bool = True) -> dict[str, object]:
        del include_corpus_status
        raise corpus_operator.OperatorError(
            "OPERATIONAL_UNAVAILABLE",
            "operational project authority is unavailable",
            status_code=503,
        )

    monkeypatch.setattr(corpus_operator, "list_projects", unavailable)

    response = client().get("/api/projects")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPERATIONAL_UNAVAILABLE"


def test_readiness_returns_service_unavailable_when_database_is_unreachable(
    monkeypatch,
) -> None:
    def unavailable(_settings: object) -> dict[str, str]:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr("pados.api.app.check_database", unavailable)

    response = client().get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {"status": "not_ready"}


def test_database_explorer_lists_real_tables_and_safe_rows() -> None:
    api = client()
    overview = api.get("/api/database/overview")
    assert overview.status_code == 200
    assert any(item["name"] == "projects" for item in overview.json()["tables"])

    rows = api.get("/api/database/tables/public/projects/rows", params={"page_size": 3, "exact_count": "true"})
    assert rows.status_code == 200
    assert rows.json()["pagination"]["page_size"] == 3
    assert len(rows.json()["rows"]) <= 3


def test_database_explorer_rejects_unknown_tables_and_large_pages() -> None:
    api = client()
    assert api.get("/api/database/tables/public/not_a_real_table/rows").status_code == 404
    assert api.get("/api/database/tables/public/projects/rows", params={"page_size": 101}).status_code == 422


def test_project_memory_runs_and_events_surfaces_are_live() -> None:
    api = client()
    projects = api.get("/api/projects", params={"page_size": 2})
    memories = api.get("/api/memories", params={"page_size": 2})
    memory_search = api.get("/api/memories/search", params={"q": "project", "page_size": 2})
    runs = api.get("/api/runs", params={"page_size": 2})
    events = api.get("/api/events", params={"page_size": 2})
    assert projects.status_code == memories.status_code == memory_search.status_code == runs.status_code == events.status_code == 200
    assert projects.json()["pagination"]["page_size"] == 2
    assert "memories" in memories.json()
    assert "memories" in memory_search.json()
    assert "runs" in runs.json()
    assert "events" in events.json()


def test_secret_like_and_binary_values_are_redacted() -> None:
    value = safe_row({"db_password": "hidden", "api_key": "hidden", "payload": {"token": "hidden"}, "blob": b"bytes"})
    assert value["db_password"] == "[REDACTED]"
    assert value["api_key"] == "[REDACTED]"
    assert value["payload"] == {"token": "[REDACTED]"}
    assert value["blob"] == "[BINARY]"
