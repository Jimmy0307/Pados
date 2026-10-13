import json

import pytest

from pados.enterprise.rerank import (
    DeterministicLexicalReranker,
    HttpReranker,
    NoOpReranker,
    RerankCandidate,
    RerankError,
    RerankMode,
    rerank,
)
from pados.enterprise.secrets import SecretValue

CANDIDATES = [RerankCandidate("c1", "pump fittings overview", 0.9),
              RerankCandidate("c2", "coolant pump inspection interval every 90 days", 0.5),
              RerankCandidate("c3", "unrelated ledger", 0.1)]


def test_noop_reranker_keeps_order_and_reports_metadata():
    ranked, report = rerank(NoOpReranker(), RerankMode.ACTIVE, "pump", CANDIDATES)
    assert [cid for cid, _ in ranked] == ["c1", "c2", "c3"]
    for key in ("reranker_id", "model", "version", "input_count", "output_ranking", "latency_ms",
                "resource_class", "provenance"):
        assert key in report
    assert report["input_count"] == 3


def test_local_deterministic_reranker_orders_by_query_coverage():
    ranked, report = rerank(DeterministicLexicalReranker(), RerankMode.ACTIVE,
                            "coolant pump inspection interval", CANDIDATES)
    assert ranked[0][0] == "c2" and report["applied"] is True
    assert report["output_ranking"][0] == "c2" and report["resource_class"] == "CPU_LIGHT"
    again, _ = rerank(DeterministicLexicalReranker(), RerankMode.ACTIVE, "coolant pump inspection interval", CANDIDATES)
    assert again == ranked


def test_shadow_mode_scores_without_changing_order_and_disabled_never_calls():
    ranked, report = rerank(DeterministicLexicalReranker(), RerankMode.SHADOW, "coolant pump inspection", CANDIDATES)
    assert [cid for cid, _ in ranked] == ["c1", "c2", "c3"] and report["shadow_ranking"][0] == "c2"

    class Exploding(NoOpReranker):
        def score(self, query, candidates):
            raise AssertionError("must not be called")

    ranked, report = rerank(Exploding(), RerankMode.DISABLED, "q", CANDIDATES)
    assert all(score is None for _, score in ranked) and report["applied"] is False


def test_http_reranker_validates_schema_and_redacts_credential():
    seen = {}

    def transport(url, body, headers):
        seen.update(url=url, body=json.loads(body), auth=headers.get("authorization"))
        return 200, json.dumps({"results": [{"index": 2, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.3},
                                            {"index": 1, "relevance_score": 0.9}]}).encode()

    credential = SecretValue("synthetic-credential-value-123")
    reranker = HttpReranker(endpoint="http://127.0.0.1:9/rerank", model="local-reranker", version="2",
                            transport=transport, credential=credential)
    ranked, report = rerank(reranker, RerankMode.ACTIVE, "pump", CANDIDATES)
    assert ranked[0][0] == "c2" and report["model"] == "local-reranker"
    assert seen["auth"] == "Bearer synthetic-credential-value-123"  # only inside the call
    assert "synthetic-credential-value-123" not in json.dumps(report) and "synthetic" not in repr(credential)

    def bad(url, body, headers):
        return 200, b'{"results": [{"index": 0, "relevance_score": 5}]}'

    with pytest.raises(RerankError, match="RERANK_SCHEMA_INVALID"):
        rerank(HttpReranker(endpoint="x", model="m", version="1", transport=bad), RerankMode.ACTIVE, "q", CANDIDATES)

    def failing(url, body, headers):
        raise ConnectionError(f"refused with {headers['authorization']}")

    with pytest.raises(RerankError) as info:
        rerank(HttpReranker(endpoint="x", model="m", version="1", transport=failing, credential=credential),
               RerankMode.ACTIVE, "q", CANDIDATES)
    assert "synthetic-credential-value-123" not in str(info.value)


def test_active_failure_fails_closed_shadow_failure_is_reported():
    unconfigured = HttpReranker(endpoint="x", model="m", version="1", transport=None)
    assert unconfigured.health().value == "NOT_CONFIGURED"
    with pytest.raises(RerankError, match="NOT_CONFIGURED"):
        rerank(unconfigured, RerankMode.ACTIVE, "q", CANDIDATES)
    ranked, report = rerank(unconfigured, RerankMode.SHADOW, "q", CANDIDATES)
    assert report["error"] == "RERANK_TRANSPORT_NOT_CONFIGURED" and [c for c, _ in ranked] == ["c1", "c2", "c3"]
