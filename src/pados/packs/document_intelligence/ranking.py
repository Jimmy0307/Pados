# SPDX-License-Identifier: AGPL-3.0-only
"""``retrieval.scored-ranking/v1``: the passage ranking a dense, fusion or cross-encoder stage produces.

Like the lexical ``retrieval.ranking/v1`` it names passages by block id, section, page and offsets in the
canonical reading-order view and never carries document or query text (the query is identified by the SAME hash
the BM25 stage records, so every stage of one task can be cross-checked against one query). It also records how
the ranking was made: the method, the model pin and engine identity (model stages), the input rankings it was
built from (fusion and rerank), the rerank mode and, in SHADOW mode, the order the reranker would have produced
while the published order stays the prior one.

Shared helpers load the OCR document input, derive the passages exactly as BM25 does, and resolve the query hash.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability.adapter import StageContext, StageRequest
from pados.capability.artifacts import ArtifactRef
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.packs.algorithms.contracts import AlgorithmError, canonical_json, parse_json_document, validated
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, OcrDocument
from pados.packs.retrieval.lexical import (
    MAX_PASSAGES,
    MAX_QUERY_TERMS,
    RANKING_KIND,
    TEXT_VIEW,
    TOKENIZER_ID,
    Passage,
    RankingDocument,
    load_ranking,
    passages,
    tokenize,
)

SCORED_CONTRACT: Final = "retrieval.scored-ranking/v1"
SCORED_SCHEMA: Final = "pados.retrieval.scored-ranking.v1"
SCORED_KIND: Final = "retrieval.scored-ranking"
SCORED_CHECKER_ID: Final = "document-intelligence.scored-reference"
MAX_RESULTS = 64
MAX_INPUT_RANKINGS = 4
MAX_SCORED_BYTES = 1024 * 1024
_SHA256 = r"^[0-9a-f]{64}$"
_PASSAGE = r"^[a-z][a-z0-9.-]{0,63}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid")
COSINE_BOUND: Final = 1.000001
SCORED_KEYS = frozenset({"rerank_outcome", "schema", "semantic_authority", "method", "source_document_sha256", "source_sha256",
                         "query_sha256", "query_terms", "tokenizer", "unicode_version", "text_view",
                         "passages_total", "top_k", "input_rankings", "model_pin_id", "model_pin_digest", "engine",
                         "rerank_mode", "fusion_k", "truncated_inputs", "results", "shadow_order"})


class DocumentIntelligenceError(ValueError):
    """Closed-code failure (never echoes document or query text)."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ScoredPassage(BaseModel):
    model_config = _FROZEN

    rank: int = Field(ge=1, le=MAX_RESULTS)
    passage_id: str = Field(pattern=_PASSAGE)
    section_id: str | None = Field(default=None, pattern=_PASSAGE)
    page: int = Field(ge=1, le=100_000)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    score: float = Field(allow_inf_nan=False)
    prior_rank: int | None = Field(default=None, ge=1, le=MAX_PASSAGES)
    model_score: float | None = Field(default=None, allow_inf_nan=False)


class ScoredRanking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_version: Literal["pados.retrieval.scored-ranking.v1"] = Field(default=SCORED_SCHEMA, alias="schema")
    semantic_authority: Literal["NONE"] = "NONE"
    method: Literal["DENSE_COSINE", "RRF_FUSION", "CROSS_ENCODER"]
    source_document_sha256: str = Field(pattern=_SHA256)
    source_sha256: str = Field(pattern=_SHA256)
    query_sha256: str = Field(pattern=_SHA256)
    query_terms: int = Field(ge=1, le=MAX_QUERY_TERMS)
    tokenizer: Literal["pados.lexical-tokenizer.v1"] = TOKENIZER_ID
    unicode_version: str = Field(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
    text_view: Literal["pados.retrieval.reading-order-view.v1"] = TEXT_VIEW
    passages_total: int = Field(ge=1, le=MAX_PASSAGES)
    top_k: int = Field(ge=1, le=MAX_RESULTS)
    input_rankings: tuple[Annotated[str, Field(pattern=_SHA256)], ...] = Field(default=(),
                                                                         max_length=MAX_INPUT_RANKINGS)
    model_pin_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9.-]{2,63}$")
    model_pin_digest: str | None = Field(default=None, pattern=_SHA256)
    engine: str | None = Field(default=None, max_length=160)
    rerank_mode: Literal["SHADOW", "ACTIVE"] | None = None
    rerank_outcome: Literal["APPLIED", "SHADOW_SCORED", "SHADOW_FAILED"] | None = None
    fusion_k: int | None = Field(default=None, ge=1, le=1000)
    truncated_inputs: int = Field(default=0, ge=0, le=MAX_PASSAGES + 1)
    results: tuple[ScoredPassage, ...] = Field(max_length=MAX_RESULTS)
    shadow_order: tuple[Annotated[str, Field(pattern=_PASSAGE)], ...] = Field(default=(), max_length=MAX_RESULTS)

    @model_validator(mode="after")
    def _coherent(self) -> ScoredRanking:
        results = self.results
        if (len(results) > self.top_k or len(results) > self.passages_total
                or [item.rank for item in results] != list(range(1, len(results) + 1))
                or any(a.score < b.score for a, b in zip(results, results[1:], strict=False))
                or any(item.char_end < item.char_start for item in results)
                or len({item.passage_id for item in results}) != len(results)):
            raise ValueError("SCORED_RESULTS_INVALID")
        model = self.method in ("DENSE_COSINE", "CROSS_ENCODER")
        if model != (self.model_pin_id is not None and self.model_pin_digest is not None and bool(self.engine)):
            raise ValueError("SCORED_MODEL_IDENTITY_INVALID")
        if (self.method == "CROSS_ENCODER") != (self.rerank_mode is not None) or (
                self.rerank_mode is None) != (self.rerank_outcome is None) or (
                (self.rerank_mode == "ACTIVE") != (self.rerank_outcome == "APPLIED")):
            raise ValueError("SCORED_RERANK_MODE_INVALID")
        if self.method == "DENSE_COSINE" and any(
                abs(value) > COSINE_BOUND for item in results for value in (item.score, item.model_score or 0.0)):
            raise ValueError("SCORED_COSINE_OUT_OF_RANGE")
        if (self.method == "RRF_FUSION") != (self.fusion_k is not None):
            raise ValueError("SCORED_FUSION_INVALID")
        if (self.method == "DENSE_COSINE") == bool(self.input_rankings):
            raise ValueError("SCORED_INPUTS_INVALID")
        if self.rerank_mode == "SHADOW":
            # The reranker's own top-k over the same candidates (it may name passages the prior cut off).
            if len(self.shadow_order) != len(results) or len(set(self.shadow_order)) != len(self.shadow_order):
                raise ValueError("SCORED_SHADOW_INVALID")
        elif self.shadow_order:
            raise ValueError("SCORED_SHADOW_INVALID")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def query_identity(query: str) -> tuple[str, int]:
    """The query hash and term count exactly as the BM25 stage records them (``pados.lexical-tokenizer.v1``)."""
    terms = tokenize(query)
    if not terms:
        raise DocumentIntelligenceError("DI_QUERY_EMPTY")
    if len(terms) > MAX_QUERY_TERMS:
        raise DocumentIntelligenceError("DI_QUERY_TOO_LONG")
    return canonical_hash({"tokenizer": TOKENIZER_ID, "terms": terms}), len(terms)


def unicode_version() -> str:
    return unicodedata.unidata_version


def document_input(request: StageRequest, context: StageContext) -> tuple[ArtifactRef, OcrDocument, list[Passage]]:
    """The single ``ocr.document/v1`` input and its passages (BM25's reading-order units)."""
    documents = [ref for ref in request.inputs if ref.kind == KIND_DOCUMENT]
    if len(documents) != 1 or documents[0].media_type != JSON_MEDIA_TYPE:
        raise DocumentIntelligenceError("DI_INPUT_NOT_DOCUMENT")
    ref = documents[0]
    document = OcrDocument.from_bytes(context.artifacts.get(ref))
    units = passages(document)
    if not units:
        raise DocumentIntelligenceError("DI_NO_PASSAGES")
    return ref, document, units


def scored_payload(ranking: ScoredRanking) -> bytes:
    record = ranking.canonical()
    try:
        assert_minimized(record, allowed_keys=SCORED_KEYS | {"rank", "passage_id", "section_id", "page",
                                                             "char_start", "char_end", "score", "prior_rank",
                                                             "model_score"},
                         max_string=256, max_items=MAX_RESULTS, max_total_bytes=MAX_SCORED_BYTES)
    except DataMinimizationViolation:
        record = {}
    if not record:  # outside the handler: no minimization detail in the exception chain
        raise DocumentIntelligenceError("DI_RANKING_NOT_MINIMIZED")
    return canonical_json(record)


def load_scored(payload: bytes) -> ScoredRanking:
    """Strict, canonical load of a scored ranking (closed ``SCORED_*`` codes)."""
    try:
        document = parse_json_document(payload, max_bytes=MAX_SCORED_BYTES, code="SCORED")
        ranking = validated(ScoredRanking, document, "SCORED_SCHEMA_INVALID")
    except AlgorithmError as exc:
        code = exc.code
    else:
        if canonical_json(ranking.canonical()) != payload:
            raise DocumentIntelligenceError("SCORED_NOT_CANONICAL")
        return ranking
    raise DocumentIntelligenceError(code)


def scored_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``retrieval.scored-ranking/v1``: one canonical scored-ranking artifact."""
    if len(outputs) != 1 or outputs[0][0].kind != SCORED_KIND or outputs[0][0].media_type != JSON_MEDIA_TYPE:
        return False
    try:
        load_scored(outputs[0][1])
    except DocumentIntelligenceError:
        return False
    return True


def input_ranking(ref: ArtifactRef, payload: bytes) -> RankingDocument | ScoredRanking:
    """A ranking input of either contract (anything else is refused)."""
    if ref.media_type != JSON_MEDIA_TYPE or ref.kind not in (RANKING_KIND, SCORED_KIND):
        raise DocumentIntelligenceError("DI_INPUT_NOT_RANKING")
    try:
        return load_ranking(payload) if ref.kind == RANKING_KIND else load_scored(payload)
    except ValueError:  # RetrievalError and DocumentIntelligenceError: closed codes, replaced below
        pass
    raise DocumentIntelligenceError("DI_INPUT_RANKING_INVALID")


def require_bound(ranking: RankingDocument | ScoredRanking, document_sha256: str, query_sha256: str) -> None:
    """A ranking is usable only for the very document and query of this stage (stale/foreign refused)."""
    if ranking.source_document_sha256 != document_sha256:
        raise DocumentIntelligenceError("DI_RANKING_DOCUMENT_MISMATCH")
    if ranking.query_sha256 != query_sha256:
        raise DocumentIntelligenceError("DI_RANKING_QUERY_MISMATCH")
