# SPDX-License-Identifier: AGPL-3.0-only
"""Stage-level contracts of the Document Intelligence adapters, their manifests and the evidence derivation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from di_support import corpus, engines

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts, ScopedArtifacts
from pados.capability.pack import register_pack
from pados.capability.pack_operability import check
from pados.capability.registry import CapabilityRegistry
from pados.enterprise.classification import DataClassification
from pados.packs.document_intelligence import deterministic_stages as det
from pados.packs.document_intelligence.evidence import build_evidence, statement, token
from pados.packs.document_intelligence.model_stages import (
    CrossEncoderRerankAdapter,
    DenseRetrievalAdapter,
    config_digest,
    scored_reference_check,
)
from pados.packs.document_intelligence.ranking import (
    SCORED_KIND,
    DocumentIntelligenceError,
    load_scored,
    scored_output_valid,
)
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, KIND_SOURCE, PDF_MEDIA_TYPE, OcrDocument
from pados.packs.problem_solving.contracts import EIGHT_D
from pados.packs.retrieval.lexical import LexicalRetrievalAdapter, passages

CASE1 = (corpus.case("case.ncr-2041"),)
QUERY = "why did the pump housings crack"


class Bench:
    def __init__(self, tmp_path: Path) -> None:
        self.store = ObjectStoreArtifacts(tmp_path / "objects")
        self.embedder, self.encoder = engines(tmp_path / "models")

    def document(self, cases: tuple[corpus.Case, ...] = CASE1) -> ArtifactRef:
        source = self.store.put(corpus.binder_pdf(cases), media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
        built: Any = corpus.ocr_document(source, cases)
        return self.store.put(built.to_bytes(), media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)

    def run(self, adapter: Any, capability_id: str, inputs: tuple[ArtifactRef, ...],
            parameters: dict[str, Any]) -> StageOutput:
        scoped = ScopedArtifacts(self.store, readable=inputs, max_outputs=8, max_output_bytes=8 * 1024 * 1024)
        request = StageRequest("stage", capability_id, inputs, parameters, DataClassification.CONFIDENTIAL)
        return adapter.execute(request, context=StageContext(artifacts=scoped, project_id="proj-a", stage_id="stage"))

    def lexical(self, document: ArtifactRef, query: str = QUERY) -> ArtifactRef:
        return self.run(LexicalRetrievalAdapter(), "PASSAGE_RETRIEVAL", (document,), {"query": query}).outputs[0]

    def dense(self, document: ArtifactRef, query: str = QUERY) -> ArtifactRef:
        adapter = DenseRetrievalAdapter(self.embedder)
        return self.run(adapter, "PASSAGE_RETRIEVAL_DENSE", (document,), {"query": query}).outputs[0]


def _refused(call: Any) -> str:
    with pytest.raises(CapabilityAdapterError) as excinfo:
        call()
    assert excinfo.value.__cause__ is None
    return excinfo.value.code


def test_dense_ranking_is_schema_valid_and_names_its_model(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    ref = bench.dense(bench.document())
    payload = bytes(bench.store.get(ref))
    ranking = load_scored(payload)
    assert ranking.method == "DENSE_COSINE" and ranking.model_pin_id == bench.embedder.pin.pin_id
    assert scored_output_valid(((ref, payload),))
    assert scored_reference_check(bench.embedder, bench.encoder)(((ref, payload),))
    assert scored_reference_check(bench.encoder)(((ref, payload),)) is False  # an unregistered pin never passes
    assert b"pump" not in payload.lower()  # rankings carry references, never text


def test_fusion_refuses_a_ranking_of_another_document_or_query(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document, foreign = bench.document(), bench.document((corpus.case("case.ncr-2057"),))
    lexical, dense = bench.lexical(document), bench.dense(document)
    fusion = det.FusionAdapter()
    output = bench.run(fusion, det.PASSAGE_RANK_FUSION, (document, lexical, dense), {"query": QUERY})
    fused = load_scored(bytes(bench.store.get(output.outputs[0])))
    assert fused.method == "RRF_FUSION" and fused.input_rankings == (lexical.sha256, dense.sha256)
    assert _refused(lambda: bench.run(fusion, det.PASSAGE_RANK_FUSION, (foreign, lexical, dense),
                                      {"query": QUERY})) == "DI_RANKING_DOCUMENT_MISMATCH"
    assert _refused(lambda: bench.run(fusion, det.PASSAGE_RANK_FUSION, (document, lexical, dense),
                                      {"query": "a different question"})) == "DI_RANKING_QUERY_MISMATCH"
    assert _refused(lambda: bench.run(fusion, det.PASSAGE_RANK_FUSION, (document, lexical),
                                      {"query": QUERY})) == "DI_INPUT_COUNT_INVALID"


def test_fusion_is_deterministic(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document = bench.document()
    inputs = (document, bench.lexical(document), bench.dense(document))
    first = bench.run(det.FusionAdapter(), det.PASSAGE_RANK_FUSION, inputs, {"query": QUERY})
    again = bench.run(det.FusionAdapter(), det.PASSAGE_RANK_FUSION, inputs, {"query": QUERY})
    assert first.outputs[0].sha256 == again.outputs[0].sha256


@pytest.mark.parametrize("parameters", [{"query": QUERY, "top_k": True}, {"query": QUERY, "extra": 1},
                                        {"query": ""}, {"query": QUERY, "mode": "DISABLED"}])
def test_rerank_parameters_fail_closed(tmp_path: Path, parameters: dict[str, Any]) -> None:
    bench = Bench(tmp_path)
    document = bench.document()
    adapter = CrossEncoderRerankAdapter(bench.encoder)
    assert _refused(lambda: bench.run(adapter, "PASSAGE_RERANK_CROSS_ENCODER", (document, bench.lexical(document)),
                                      parameters)) == "DI_PARAMETERS_INVALID"


def test_active_rerank_publishes_the_model_order(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document = bench.document()
    adapter = CrossEncoderRerankAdapter(bench.encoder)
    output = bench.run(adapter, "PASSAGE_RERANK_CROSS_ENCODER", (document, bench.lexical(document)),
                       {"query": QUERY, "mode": "ACTIVE", "top_k": 5})
    ranking = load_scored(bytes(bench.store.get(output.outputs[0])))
    assert ranking.rerank_mode == "ACTIVE" and output.evidence["rerank_outcome"] == "APPLIED"
    assert [item.score for item in ranking.results] == [item.model_score for item in ranking.results]
    assert ranking.shadow_order == ()


def test_a_tampered_scored_ranking_is_not_schema_valid(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    ref = bench.dense(bench.document())
    payload = json.loads(bytes(bench.store.get(ref)))
    payload["results"][0]["score"], payload["results"][1]["score"] = -1.0, 5.0  # out of order
    forged = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert not scored_output_valid(((ArtifactRef.model_validate({**ref.model_dump(), "sha256": "0" * 64,
                                                                 "size_bytes": len(forged)}), forged),))
    with pytest.raises(DocumentIntelligenceError):
        load_scored(json.dumps(json.loads(forged), indent=1).encode())


def test_evidence_items_come_only_from_labelled_profile_subjects(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    document = OcrDocument.from_bytes(bytes(bench.store.get(bench.document())))
    units = passages(document)
    pack, mapped = build_evidence(units, case_id="case.ncr-2041", source_id="src.ncr-2041", profile=EIGHT_D,
                                  max_items=64)
    structured = {(item.subject, item.assertion) for item in pack.items if item.subject}
    assert ("root_cause", "resin_moisture_above_specification_after_the_hopper_dryer_failed") in structured
    assert all(subject in {s for section in EIGHT_D.sections for s in section.subjects} for subject, _ in structured)
    # "Summary:", "Discussion:" and "Warehouse note:" are not profile subjects: statement-only, never groundable.
    assert all(item.subject is None for item in pack.items if item.statement.startswith(("Summary", "Discussion")))
    assert len(mapped) == len(pack.items) and len({m.evidence_id for m in mapped}) == len(mapped)
    small, _ = build_evidence(units, case_id="case.ncr-2041", source_id="src.ncr-2041", profile=EIGHT_D, max_items=3)
    assert len(small.items) == 3


@pytest.mark.parametrize(("text", "numeric", "expected"), [
    ("Root cause", False, "root_cause"), ("  Corrective Action ", False, "corrective_action"),
    ("12 ppm", True, "v_12_ppm"), ("12 ppm", False, None), ("___", False, None), ("Ｒｏｏｔ　ｃａｕｓｅ", False, "root_cause")])
def test_tokens_are_normalized_deterministically(text: str, numeric: bool, expected: str | None) -> None:
    assert token(text, numeric_prefix=numeric) == expected


def test_statements_are_printable_and_bounded() -> None:
    value = statement("a‮b​c\n" + "x" * 1000)
    assert "‮" not in value and "​" not in value and "\n" not in value and len(value) <= 280


def test_every_document_intelligence_manifest_meets_the_operability_contract(tmp_path: Path) -> None:
    embedder, encoder = engines(tmp_path)
    for manifest in (DenseRetrievalAdapter(embedder).manifest, CrossEncoderRerankAdapter(encoder).manifest,
                     det.MANIFEST):
        assert check(manifest) == ()
    dense = DenseRetrievalAdapter(embedder).manifest
    assert dense.runtime.execution_config_digest == config_digest(embedder.pin, embedder.identity)
    assert [f.code for f in dense.failure_codes if f.retryable] == ["LOCAL_MODEL_TIMEOUT"]
    assert dense.adapters[0].license.spdx == "Apache-2.0" and det.MANIFEST.adapters[0].license.spdx == "AGPL-3.0-only"


def test_the_config_digest_binds_the_model_pin(tmp_path: Path) -> None:
    embedder, _ = engines(tmp_path)
    changed = embedder.pin.model_copy(update={"max_batch": 2})
    assert config_digest(embedder.pin, embedder.identity) != config_digest(changed, embedder.identity)
    assert config_digest(embedder.pin, embedder.identity) != config_digest(embedder.pin, None)


def test_packs_register_through_the_existing_registry_only(tmp_path: Path) -> None:
    embedder, _ = engines(tmp_path)
    registry = CapabilityRegistry()
    adapter = DenseRetrievalAdapter(embedder)
    registration = register_pack(registry, adapter.manifest, {adapter.descriptor.adapter_id: adapter},
                                 operator="operator.test")
    assert registration.adapter_ids == (adapter.descriptor.adapter_id,) and registration.semantic_authority == "NONE"


def test_engine_failures_surface_as_declared_closed_codes(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    embedder, _ = engines(tmp_path / "missing", installed=False)
    adapter = DenseRetrievalAdapter(embedder)
    assert adapter.available() is False
    assert _refused(lambda: bench.run(adapter, "PASSAGE_RETRIEVAL_DENSE", (bench.document(),),
                                      {"query": QUERY})) == "ENGINE_NOT_INSTALLED"
    assert "ENGINE_NOT_INSTALLED" in {f.code for f in adapter.manifest.failure_codes}


def test_kind_constants_are_distinct() -> None:
    assert len({SCORED_KIND, det.EVIDENCE_MAP_KIND, KIND_DOCUMENT}) == 3
