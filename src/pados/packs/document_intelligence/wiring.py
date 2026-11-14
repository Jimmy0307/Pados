# SPDX-License-Identifier: AGPL-3.0-only
"""Operator wiring for the Document Intelligence workflow (extensions of ``wire_pipeline``; grants nothing).

Two governed tasks, each planned and executed by the EXISTING fabric planner and executor:

1. ``document_evidence.<start>.<retrieval>``: document -> (OCR stages when starting from a PDF) -> BM25 ->
   [dense -> RRF fusion -> [cross-encoder rerank]] -> ``PASSAGE_EVIDENCE_PACK``. Its verified output is an evidence
   pack CANDIDATE plus the evidence map.
2. ``problem_solving.document``: the existing ``STRUCTURED_PROBLEM_SOLVING`` pack over that evidence pack. The task
   (PADOS, never the pack or a provider) pins the pack digest and names the authorized source as task
   parameters, so a stale or foreign pack is refused by the existing ``admit_evidence``.

The verified report reaches a ``ResultCandidate`` only through the existing work-protocol functions; see
``pados.packs.document_intelligence.workflow``. Rerank mode is fixed by the operator's template (SHADOW by default;
ACTIVE is a separate template), never by a task.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pados.capability.adapter import CapabilityAdapter
from pados.capability.graph import DecompositionTemplate, TemplateRegistry
from pados.capability.pack import register_pack
from pados.capability.registry import CapabilityRegistry
from pados.capability.task import Scalar
from pados.capability.verification import ValidatorRegistry
from pados.packs.document_intelligence import deterministic_stages as det
from pados.packs.document_intelligence.local_models import PinnedCrossEncoder, PinnedTextEmbedder
from pados.packs.document_intelligence.model_stages import (
    PASSAGE_RERANK_CROSS_ENCODER,
    PASSAGE_RETRIEVAL_DENSE,
    CrossEncoderRerankAdapter,
    DenseRetrievalAdapter,
    scored_reference_check,
)
from pados.packs.document_intelligence.ranking import (
    SCORED_CHECKER_ID,
    SCORED_CONTRACT,
    SCORED_KIND,
    scored_output_valid,
)
from pados.packs.intelligence.template import OCR_PARAMETERS
from pados.packs.intelligence.wiring import WiringExtension
from pados.packs.ocr.contracts import KIND_DOCUMENT, KIND_PREPROCESSED
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_OCR,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    TABLE_EXTRACTION,
)
from pados.packs.problem_solving import pack as ps
from pados.packs.retrieval.lexical import PASSAGE_RETRIEVAL, RANKING_KIND

Start = Literal["pdf", "document"]
Retrieval = Literal["lexical", "hybrid", "hybrid_rerank"]
DOCUMENT_INPUT: Final = "document"
EVIDENCE_INPUT: Final = "evidence"
SOLVE_TASK_TYPE: Final = "problem_solving.document"
SOLVE_TEMPLATE_ID: Final = "document-intelligence.problem-solving"
SCHEMA_CHECKER: Final = "document-intelligence.schema"
EVIDENCE_TASK_PARAMETERS: Final = ("case_id", "source_id", "profile")
SOLVE_TASK_PARAMETERS: Final = ("profile", "evidence_pack_digest", "authorized_sources")
CANDIDATE_POOL: Final = 20
FINAL_TOP_K: Final = 8


def task_type(start: Start, retrieval: Retrieval, rerank_mode: Literal["SHADOW", "ACTIVE"] = "SHADOW") -> str:
    suffix = "" if retrieval != "hybrid_rerank" or rerank_mode == "SHADOW" else ".active"
    return f"document_evidence.{start}.{retrieval}{suffix}"


def _task(name: str) -> dict[str, Any]:
    return {"source": "TASK_INPUT", "name": name}


def _stage(name: str, kind: str | None = None) -> dict[str, Any]:
    return {"source": "STAGE_OUTPUT", "name": name, "artifact_kind": kind}


def _ocr_stages() -> list[dict[str, Any]]:
    ocr: Mapping[str, Scalar] = dict(OCR_PARAMETERS)
    return [
        {"stage_id": "preprocess", "capability_id": DOCUMENT_PREPROCESS, "inputs": [_task(DOCUMENT_INPUT)],
         "parameters": ocr},
        {"stage_id": "recognize", "capability_id": DOCUMENT_OCR,
         "inputs": [_task(DOCUMENT_INPUT), _stage("preprocess", KIND_PREPROCESSED)], "parameters": ocr},
        {"stage_id": "layout", "capability_id": DOCUMENT_LAYOUT_ANALYSIS,
         "inputs": [_stage("recognize", KIND_DOCUMENT)], "parameters": ocr},
        {"stage_id": "tables", "capability_id": TABLE_EXTRACTION, "inputs": [_stage("layout", KIND_DOCUMENT)],
         "parameters": ocr},
        {"stage_id": "structure", "capability_id": DOCUMENT_STRUCTURE_PARSE,
         "inputs": [_stage("tables", KIND_DOCUMENT)], "parameters": ocr},
    ]


def evidence_stages(start: Start, retrieval: Retrieval,
                    rerank_mode: Literal["SHADOW", "ACTIVE"] = "SHADOW") -> list[dict[str, Any]]:
    stages = _ocr_stages() if start == "pdf" else []
    document = _stage("structure", KIND_DOCUMENT) if start == "pdf" else _task(DOCUMENT_INPUT)
    query = ["query"]
    stages.append({"stage_id": "retrieve", "capability_id": PASSAGE_RETRIEVAL, "inputs": [document],
                   "parameters": {"top_k": CANDIDATE_POOL, "k1": 1.2, "b": 0.75}, "task_parameters": query})
    final = _stage("retrieve", RANKING_KIND)
    if retrieval != "lexical":
        stages.append({"stage_id": "dense", "capability_id": PASSAGE_RETRIEVAL_DENSE, "inputs": [document],
                       "parameters": {"top_k": CANDIDATE_POOL}, "task_parameters": query})
        stages.append({"stage_id": "fuse", "capability_id": det.PASSAGE_RANK_FUSION,
                       "inputs": [document, _stage("retrieve", RANKING_KIND), _stage("dense", SCORED_KIND)],
                       "parameters": {"top_k": CANDIDATE_POOL, "rrf_k": 60}, "task_parameters": query})
        final = _stage("fuse", SCORED_KIND)
    if retrieval == "hybrid_rerank":
        stages.append({"stage_id": "rerank", "capability_id": PASSAGE_RERANK_CROSS_ENCODER,
                       "inputs": [document, _stage("fuse", SCORED_KIND)],
                       "parameters": {"top_k": FINAL_TOP_K, "mode": rerank_mode}, "task_parameters": query})
        final = _stage("rerank", SCORED_KIND)
    stages.append({"stage_id": "evidence", "capability_id": det.PASSAGE_EVIDENCE_PACK, "inputs": [document, final],
                   "parameters": {"max_items": 16}, "task_parameters": list(EVIDENCE_TASK_PARAMETERS)})
    return stages


def evidence_template(start: Start, retrieval: Retrieval,
                      rerank_mode: Literal["SHADOW", "ACTIVE"] = "SHADOW") -> DecompositionTemplate:
    kind = task_type(start, retrieval, rerank_mode)
    return DecompositionTemplate.model_validate({
        "template_id": f"document-intelligence.{kind.removeprefix('document_evidence.')}", "version": "1",
        "task_type": kind, "graph": {"stages": evidence_stages(start, retrieval, rerank_mode)}})


def solve_template() -> DecompositionTemplate:
    return DecompositionTemplate.model_validate({
        "template_id": SOLVE_TEMPLATE_ID, "version": "1", "task_type": SOLVE_TASK_TYPE,
        "graph": {"stages": [{"stage_id": "solve", "capability_id": ps.CAPABILITY_ID,
                              "inputs": [_task(EVIDENCE_INPUT)], "parameters": {},
                              "task_parameters": list(SOLVE_TASK_PARAMETERS)}]}})


def all_evidence_templates() -> tuple[DecompositionTemplate, ...]:
    starts: tuple[Start, ...] = ("pdf", "document")
    retrievals: tuple[Retrieval, ...] = ("lexical", "hybrid", "hybrid_rerank")
    shadow = [evidence_template(start, retrieval) for start in starts for retrieval in retrievals]
    return (*shadow, *(evidence_template(start, "hybrid_rerank", "ACTIVE") for start in starts))


def stage_grants(*templates: DecompositionTemplate) -> tuple[str, ...]:
    return tuple(sorted({f"CAPABILITY:{stage.capability_id}" for template in templates
                         for stage in template.graph.stages}))


def document_intelligence_extension(embedder: PinnedTextEmbedder, encoder: PinnedCrossEncoder, *,
                                    operator: str) -> WiringExtension:
    """Registers the three Document Intelligence packs, their validators and the evidence templates."""
    def extend(registry: CapabilityRegistry, templates: TemplateRegistry, schemas: ValidatorRegistry,
               references: ValidatorRegistry) -> Mapping[str, CapabilityAdapter]:
        dense, rerank = DenseRetrievalAdapter(embedder), CrossEncoderRerankAdapter(encoder)
        fusion, evidence = det.FusionAdapter(), det.PassageEvidenceAdapter()
        installed: dict[str, CapabilityAdapter] = {}
        for manifest, implementations in ((dense.manifest, {dense.descriptor.adapter_id: dense}),
                                          (rerank.manifest, {rerank.descriptor.adapter_id: rerank}),
                                          (det.MANIFEST, {fusion.descriptor.adapter_id: fusion,
                                                          evidence.descriptor.adapter_id: evidence})):
            registration = register_pack(registry, manifest, implementations, operator=operator)
            for adapter_id in registration.adapter_ids:
                installed[adapter_id] = implementations[adapter_id]
        schemas.register(SCORED_CONTRACT, SCHEMA_CHECKER, scored_output_valid)
        schemas.register(det.EVIDENCE_CONTRACT, det.EVIDENCE_CHECKER_ID, det.evidence_output_valid)
        references.register(SCORED_CONTRACT, SCORED_CHECKER_ID, scored_reference_check(embedder, encoder))
        for template in all_evidence_templates():
            templates.register(template)
        return installed
    return extend


def problem_solving_extension(provider: ps.ClaimProvider, *, operator: str,
                              critic: ps.Critic | None = None) -> WiringExtension:
    """Registers the existing Structured Problem Solving pack and its validators for the grounding task."""
    def extend(registry: CapabilityRegistry, templates: TemplateRegistry, schemas: ValidatorRegistry,
               references: ValidatorRegistry) -> Mapping[str, CapabilityAdapter]:
        adapter = ps.ProblemSolvingAdapter(provider, critic=critic)
        register_pack(registry, ps.MANIFEST, {ps.ADAPTER_ID: adapter}, operator=operator)
        schemas.register(ps.OUTPUT_CONTRACT, SCHEMA_CHECKER, ps.report_output_valid)
        references.register(ps.OUTPUT_CONTRACT, ps.CHECKER_ID, ps.report_reference_check)
        templates.register(solve_template())
        return {ps.ADAPTER_ID: adapter}
    return extend
