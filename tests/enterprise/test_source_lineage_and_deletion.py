import pytest
from enterprise_support import access

from pados.enterprise.classification import DataClassification
from pados.enterprise.lineage import LineageGraph, LineageKind, derived_id, register_source
from pados.enterprise.retrieval import RetrievalRequest
from pados.knowledge.identity import source_identity_from_bytes


def test_enterprise_source_reuses_pados_source_identity_and_preserves_metadata():
    content = b"synthetic source"
    source = register_source(content, source_type="synthetic-doc", location_ref="synthetic://a",
                             classification=DataClassification.INTERNAL, tenant_id="t1", project_id="p1",
                             parser_version="pados.text:1", ingested_at="2026-09-30T00:00:00+00:00")
    assert source.identity == source_identity_from_bytes(content)
    lineage = source.lineage()
    assert lineage["source_id"] == source_identity_from_bytes(content).source_id
    for key in ("content_hash", "source_type", "location_ref", "ingested_at", "classification", "tenant_id",
                "project_id", "parser_version", "chunking_version", "embedding_version", "provenance"):
        assert key in lineage
    moved = register_source(content, source_type="synthetic-doc", location_ref="synthetic://moved",
                            classification=DataClassification.INTERNAL, tenant_id="t1", project_id="p1",
                            parser_version="pados.text:1")
    assert moved.source_id == source.source_id  # location is lineage, not identity
    with pytest.raises(ValueError, match="SOURCE_BINDING_INVALID"):
        register_source(content, source_type="x", location_ref="y", classification=DataClassification.PUBLIC,
                        tenant_id="", project_id="p1", parser_version="1")


def test_every_derived_chunk_keeps_source_lineage(env):
    for record in env.pipeline.exact.records():
        assert record.source.source_id in env.pipeline.lineage.nodes
        assert record.chunk_id in env.pipeline.lineage.descendants(record.source.source_id)
        assert record.chunking_version and record.embedding_version and record.extractor_id
    graph = LineageGraph()
    with pytest.raises(ValueError, match="REQUIRES_LINEAGE"):
        graph.add("chunk:orphan", LineageKind.CHUNK)
    graph.add("sha256:" + "a" * 64, LineageKind.SOURCE)
    with pytest.raises(ValueError, match="PARENT_INVALID"):
        graph.add("embedding:x", LineageKind.EMBEDDING, ["sha256:" + "a" * 64])


def test_revocation_is_a_deterministic_dry_run_that_flags_trained_artifacts(env):
    source_id = env.source_ids["handbook"]
    graph = env.pipeline.lineage
    chunk = next(r.chunk_id for r in env.pipeline.exact.records() if r.source_id == source_id)
    evaluation = graph.add(derived_id("EVALUATION", chunk), LineageKind.EVALUATION_ARTIFACT, [chunk])
    dataset = graph.add(derived_id("DATASET", evaluation), LineageKind.DATASET_REFERENCE, [evaluation])
    trained = graph.add("trained:reranker-ft-1", LineageKind.TRAINED_ARTIFACT, [dataset])
    cache = graph.add(derived_id("CACHE", chunk), LineageKind.CACHE, [chunk])
    before = dict(graph.nodes)
    plan = env.pipeline.plan_revocation(source_id, reason="synthetic revoke")
    assert plan["dry_run"] is True and plan["model_weights_deleted"] is False
    assert plan["human_review_required"] is True and set(plan["human_review_items"]) == {dataset, trained}
    assert cache in plan["invalidate_automatically"] and trained not in plan["invalidate_automatically"]
    for kind in ("NORMALIZED_ARTIFACT", "CHUNK", "EMBEDDING", "RETRIEVAL_PROJECTION", "CACHE",
                 "EVALUATION_ARTIFACT", "DATASET_REFERENCE", "TRAINED_ARTIFACT"):
        assert plan["affected"][kind], kind
    assert graph.nodes == before and not graph.revoked  # planning changes nothing
    assert env.pipeline.plan_revocation(source_id, reason="synthetic revoke")["plan_hash"] == plan["plan_hash"]


def test_applied_revocation_masks_retrieval_projections_but_never_trained_artifacts(env):
    who = access()
    request = RetrievalRequest(project_id=who.project_id, tenant_id=who.tenant_id, principal_id=who.principal_id,
                               session_id=who.session_id, query="coolant pump inspection ledger",
                               classification_ceiling=DataClassification.CONFIDENTIAL)
    source_id = env.source_ids["handbook"]
    assert source_id in {i.source_id for i in env.pipeline.retrieve(request, who, env.authorize(who)).evidence}
    plan = env.pipeline.plan_revocation(source_id, reason="source withdrawn")
    marked = env.pipeline.apply_revocation(plan)
    assert source_id in marked
    after = env.pipeline.retrieve(request, who, env.authorize(who))
    assert source_id not in {i.source_id for i in after.evidence}
    assert after.authorization["coarse_scope"]["revoked_excluded"] == len(marked)
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        env.pipeline.apply_revocation({**plan, "dry_run": False})
