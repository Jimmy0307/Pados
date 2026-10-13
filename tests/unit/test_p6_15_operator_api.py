from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.config import Settings
from pados.corpus import operator
from pados.corpus.api import register_corpus_routes


def client() -> TestClient:
    app = FastAPI()
    register_corpus_routes(app, Settings())
    return TestClient(app)


def test_authorization_route_preserves_human_decision_payload(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_authorize(batch_id: str, **kwargs):
        seen["batch_id"] = batch_id
        seen.update(kwargs)
        return {"status": "ok", "batch": {"status": "AUTHORIZED"}, "authorization": {"authorizer": "human"}}

    monkeypatch.setattr(operator, "authorize_batch", fake_authorize)
    response = client().post(
        "/api/corpus/batches/11111111-1111-1111-1111-111111111111/authorize",
        json={
            "manifest_sha256": "a" * 64,
            "selected_file_hashes": {"paper.pdf": "b" * 64},
            "expires_hours": 12,
            "ingest_after": False,
        },
    )
    assert response.status_code == 200
    assert seen == {
        "batch_id": "11111111-1111-1111-1111-111111111111",
        "manifest_sha256": "a" * 64,
        "selected_file_hashes": {"paper.pdf": "b" * 64},
        "expires_hours": 12,
        "ingest_after": False,
    }


def test_unauthorized_ingest_is_structured_and_fail_closed(monkeypatch) -> None:
    def fail_closed(_batch_id: str):
        raise operator.OperatorError("UNAUTHORIZED_INGEST", "human authorization is required", status_code=403)

    monkeypatch.setattr(operator, "ingest_batch", fail_closed)
    response = client().post("/api/corpus/batches/11111111-1111-1111-1111-111111111111/ingest")
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "UNAUTHORIZED_INGEST",
        "message": "human authorization is required",
        "write_occurred": False,
    }


def test_search_contract_contains_grounded_evidence_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        operator,
        "search",
        lambda _project_id, _query, *, limit: {
            "status": "ok",
            "policy": "KEYWORD_ONLY",
            "query": "risk",
            "result_count": 1,
            "results": [
                {
                    "document_id": "doc",
                    "version_id": "version",
                    "chunk_id": "chunk",
                    "evidence_span_id": "evidence",
                    "source_filename": "paper.pdf",
                    "page_number": 2,
                    "matched_excerpt": "risk evidence",
                    "match_count": 1,
                }
            ],
            "limit": limit,
        },
    )
    response = client().post(
        "/api/projects/325f8168-efec-4818-99ac-b00a03d6accd/corpus/search",
        json={"query": "risk", "limit": 10},
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["evidence_span_id"] == "evidence"
    assert result["source_filename"] == "paper.pdf"
    assert result["page_number"] == 2
