import json

import pytest
from enterprise_support import access

from pados.enterprise.budget import EvidenceBudget, assert_within_budget
from pados.enterprise.classification import DataClassification
from pados.enterprise.retrieval import RetrievalRequest, RetrievalStrategy


def _request(who, **kwargs):
    values = {"query": "coolant pump inspection interval", "classification_ceiling": DataClassification.CONFIDENTIAL}
    values.update(kwargs)
    return RetrievalRequest(project_id=who.project_id, tenant_id=who.tenant_id, principal_id=who.principal_id,
                            session_id=who.session_id, **values)


def test_response_is_evidence_with_identity_spans_scores_provenance_and_versions(env):
    who = access()
    response = env.pipeline.retrieve(_request(who), who, env.authorize(who))
    body = response.to_dict()
    assert body["semantic_authority"] == "NONE" and body["promotable_to_truth"] is False
    assert body["evidence"], "authorized synthetic evidence expected"
    top = body["evidence"][0]
    assert top["source_id"] == env.source_ids["handbook"]
    for item in body["evidence"]:
        assert item["source_id"].startswith("sha256:")
        assert {"page", "char_start", "char_end"} <= set(item["span"])
        assert "fused" in item["retrieval_scores"] and item["rerank_score"] is not None
        prov = item["provenance"]
        for key in ("content_hash", "location_ref", "ingested_at", "classification", "tenant_id", "project_id",
                    "parser_version", "chunking_version", "embedding_version", "extractor_id", "normalized_id"):
            assert prov[key]
    assert {b["backend_id"] for b in body["backends"]} >= {"pados.retrieval.exact.in-memory",
                                                             "pados.retrieval.vector.in-memory"}
    assert body["authorization"]["decision"] == "ALLOW" and body["authorization"]["ceiling"] == "CONFIDENTIAL"
    assert body["versions"]["retrieval_contract"] == "pados.enterprise.retrieval.v1"
    assert len(response.package_hash) == 64
    # Deterministic: the same request yields the same package.
    again = env.pipeline.retrieve(_request(who), who, env.authorize(who))
    assert again.package_hash == response.package_hash


@pytest.mark.parametrize("strategy", list(RetrievalStrategy))
def test_strategies_are_composable(env, strategy):
    who = access()
    response = env.pipeline.retrieve(_request(who, strategy=strategy), who, env.authorize(who))
    components = set().union(*(item.retrieval_scores for item in response.evidence))
    if strategy is RetrievalStrategy.EXACT:
        assert "vector" not in components
    if strategy is RetrievalStrategy.VECTOR:
        assert "exact" not in components


def test_budget_is_enforced_before_invocation(env):
    who = access(clearance=DataClassification.RESTRICTED)
    budget = EvidenceBudget(max_sources=1, max_chunks=1, max_context_tokens=500)
    response = env.pipeline.retrieve(_request(who, budget=budget), who, env.authorize(who))
    assert len(response.evidence) == 1 and response.budget["used"]["sources"] == 1
    assert sum(response.budget["dropped"].values()) >= 1
    with pytest.raises(PermissionError, match="EVIDENCE_BUDGET_EXCEEDED"):
        assert_within_budget(list(response.evidence) * 2, budget)
    with pytest.raises(ValueError):
        EvidenceBudget(max_chunks=0)


def test_restricted_exposure_budget(env):
    finance = access("finance-lead", clearance=DataClassification.RESTRICTED)
    query = "pump supplier renegotiation target price"
    zero = env.pipeline.retrieve(_request(finance, query=query, classification_ceiling=DataClassification.RESTRICTED),
                                 finance, env.authorize(finance))
    assert DataClassification.RESTRICTED not in {i.classification for i in zero.evidence}
    assert zero.budget["dropped"]["max_restricted_items"] == 1
    one = env.pipeline.retrieve(_request(finance, query=query, classification_ceiling=DataClassification.RESTRICTED,
                                         budget=EvidenceBudget(max_restricted_items=1)), finance, env.authorize(finance))
    assert one.budget["used"]["restricted_items"] == 1


def test_request_bounds_and_identity_binding(env):
    who = access()
    with pytest.raises(ValueError):
        _request(who, query=" ")
    with pytest.raises(ValueError):
        _request(who, top_k=0)
    other = access("someone-else")
    with pytest.raises(PermissionError, match="ACCESS_MISMATCH"):
        env.pipeline.retrieve(_request(other), who, env.authorize(who))


def test_evidence_without_text_projection_contains_no_content(env):
    who = access()
    response = env.pipeline.retrieve(_request(who), who, env.authorize(who))
    projected = json.dumps(response.to_dict(include_text=False))
    assert "coolant pump inspection interval is every 90 days" not in projected
