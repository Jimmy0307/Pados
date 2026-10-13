"""The new STRUCTURED_EXTRACTION and PASSAGE_RETRIEVAL capabilities: closed error codes (exercised through
the fabric executor), exact deterministic scoring, strict output validators and contract-only backends."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pipeline_support import TASK_PARAMETERS, pipeline

from pados.capability.artifacts import ArtifactRef
from pados.capability.executor import NOT_EXECUTED
from pados.capability.model import Distribution, LifecycleState
from pados.packs.algorithms.contracts import DATASET_KIND, Criterion, FeatureType, parse_dataset
from pados.packs.extraction.table_dataset import dataset_output_valid
from pados.packs.retrieval.engines import embedding_adapter, reranker_adapter
from pados.packs.retrieval.lexical import RANKING_KIND, bm25_scores, load_ranking, ranking_output_valid, tokenize


def _failure(tmp_path: Path, stage_id: str, **parameters: Any) -> tuple[str | None, dict[str, Any]]:
    fab = pipeline(tmp_path)
    merged = {key: value for key, value in {**TASK_PARAMETERS, **parameters}.items() if value is not None}
    _, _, result = fab.run(fab.task(parameters=merged))
    receipts = {stage.stage_id: stage for stage in result.plan_receipt.stages}
    later = list(receipts)[list(receipts).index(stage_id) + 1:]
    assert receipts[stage_id].outputs == () and all(receipts[s].failure_code == NOT_EXECUTED for s in later)
    return receipts[stage_id].failure_code, receipts


@pytest.mark.parametrize(("parameters", "code"), [
    ({"column_2": "Quality|quality|ORDINAL|GAIN|low,medium"}, "EXTRACTION_VALUE_OUT_OF_DOMAIN"),
    ({"column_2": "Quality|quality|NUMERIC|GAIN"}, "EXTRACTION_VALUE_NOT_NUMERIC"),
    ({"column_3": None}, "EXTRACTION_COLUMN_UNDECLARED"),
    ({"column_5": "Weight|weight|NUMERIC|GAIN"}, "EXTRACTION_COLUMN_NOT_FOUND"),
    ({"column_1": "Cost|cost|NUMERIC|BEST"}, "EXTRACTION_COLUMN_SPEC_INVALID"),
    ({"column_1": "Cost|cost|NUMERIC|COST|cheap,dear"}, "EXTRACTION_COLUMN_SPEC_INVALID"),
    ({"column_4": "Rating|cost|ORDINAL|GAIN|poor,fair,good"}, "EXTRACTION_COLUMN_SPEC_INVALID"),
    ({"decision_column": "Score"}, "EXTRACTION_DECISION_COLUMN_UNDECLARED"),
    ({"object_column": None}, "EXTRACTION_PARAMETERS_INVALID"),
    ({"table_ordinal": 2}, "EXTRACTION_TABLE_NOT_FOUND"),
])
def test_extraction_fails_closed_with_a_closed_code(tmp_path: Path, parameters: dict[str, Any], code: str) -> None:
    failure, _ = _failure(tmp_path, "extract", **parameters)
    assert failure == code


@pytest.mark.parametrize(("parameters", "code"), [
    ({"query": "?? -- !!"}, "RETRIEVAL_QUERY_EMPTY"),
    ({"top_k": 0}, "RETRIEVAL_PARAMETERS_INVALID"),
    ({"top_k": True}, "RETRIEVAL_PARAMETERS_INVALID"),
])
def test_retrieval_fails_closed_with_a_closed_code(tmp_path: Path, parameters: dict[str, Any], code: str) -> None:
    failure, receipts = _failure(tmp_path, "retrieve", **parameters)
    assert failure == code and receipts["extract"].failure_code is None


def test_extracted_dataset_carries_the_declared_types_and_criteria(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    _, _, result = fab.run()
    ref = fab.outputs(result)["extract"][0]
    dataset = parse_dataset(fab.store.get(ref))
    assert [(c.name, c.type, c.criterion, c.domain) for c in dataset.columns] == [
        ("cost", FeatureType.NUMERIC, Criterion.COST, ()),
        ("quality", FeatureType.ORDINAL, Criterion.GAIN, ("low", "medium", "high")),
        ("delivery_days", FeatureType.NUMERIC, Criterion.COST, ()),
        ("rating", FeatureType.ORDINAL, Criterion.GAIN, ("poor", "fair", "good"))]
    assert dataset.decision == "rating" and dataset.object_ids == ("P1", "P2", "P3", "P4", "P5", "P6")
    assert dataset.rows[0] == (120.0, "high", 5.0, "good")
    assert dataset_output_valid([(ref, fab.store.get(ref))])
    loose = json.dumps(json.loads(fab.store.get(ref)), indent=1).encode()
    assert not dataset_output_valid([(ref.model_copy(update={"size_bytes": len(loose)}), loose)])
    result_document = json.loads(fab.store.get(fab.outputs(result)["analyze"][0]))
    assert result_document["metrics"]["rule_count"] == 4.0
    assert result_document["metrics"]["quality_of_approximation"] == 1.0


def test_tokenizer_and_bm25_are_exact_and_deterministic() -> None:
    assert tokenize("Ｃost-AND delivery_days 12") == ["cost", "and", "delivery", "days", "12"]
    # idf(b) = ln(1 + (2 - 1 + 0.5) / (1 + 0.5)) = ln 2; tf = 1, dl = 2, avgdl = 1.5, k1 = 1.2, b = 0.75:
    # ln 2 * 2.2 / (1 + 1.2 * 1.25) = 0.88 * ln 2 = 0.6099695... -> 0.609970
    assert bm25_scores(["b", "b"], [["a", "b"], ["a"]], k1=1.2, b=0.75) == [(Decimal("0.609970"), 1),
                                                                           (Decimal("0.000000"), 0)]


def test_ranking_references_passages_without_text(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    _, _, result = fab.run()
    ref = fab.outputs(result)["retrieve"][0]
    payload = fab.store.get(ref)
    ranking = load_ranking(payload)
    assert ref.kind == RANKING_KIND and ranking.passages_total == 5 and len(ranking.results) == 3
    assert [item.passage_id for item in ranking.results] == ["p1.b5", "p1.b2", "p1.b3"]
    assert ranking.results[0].section_id == "s2" and ranking.results[0].score > ranking.results[1].score
    assert b"Lower cost" not in payload and b"delivery" not in payload  # no document or query text
    assert ranking_output_valid([(ref, payload)])
    altered = payload.replace(b'"rank":1', b'"rank":2')
    assert not ranking_output_valid([(ArtifactRef(sha256="0" * 64, media_type=ref.media_type,
                                                  size_bytes=len(altered), kind=RANKING_KIND), altered)])
    assert not ranking_output_valid([(ref.model_copy(update={"kind": DATASET_KIND}), payload)])


def test_embedding_and_reranker_backends_are_contract_only() -> None:
    for adapter in (embedding_adapter(), reranker_adapter()):
        descriptor = adapter.descriptor
        assert adapter.available() is False
        assert descriptor.license.distribution is Distribution.RUNTIME_OPTIONAL
        assert descriptor.license.spdx == "LicenseRef-Model-Specific"
        assert descriptor.lifecycle is LifecycleState.EXPERIMENTAL and descriptor.resources.gpu_required
