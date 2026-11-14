# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic stages: reciprocal-rank fusion and the passage -> evidence-pack hand-off (Capability Pack V1).

``PASSAGE_RANK_FUSION`` fuses two to four rankings of ONE document for ONE query (BM25 and dense, typically) with
the enterprise pipeline's own ``reciprocal_rank_fusion`` -- one formula for both retrieval stacks. Every input
ranking must name this stage's document and query hash; a ranking of another document or query is refused, never
silently mixed. ``PASSAGE_EVIDENCE_PACK`` turns a final ranking into a ``problem_solving.evidence-pack/v1`` plus an
evidence map (``evidence``). Both are DETERMINISTIC, so the executor verifies them by replay as well as by schema.

The evidence pack is a CANDIDATE: a Structured Problem Solving task admits it only when the task pins its digest
and authorizes its source (``admit_evidence``); this stage cannot authorize anything.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability.adapter import StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    LifecycleState,
    Modality,
    ResourceRequirement,
    VerificationMethod,
)
from pados.capability.pack import (
    CORE_EVIDENCE,
    CapabilityPackManifest,
    CheckerSpec,
    Egress,
    FailureCodeSpec,
    PackProvenance,
    PackProvider,
    PackRuntime,
    PackSecurity,
    PackSource,
    RuntimeType,
    outputs_digest,
)
from pados.capability_loop.contracts import CapabilityState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.pipeline import reciprocal_rank_fusion
from pados.execution.context import canonical_hash
from pados.packs.algorithms.contracts import canonical_json, validated
from pados.packs.document_intelligence.boundary import closed
from pados.packs.document_intelligence.evidence import build_evidence, evidence_map
from pados.packs.document_intelligence.ranking import (
    MAX_INPUT_RANKINGS,
    MAX_RESULTS,
    SCORED_CHECKER_ID,
    SCORED_CONTRACT,
    SCORED_KIND,
    DocumentIntelligenceError,
    ScoredPassage,
    ScoredRanking,
    document_input,
    input_ranking,
    query_identity,
    require_bound,
    scored_payload,
    unicode_version,
)
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, OCR_DOCUMENT_CONTRACT
from pados.packs.problem_solving.contracts import PROFILES
from pados.packs.problem_solving.pack import KIND_EVIDENCE, load_evidence_pack

PASSAGE_RANK_FUSION = "PASSAGE_RANK_FUSION"
PASSAGE_EVIDENCE_PACK = "PASSAGE_EVIDENCE_PACK"
FUSION_ADAPTER_ID = "document-intelligence.rrf-fusion"
EVIDENCE_ADAPTER_ID = "document-intelligence.passage-evidence"
PACK_ID = "pados.document-intelligence.deterministic"
EVIDENCE_CONTRACT: Final = "document_intelligence.passage-evidence/v1"
EVIDENCE_MAP_KIND: Final = "document_intelligence.evidence-map"
EVIDENCE_CHECKER_ID = "document-intelligence.passage-evidence.schema"
RUNTIME_VERSION = "1.0.0"
RULES_VERSION = "pados.document-intelligence.deterministic.v1"
CONFIG_DIGEST = canonical_hash({"rules": RULES_VERSION, "fusion": "rrf", "profiles": sorted(PROFILES)})
MAX_EVIDENCE_ITEMS = 64
EVIDENCE_KEYS = (*CORE_EVIDENCE, "output_contract", "input_document_sha256", "passages_total", "results")
FAILURES = ("DI_PARAMETERS_INVALID", "DI_INPUT_COUNT_INVALID", "DI_TOO_MANY_PASSAGES", "DI_INPUT_NOT_DOCUMENT", "DI_DOCUMENT_INVALID",
            "DI_INPUT_UNREADABLE", "DI_NO_PASSAGES", "DI_QUERY_EMPTY", "DI_QUERY_TOO_LONG", "DI_SEED_NOT_APPLICABLE",
            "DI_RANKING_NOT_MINIMIZED", "DI_INPUT_NOT_RANKING", "DI_INPUT_RANKING_INVALID",
            "DI_RANKING_DOCUMENT_MISMATCH", "DI_RANKING_QUERY_MISMATCH", "DI_EVIDENCE_EMPTY",
            "DI_EVIDENCE_INVALID", "DI_UNEXPECTED_ERROR")
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class _Strict(BaseModel):
    model_config = _FROZEN

    @model_validator(mode="before")
    @classmethod
    def _no_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(isinstance(item, bool) for item in value.values()):
            raise ValueError("DI_PARAMETERS_INVALID")
        return value


class FusionParameters(_Strict):
    query: str = Field(min_length=1, max_length=256)
    top_k: int = Field(default=10, ge=1, le=MAX_RESULTS)
    rrf_k: int = Field(default=60, ge=1, le=1000)


class EvidenceParameters(_Strict):
    case_id: str = Field(pattern=_ID.pattern)
    source_id: str = Field(pattern=_ID.pattern)
    profile: str = Field(min_length=1, max_length=32)
    max_items: int = Field(default=16, ge=1, le=MAX_EVIDENCE_ITEMS)

    @model_validator(mode="after")
    def _known_profile(self) -> EvidenceParameters:
        if self.profile not in PROFILES:
            raise ValueError("DI_PARAMETERS_INVALID")
        return self


def _capability(capability_id: str, operation: str, input_contract: str, output_contract: str,
                description: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, version="1", family=CapabilityFamily.RETRIEVAL, operation=operation,
        input_contract=input_contract, output_contract=output_contract, determinism=Determinism.DETERMINISTIC,
        modalities=(Modality.TEXT,), max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY,
        verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
        failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
        description=description)


FUSION_CAPABILITY = _capability(PASSAGE_RANK_FUSION, "FUSE_RANKINGS", SCORED_CONTRACT, SCORED_CONTRACT,
                                "Reciprocal-rank fusion of passage rankings of one document and query")
EVIDENCE_CAPABILITY = _capability(PASSAGE_EVIDENCE_PACK, "BUILD_EVIDENCE_PACK", OCR_DOCUMENT_CONTRACT,
                                  EVIDENCE_CONTRACT, "Ranked passages to a problem-solving evidence pack")


def _adapter(adapter_id: str, capability_id: str) -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=adapter_id, capability_id=capability_id, version="1", backend="python",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, resources=ResourceRequirement(cpu_cores=1),
        license=LicenseInfo(spdx="AGPL-3.0-only"), lifecycle=LifecycleState.EXPERIMENTAL)


FUSION_ADAPTER = _adapter(FUSION_ADAPTER_ID, PASSAGE_RANK_FUSION)
EVIDENCE_ADAPTER = _adapter(EVIDENCE_ADAPTER_ID, PASSAGE_EVIDENCE_PACK)

MANIFEST = CapabilityPackManifest(
    pack_id=PACK_ID, version="1",
    provider=PackProvider(provider_id="pados.document-intelligence", runtime_id="python",
                          runtime_version=RUNTIME_VERSION),
    provenance=PackProvenance(source=PackSource.IN_TREE, license=LicenseInfo(spdx="AGPL-3.0-only"), publisher="pados"),
    capability_ids=(PASSAGE_RANK_FUSION, PASSAGE_EVIDENCE_PACK), capabilities=(FUSION_CAPABILITY, EVIDENCE_CAPABILITY),
    adapters=(FUSION_ADAPTER, EVIDENCE_ADAPTER),
    runtime=PackRuntime(runtime_type=RuntimeType.IN_PROCESS, device=(ResourceRequirement(cpu_cores=1),),
                        execution_config_digest=CONFIG_DIGEST, timeout_seconds=60,
                        network=NetworkRequirement.LOCAL_ONLY),
    security=PackSecurity(classification_ceiling=DataClassification.RESTRICTED, egress=Egress.NONE),
    evidence_requirements=EVIDENCE_KEYS,
    verification_hooks=(CheckerSpec(output_contract=SCORED_CONTRACT, checker_id=SCORED_CHECKER_ID),
                        CheckerSpec(output_contract=EVIDENCE_CONTRACT, checker_id=EVIDENCE_CHECKER_ID)),
    initial_state=CapabilityState.EXPERIMENTAL,
    failure_codes=tuple(FailureCodeSpec(code=code) for code in FAILURES))


def _evidence(output_refs: tuple[ArtifactRef, ...], document_sha256: str, passages_total: int,
              results: int, contract: str) -> dict[str, str | int | float | bool | None]:
    return {"runtime_version": RUNTIME_VERSION, "device": "cpu", "config_digest": CONFIG_DIGEST,
            "provider_id": MANIFEST.provider.provider_id, "artifact_digest": outputs_digest(output_refs),
            "output_contract": contract, "input_document_sha256": document_sha256,
            "passages_total": passages_total, "results": results}


class _DeterministicAdapter:
    _descriptor: AdapterDescriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        return closed(self._execute, request, context)

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        raise NotImplementedError


class FusionAdapter(_DeterministicAdapter):
    """PASSAGE_RANK_FUSION: inputs are the document and 2-4 rankings (lexical or scored) of it."""

    def __init__(self) -> None:
        self._descriptor = FUSION_ADAPTER

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.seed is not None:
            raise DocumentIntelligenceError("DI_SEED_NOT_APPLICABLE")
        if not 3 <= len(request.inputs) <= MAX_INPUT_RANKINGS + 1:
            raise DocumentIntelligenceError("DI_INPUT_COUNT_INVALID")
        parameters = validated(FusionParameters, dict(request.parameters), "DI_PARAMETERS_INVALID")
        ref, document, units = document_input(request, context)
        query_sha256, terms = query_identity(parameters.query)
        index = {unit.passage_id: position for position, unit in enumerate(units)}
        named: list[tuple[str, list[tuple[str, float]]]] = []
        sources: list[str] = []
        for position, item in enumerate(item for item in request.inputs if item is not ref):
            ranking = input_ranking(item, context.artifacts.get(item))
            require_bound(ranking, ref.sha256, query_sha256)
            if any(result.passage_id not in index for result in ranking.results):
                raise DocumentIntelligenceError("DI_INPUT_RANKING_INVALID")
            named.append((f"r{position}", [(result.passage_id, result.score) for result in ranking.results]))
            sources.append(item.sha256)
        fused = reciprocal_rank_fusion(named, k=parameters.rrf_k)
        order = sorted(fused, key=lambda pid: (-fused[pid]["fused"], index[pid]))[:parameters.top_k]
        if not order:
            raise DocumentIntelligenceError("DI_INPUT_RANKING_INVALID")
        by_id = {unit.passage_id: unit for unit in units}
        ranking_out = ScoredRanking(
            method="RRF_FUSION", source_document_sha256=ref.sha256, source_sha256=document.source.sha256,
            query_sha256=query_sha256, query_terms=terms, unicode_version=unicode_version(),
            passages_total=len(units), top_k=parameters.top_k, input_rankings=tuple(sources),
            fusion_k=parameters.rrf_k,
            results=tuple(ScoredPassage(rank=position, passage_id=pid, section_id=by_id[pid].section_id,
                                        page=by_id[pid].page, char_start=by_id[pid].char_start,
                                        char_end=by_id[pid].char_end, score=fused[pid]["fused"])
                          for position, pid in enumerate(order, start=1)))
        out = context.artifacts.put(scored_payload(ranking_out), media_type=JSON_MEDIA_TYPE, kind=SCORED_KIND)
        return StageOutput(outputs=(out,), metrics={"inputs": float(len(named)), "results": float(len(order))},
                           evidence=_evidence((out,), ref.sha256, len(units), len(order), SCORED_CONTRACT))


class PassageEvidenceAdapter(_DeterministicAdapter):
    """PASSAGE_EVIDENCE_PACK: inputs are the document and exactly one final ranking of it."""

    def __init__(self) -> None:
        self._descriptor = EVIDENCE_ADAPTER

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.seed is not None:
            raise DocumentIntelligenceError("DI_SEED_NOT_APPLICABLE")
        if len(request.inputs) != 2:
            raise DocumentIntelligenceError("DI_INPUT_COUNT_INVALID")
        parameters = validated(EvidenceParameters, dict(request.parameters), "DI_PARAMETERS_INVALID")
        ref, document, units = document_input(request, context)
        (ranking_ref,) = [item for item in request.inputs if item is not ref]
        ranking = input_ranking(ranking_ref, context.artifacts.get(ranking_ref))
        if ranking.source_document_sha256 != ref.sha256:
            raise DocumentIntelligenceError("DI_RANKING_DOCUMENT_MISMATCH")
        by_id = {unit.passage_id: unit for unit in units}
        if any(result.passage_id not in by_id for result in ranking.results):
            raise DocumentIntelligenceError("DI_INPUT_RANKING_INVALID")
        ranked = [by_id[result.passage_id] for result in ranking.results]
        if not ranked:
            raise DocumentIntelligenceError("DI_EVIDENCE_EMPTY")
        try:
            pack, mapped = build_evidence(ranked, case_id=parameters.case_id, source_id=parameters.source_id,
                                          profile=PROFILES[parameters.profile], max_items=parameters.max_items)
        except ValueError:
            pack = None
        if pack is None:  # outside the handler: the validation text is never chained
            raise DocumentIntelligenceError("DI_EVIDENCE_INVALID")
        pack_ref = context.artifacts.put(canonical_json(pack.model_dump(mode="json")), media_type=JSON_MEDIA_TYPE,
                                         kind=KIND_EVIDENCE)
        map_ref = context.artifacts.put(
            canonical_json(evidence_map(pack, mapped, document_sha256=ref.sha256,
                                        source_sha256=document.source.sha256, ranking_sha256=ranking_ref.sha256)),
            media_type=JSON_MEDIA_TYPE, kind=EVIDENCE_MAP_KIND)
        refs = (pack_ref, map_ref)
        structured = sum(1 for item in pack.items if item.subject is not None)
        return StageOutput(outputs=refs, metrics={"items": float(len(pack.items)), "structured": float(structured)},
                           evidence=_evidence(refs, ref.sha256, len(units), len(pack.items), EVIDENCE_CONTRACT))


def evidence_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``document_intelligence.passage-evidence/v1``: a canonical, valid evidence pack and a map
    that names exactly that pack's items and digest."""
    found = {ref.kind: data for ref, data in outputs}
    if len(outputs) != 2 or set(found) != {KIND_EVIDENCE, EVIDENCE_MAP_KIND}:
        return False
    try:
        pack = load_evidence_pack(found[KIND_EVIDENCE])
        mapping = json.loads(found[EVIDENCE_MAP_KIND].decode("ascii"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(mapping, dict) or canonical_json(mapping) != found[EVIDENCE_MAP_KIND]:
        return False
    items = mapping.get("items")
    return (mapping.get("evidence_pack_digest") == pack.digest and isinstance(items, list)
            and [entry.get("evidence_id") for entry in items if isinstance(entry, dict)]
            == [item.evidence_id for item in pack.items])

