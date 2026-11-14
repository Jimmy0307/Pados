# SPDX-License-Identifier: AGPL-3.0-only
"""Model-backed stages: dense passage retrieval and cross-encoder reranking (Capability Pack V1).

``PASSAGE_RETRIEVAL_DENSE`` embeds the query and every reading-order passage of an ``ocr.document/v1`` with the
pinned embedding model and ranks passages by cosine similarity. ``PASSAGE_RERANK_CROSS_ENCODER`` scores a prior
ranking's passages against the query with the pinned cross-encoder through the EXISTING enterprise ``rerank``
function: in SHADOW mode the published order stays the prior one and the reranker's order is recorded beside it;
in ACTIVE mode the reranked order is published and any reranker failure fails the stage closed (no silent
fallback to the prior order).

Each stage is its own pack manifest whose ``execution_config_digest`` binds the model pin digest and the engine
identity, so a receipt names exactly the model bytes and runtime that produced the ranking. The REFERENCE_CHECK
hook re-checks that binding on the output. Both adapters are PROBABILISTIC (floating-point inference is not
promised to replay bit-for-bit across machines) and unavailable while the engine or a verified model directory is
missing.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability.adapter import StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Distribution,
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
from pados.capability.verification import OutputValidator
from pados.capability_loop.contracts import CapabilityState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.rerank import RerankCandidate, RerankError, RerankMode, rerank
from pados.execution.context import canonical_hash
from pados.packs.algorithms.contracts import validated
from pados.packs.document_intelligence.boundary import closed
from pados.packs.document_intelligence.local_models import (
    MAX_TEXTS,
    CrossEncoderReranker,
    EngineIdentity,
    PinnedCrossEncoder,
    PinnedTextEmbedder,
)
from pados.packs.document_intelligence.model_pin import ModelPin
from pados.packs.document_intelligence.ranking import (
    MAX_RESULTS,
    SCORED_CHECKER_ID,
    SCORED_CONTRACT,
    SCORED_KIND,
    DocumentIntelligenceError,
    ScoredPassage,
    ScoredRanking,
    document_input,
    input_ranking,
    load_scored,
    query_identity,
    require_bound,
    scored_payload,
    unicode_version,
)
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, OCR_DOCUMENT_CONTRACT

PASSAGE_RETRIEVAL_DENSE = "PASSAGE_RETRIEVAL_DENSE"
PASSAGE_RERANK_CROSS_ENCODER = "PASSAGE_RERANK_CROSS_ENCODER"
DENSE_ADAPTER_ID = "document-intelligence.dense-fastembed"
RERANK_ADAPTER_ID = "document-intelligence.cross-encoder-fastembed"
DENSE_PACK_ID = "pados.document-intelligence.dense"
RERANK_PACK_ID = "pados.document-intelligence.rerank"
MAX_DENSE_PASSAGES = MAX_TEXTS - 1
CEILING = DataClassification.CONFIDENTIAL  # third-party runtime in-process: one level below the lexical stage
EVIDENCE_KEYS = (*CORE_EVIDENCE, "output_contract", "input_document_sha256", "passages_total", "results",
                 "model_pin_id", "model_pin_digest", "engine", "truncated_inputs", "rerank_outcome")
MODEL_FAILURES = ("DI_PARAMETERS_INVALID", "DI_INPUT_COUNT_INVALID", "DI_INPUT_NOT_DOCUMENT", "DI_DOCUMENT_INVALID",
                  "DI_INPUT_UNREADABLE", "DI_NO_PASSAGES", "DI_TOO_MANY_PASSAGES", "DI_QUERY_EMPTY",
                  "DI_QUERY_TOO_LONG", "DI_SEED_NOT_APPLICABLE", "DI_RANKING_NOT_MINIMIZED", "DI_INPUT_NOT_RANKING",
                  "DI_INPUT_RANKING_INVALID", "DI_RANKING_DOCUMENT_MISMATCH", "DI_RANKING_QUERY_MISMATCH",
                  "DI_RERANK_FAILED", "DI_UNEXPECTED_ERROR", "LOCAL_MODEL_INPUT_TOO_LARGE", "ENGINE_NOT_INSTALLED",
                  "ENGINE_POISONED", "MODEL_DIR_UNPINNED_FILE", "MODEL_WORKER_EXITED",
                  "MODEL_WORKER_MEMORY_LIMIT", "MODEL_WORKER_START_FAILED", "MODEL_WORKER_PROTOCOL_INVALID",
                  "MODEL_WORKER_OUTPUT_TOO_LARGE", "MODEL_WORKER_IDENTITY_REFUSED", "MODEL_WORKER_ENGINE_UNKNOWN",
                  "MODEL_PIN_ROLE_MISMATCH", "MODEL_DIR_MISSING", "MODEL_DIR_UNREADABLE", "MODEL_FILE_MISSING",
                  "MODEL_FILE_SYMLINK", "MODEL_FILE_INVALID", "MODEL_FILE_SIZE_MISMATCH",
                  "MODEL_FILE_DIGEST_MISMATCH", "LOCAL_MODEL_INPUT_INVALID", "LOCAL_MODEL_DIMENSION_MISMATCH",
                  "LOCAL_MODEL_OUTPUT_INVALID", "LOCAL_MODEL_FAILED", "LOCAL_MODEL_TIMEOUT")
_FROZEN = ConfigDict(frozen=True, extra="forbid")


class _Parameters(BaseModel):
    model_config = _FROZEN

    query: str = Field(min_length=1, max_length=256)
    top_k: int = Field(default=10, ge=1, le=MAX_RESULTS)

    @model_validator(mode="before")
    @classmethod
    def _no_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(isinstance(item, bool) for item in value.values()):
            raise ValueError("DI_PARAMETERS_INVALID")
        return value


class DenseParameters(_Parameters):
    pass


class RerankParameters(_Parameters):
    mode: Literal["SHADOW", "ACTIVE"] = "SHADOW"


def _capability(capability_id: str, operation: str, input_contract: str, description: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, version="1", family=CapabilityFamily.RETRIEVAL, operation=operation,
        input_contract=input_contract, output_contract=SCORED_CONTRACT, determinism=Determinism.PROBABILISTIC,
        modalities=(Modality.TEXT, Modality.EMBEDDING), max_classification=CEILING,
        network_requirement=NetworkRequirement.LOCAL_ONLY,
        verification=(VerificationMethod.SCHEMA, VerificationMethod.REFERENCE_CHECK),
        failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
        description=description)


DENSE_CAPABILITY = _capability(PASSAGE_RETRIEVAL_DENSE, "RANK_PASSAGES_DENSE", OCR_DOCUMENT_CONTRACT,
                               "Dense-embedding passage ranking with a pinned local model")
RERANK_CAPABILITY = _capability(PASSAGE_RERANK_CROSS_ENCODER, "RERANK_PASSAGES", SCORED_CONTRACT,
                                "Cross-encoder reranking (SHADOW or ACTIVE) with a pinned local model")


def _engine_label(identity: EngineIdentity | None) -> str:
    return identity.label() if identity is not None else "not-installed"


def _engine_version(identity: EngineIdentity | None) -> str:
    return identity.engine_version if identity is not None else "0.0.0"


def config_digest(pin: ModelPin, identity: EngineIdentity | None) -> str:
    """The execution configuration a model stage runs (and is measured) under: model bytes + engine + device."""
    return canonical_hash({"pin": pin.digest, "engine": _engine_label(identity),
                           "providers": list(identity.providers) if identity else [],
                           "threads": pin.threads, "max_batch": pin.max_batch,
                           "max_input_chars": pin.max_input_chars, "timeout_seconds": pin.timeout_seconds})


def _adapter(adapter_id: str, capability_id: str, pin: ModelPin, identity: EngineIdentity | None) -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=adapter_id, capability_id=capability_id, version="1", backend="fastembed-onnxruntime-cpu",
        determinism=Determinism.PROBABILISTIC, max_classification=CEILING,
        network_requirement=NetworkRequirement.LOCAL_ONLY,
        resources=ResourceRequirement(cpu_cores=pin.threads, ram_mb=pin.max_ram_mb),
        license=LicenseInfo(spdx="Apache-2.0", engine_version=_engine_version(identity),
                            distribution=Distribution.RUNTIME_OPTIONAL),
        lifecycle=LifecycleState.EXPERIMENTAL)


def _manifest(pack_id: str, capability: CapabilityDescriptor, adapter: AdapterDescriptor, pin: ModelPin,
              identity: EngineIdentity | None) -> CapabilityPackManifest:
    return CapabilityPackManifest(
        pack_id=pack_id, version="1",
        provider=PackProvider(provider_id=f"{pack_id}.fastembed", runtime_id="fastembed",
                              runtime_version=_engine_version(identity)),
        provenance=PackProvenance(source=PackSource.IN_TREE, license=LicenseInfo(spdx="AGPL-3.0-only"),
                                  publisher="pados"),
        capability_ids=(capability.capability_id,), capabilities=(capability,), adapters=(adapter,),
        runtime=PackRuntime(runtime_type=RuntimeType.IN_PROCESS, device=(adapter.resources,),
                            execution_config_digest=config_digest(pin, identity),
                            timeout_seconds=max(1, int(pin.timeout_seconds) * 2),
                            network=NetworkRequirement.LOCAL_ONLY),
        security=PackSecurity(classification_ceiling=CEILING, egress=Egress.NONE),
        evidence_requirements=EVIDENCE_KEYS,
        verification_hooks=(CheckerSpec(output_contract=SCORED_CONTRACT, checker_id=SCORED_CHECKER_ID),),
        initial_state=CapabilityState.EXPERIMENTAL,
        failure_codes=tuple(FailureCodeSpec(code=code, retryable=code == "LOCAL_MODEL_TIMEOUT")
                            for code in MODEL_FAILURES))


def dense_manifest(embedder: PinnedTextEmbedder) -> CapabilityPackManifest:
    adapter = _adapter(DENSE_ADAPTER_ID, PASSAGE_RETRIEVAL_DENSE, embedder.pin, embedder.identity)
    return _manifest(DENSE_PACK_ID, DENSE_CAPABILITY, adapter, embedder.pin, embedder.identity)


def rerank_manifest(encoder: PinnedCrossEncoder) -> CapabilityPackManifest:
    adapter = _adapter(RERANK_ADAPTER_ID, PASSAGE_RERANK_CROSS_ENCODER, encoder.pin, encoder.identity)
    return _manifest(RERANK_PACK_ID, RERANK_CAPABILITY, adapter, encoder.pin, encoder.identity)


def _output(manifest: CapabilityPackManifest, context: StageContext, ranking: ScoredRanking, document_sha256: str,
            outcome: str, metrics: dict[str, float]) -> StageOutput:
    ref = context.artifacts.put(scored_payload(ranking), media_type=JSON_MEDIA_TYPE, kind=SCORED_KIND)
    evidence: dict[str, str | int | float | bool | None] = {
        "runtime_version": manifest.provider.runtime_version, "device": "cpu",
        "config_digest": manifest.runtime.execution_config_digest, "provider_id": manifest.provider.provider_id,
        "artifact_digest": outputs_digest((ref,)), "output_contract": SCORED_CONTRACT,
        "input_document_sha256": document_sha256, "passages_total": ranking.passages_total,
        "results": len(ranking.results), "model_pin_id": ranking.model_pin_id,
        "model_pin_digest": ranking.model_pin_digest, "engine": ranking.engine,
        "truncated_inputs": ranking.truncated_inputs, "rerank_outcome": outcome}
    return StageOutput(outputs=(ref,), metrics=metrics, evidence=evidence)


class DenseRetrievalAdapter:
    """PASSAGE_RETRIEVAL_DENSE (registered only through ``register_pack``; reached only via the executor)."""

    def __init__(self, embedder: PinnedTextEmbedder) -> None:
        self._embedder = embedder
        self.manifest = dense_manifest(embedder)
        self._descriptor = self.manifest.adapters[0]

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._embedder.status() is None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        return closed(self._execute, request, context)

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.seed is not None:
            raise DocumentIntelligenceError("DI_SEED_NOT_APPLICABLE")
        if len(request.inputs) != 1:
            raise DocumentIntelligenceError("DI_INPUT_COUNT_INVALID")
        parameters = validated(DenseParameters, dict(request.parameters), "DI_PARAMETERS_INVALID")
        ref, document, units = document_input(request, context)
        if len(units) > MAX_DENSE_PASSAGES:
            raise DocumentIntelligenceError("DI_TOO_MANY_PASSAGES")
        query_sha256, terms = query_identity(parameters.query)
        embedded = self._embedder.embed([parameters.query, *(unit.text for unit in units)])
        query, *vectors = embedded.vectors
        scores = [round(sum(a * b for a, b in zip(query, vector, strict=True)), 6) for vector in vectors]
        order = sorted(range(len(units)), key=lambda index: (-scores[index], index))[:parameters.top_k]
        pin = self._embedder.pin
        ranking = ScoredRanking(
            method="DENSE_COSINE", source_document_sha256=ref.sha256, source_sha256=document.source.sha256,
            query_sha256=query_sha256, query_terms=terms, unicode_version=unicode_version(),
            passages_total=len(units), top_k=parameters.top_k, model_pin_id=pin.pin_id,
            model_pin_digest=pin.digest, engine=_engine_label(self._embedder.identity),
            truncated_inputs=embedded.truncated,
            results=tuple(ScoredPassage(rank=position, passage_id=units[index].passage_id,
                                        section_id=units[index].section_id, page=units[index].page,
                                        char_start=units[index].char_start, char_end=units[index].char_end,
                                        score=scores[index], model_score=scores[index])
                          for position, index in enumerate(order, start=1)))
        return _output(self.manifest, context, ranking, ref.sha256, "NOT_APPLICABLE",
                       {"passages": float(len(units)), "results": float(len(order))})


class CrossEncoderRerankAdapter:
    """PASSAGE_RERANK_CROSS_ENCODER over the existing enterprise ``rerank`` (SHADOW / ACTIVE)."""

    def __init__(self, encoder: PinnedCrossEncoder) -> None:
        self._encoder = encoder
        self._reranker = CrossEncoderReranker(encoder)
        self.manifest = rerank_manifest(encoder)
        self._descriptor = self.manifest.adapters[0]

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._encoder.status() is None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        return closed(self._execute, request, context)

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.seed is not None:
            raise DocumentIntelligenceError("DI_SEED_NOT_APPLICABLE")
        if len(request.inputs) != 2:
            raise DocumentIntelligenceError("DI_INPUT_COUNT_INVALID")
        parameters = validated(RerankParameters, dict(request.parameters), "DI_PARAMETERS_INVALID")
        ref, document, units = document_input(request, context)
        (prior_ref,) = [item for item in request.inputs if item is not ref]
        prior = input_ranking(prior_ref, context.artifacts.get(prior_ref))
        query_sha256, terms = query_identity(parameters.query)
        require_bound(prior, ref.sha256, query_sha256)
        by_id = {unit.passage_id: unit for unit in units}
        if any(item.passage_id not in by_id for item in prior.results) or not prior.results:
            raise DocumentIntelligenceError("DI_INPUT_RANKING_INVALID")
        candidates = [RerankCandidate(item.passage_id, by_id[item.passage_id].text, item.score)
                      for item in prior.results]
        mode = RerankMode(parameters.mode)
        try:
            ordered, report = rerank(self._reranker, mode, parameters.query, candidates)
        except RerankError:
            ordered = []  # ACTIVE only: the reranker failed; never fall back to the prior order
        if not ordered:
            raise DocumentIntelligenceError("DI_RERANK_FAILED")
        prior_rank = {item.passage_id: item.rank for item in prior.results}
        prior_score = {item.passage_id: item.score for item in prior.results}
        if mode is RerankMode.ACTIVE:
            # Equal model scores keep the prior order (never the passage-id spelling).
            ordered = sorted(ordered, key=lambda pair: (-(pair[1] or 0.0), prior_rank[pair[0]]))
            kept = ordered[:parameters.top_k]
            results = self._results(kept, by_id, prior_rank, lambda pid, model: float(model or 0.0))
            shadow: tuple[str, ...] = ()
            outcome: Literal["APPLIED", "SHADOW_SCORED", "SHADOW_FAILED"] = "APPLIED"
        else:
            kept = ordered[:parameters.top_k]
            results = self._results(kept, by_id, prior_rank, lambda pid, _model: prior_score[pid])
            failed = "error" in report
            if failed:
                shadow = tuple(pid for pid, _ in kept)
            else:  # the reranker's own top-k over ALL candidates, ties in prior order (as ACTIVE would publish)
                scores = dict(ordered)
                shadow = tuple(sorted(scores, key=lambda pid: (-(scores[pid] or 0.0), prior_rank[pid]))
                               [:parameters.top_k])
            outcome = "SHADOW_FAILED" if failed else "SHADOW_SCORED"
        pin = self._encoder.pin
        ranking = ScoredRanking(
            method="CROSS_ENCODER", source_document_sha256=ref.sha256, source_sha256=document.source.sha256,
            query_sha256=query_sha256, query_terms=terms, unicode_version=unicode_version(),
            passages_total=len(units), top_k=parameters.top_k, input_rankings=(prior_ref.sha256,),
            model_pin_id=pin.pin_id, model_pin_digest=pin.digest, engine=_engine_label(self._encoder.identity),
            rerank_mode=parameters.mode, rerank_outcome=outcome, results=results, shadow_order=shadow)
        return _output(self.manifest, context, ranking, ref.sha256, outcome,
                       {"candidates": float(len(candidates)), "results": float(len(results)),
                        "latency_ms": float(report.get("latency_ms", 0.0))})

    @staticmethod
    def _results(kept: Sequence[tuple[str, float | None]], by_id: dict[str, Any], prior_rank: dict[str, int],
                 score_of: Callable[[str, float | None], float]) -> tuple[ScoredPassage, ...]:
        return tuple(ScoredPassage(rank=position, passage_id=pid, section_id=by_id[pid].section_id,
                                   page=by_id[pid].page, char_start=by_id[pid].char_start,
                                   char_end=by_id[pid].char_end, score=score_of(pid, model),
                                   prior_rank=prior_rank[pid], model_score=model)
                     for position, (pid, model) in enumerate(kept, start=1))


def scored_reference_check(*engines: PinnedTextEmbedder | PinnedCrossEncoder) -> OutputValidator:
    """REFERENCE_CHECK for ``retrieval.scored-ranking/v1`` (one hook per contract, shared by every stage that emits
    it): a model ranking must name a model pin and engine the operator registered, byte-identical digest included;
    a fusion ranking must name none."""
    trusted = {engine.pin.pin_id: (engine.pin.digest, _engine_label(engine.identity)) for engine in engines}

    def check(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
        if len(outputs) != 1 or outputs[0][0].kind != SCORED_KIND:
            return False
        try:
            ranking = load_scored(outputs[0][1])
        except DocumentIntelligenceError:
            return False
        if ranking.method == "RRF_FUSION":
            return ranking.model_pin_id is None
        expected = trusted.get(ranking.model_pin_id or "")
        return expected is not None and expected == (ranking.model_pin_digest, ranking.engine)
    return check
