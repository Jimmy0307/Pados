import json

import pytest

from pados.enterprise.lineage import LineageGraph, LineageKind
from pados.lifecycle.propagation import (
    MODEL_WEIGHT_DELETION,
    Stage,
    StageStatus,
    apply_propagation,
    plan_propagation,
)

K = LineageKind


def _graph() -> LineageGraph:
    graph = LineageGraph()
    graph.add("src", K.SOURCE)
    graph.add("norm", K.NORMALIZED_ARTIFACT, ["src"])
    graph.add("chunk-1", K.CHUNK, ["norm"])
    graph.add("chunk-2", K.CHUNK, ["norm"])
    graph.add("emb-1", K.EMBEDDING, ["chunk-1"])
    graph.add("retr", K.RETRIEVAL_PROJECTION, ["emb-1"])
    graph.add("graph", K.GRAPH_PROJECTION, ["chunk-2"])
    graph.add("cache", K.CACHE, ["retr"])
    graph.add("eval", K.EVALUATION_ARTIFACT, ["retr"])
    graph.add("dataset", K.DATASET_REFERENCE, ["eval"])
    graph.add("model", K.TRAINED_ARTIFACT, ["dataset"])
    graph.add("other-src", K.SOURCE)
    graph.add("other-norm", K.NORMALIZED_ARTIFACT, ["other-src"])
    return graph


def test_plan_walks_every_stage_in_order() -> None:
    plan = plan_propagation(_graph(), "src", reason="SOURCE_DELETED")
    assert [s.stage for s in plan.stages] == list(Stage)
    assert plan.stage(Stage.SOURCE).node_ids == ("src",)
    assert plan.stage(Stage.PROJECTION).node_ids == ("graph", "norm", "retr")
    assert plan.stage(Stage.CHUNK).node_ids == ("chunk-1", "chunk-2")
    assert plan.stage(Stage.EMBEDDING).node_ids == ("emb-1",)
    assert plan.stage(Stage.CACHE).status is StageStatus.CACHE_INVALIDATION_PLANNED
    assert plan.stage(Stage.DERIVED_ARTIFACT).node_ids == ("eval",)
    assert plan.stage(Stage.TRAINING_LINEAGE).status is StageStatus.HUMAN_REVIEW_REQUIRED
    data = plan.to_dict()
    assert data["execution_order"][0] == "TRAINING_LINEAGE" and data["execution_order"][-1] == "SOURCE"
    assert "other-norm" not in json.dumps(data)
    assert data["dry_run"] is True and data["autonomous_deletion"] is False


def test_every_lineage_kind_has_a_status_and_weights_are_unsupported() -> None:
    plan = plan_propagation(_graph(), "src", reason="SOURCE_REVOKED")
    assert {kind for kind, _ in plan.kind_status} == set(LineageKind)
    assert plan.status_of(K.SOURCE) is StageStatus.PAYLOAD_PURGE_PLANNED
    assert plan.status_of(K.TRAINED_ARTIFACT) is StageStatus.HUMAN_REVIEW_REQUIRED
    assert plan.status_of(K.DATASET_REFERENCE) is StageStatus.HUMAN_REVIEW_REQUIRED
    assert plan.status_of(K.CACHE) is StageStatus.CACHE_INVALIDATION_PLANNED
    assert plan.status_of(K.CHUNK) is StageStatus.INVALIDATION_PLANNED
    data = plan.to_dict()
    assert data["model_weight_deletion"] == MODEL_WEIGHT_DELETION == "UNSUPPORTED_REQUIRES_RETRAINING_DECISION"
    assert data["model_weights_deleted"] is False
    assert data["human_review_items"] == ["dataset", "model"]


def test_cache_invalidation_and_retained_authority_metadata() -> None:
    data = plan_propagation(_graph(), "src", reason="SOURCE_DELETED").to_dict()
    assert data["cache_invalidation"]["node_ids"] == ["cache", "graph", "retr"]
    assert data["cache_invalidation"]["action"] == "INVALIDATE"
    retained = data["retained_metadata"]
    assert retained["source_payload_state"] == "METADATA_RETAINED_PAYLOAD_PURGED"
    assert retained["source_identity_hash"] == "src"
    assert set(retained["kinds"]) == {"SOURCE_IDENTITY", "AUDIT_CHAIN", "ARTIFACT_HASH"}
    assert retained["retention"] == ["R3_AUTHORITY"]


def test_plan_reuses_lineage_dry_run_and_is_deterministic() -> None:
    graph = _graph()
    plan = plan_propagation(graph, "src", reason="SOURCE_DELETED")
    assert plan.revocation_plan == graph.plan_revocation("src", reason="SOURCE_DELETED")
    assert plan.plan_hash == plan_propagation(_graph(), "src", reason="SOURCE_DELETED").plan_hash
    assert plan.plan_hash != plan_propagation(graph, "src", reason="OTHER").plan_hash


def test_source_without_derivatives_still_marks_training_lineage_for_review() -> None:
    graph = LineageGraph()
    graph.add("lonely", K.SOURCE)
    plan = plan_propagation(graph, "lonely", reason="SOURCE_DELETED")
    assert plan.stage(Stage.CHUNK).status is StageStatus.NOTHING_TO_PROPAGATE
    assert plan.stage(Stage.TRAINING_LINEAGE).status is StageStatus.NOTHING_TO_PROPAGATE
    assert plan.status_of(K.TRAINED_ARTIFACT) is StageStatus.HUMAN_REVIEW_REQUIRED
    with pytest.raises(KeyError):
        plan_propagation(graph, "missing", reason="SOURCE_DELETED")


def test_apply_requires_exact_hash_and_leaves_human_review_items() -> None:
    graph = _graph()
    plan = plan_propagation(graph, "src", reason="SOURCE_DELETED")
    with pytest.raises(PermissionError, match="NOT_APPROVED"):
        apply_propagation(graph, plan, approved_plan_hash="0" * 64)
    assert not graph.revoked
    marked = apply_propagation(graph, plan, approved_plan_hash=plan.plan_hash)
    assert set(marked) == {"src", "norm", "chunk-1", "chunk-2", "emb-1", "retr", "graph", "cache", "eval"}
    assert not graph.is_revoked("model") and not graph.is_revoked("dataset")
    assert not graph.is_revoked("other-norm")
    assert apply_propagation(graph, plan, approved_plan_hash=plan.plan_hash) == sorted(marked)


def test_stale_plan_is_refused() -> None:
    graph = _graph()
    plan = plan_propagation(graph, "src", reason="SOURCE_DELETED")
    graph.add("chunk-3", K.CHUNK, ["norm"])
    with pytest.raises(ValueError, match="PROPAGATION_PLAN_STALE"):
        apply_propagation(graph, plan, approved_plan_hash=plan.plan_hash)
