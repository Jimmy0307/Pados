# SPDX-License-Identifier: AGPL-3.0-only
"""PASSAGE_RETRIEVAL: deterministic lexical BM25 over the reading-order passages of an OCR document.

Passages are the top-level reading-order blocks of each page (headings and paragraphs after layout and
structure; the raw lines when no layout ran), in document order. Offsets index the canonical text view
``pados.retrieval.reading-order-view.v1``: passage texts joined by a blank line ("\\n\\n"), in code points.

Tokenizer ``pados.lexical-tokenizer.v1``: Unicode NFKC, ``casefold()``, then maximal runs of letters and
digits (``[^\\W_]+``). The Unicode database version is recorded with every ranking.

Scoring is Okapi BM25 (Robertson and Zaragoza, "The Probabilistic Relevance Framework: BM25 and Beyond",
2009) with the non-negative idf ``ln(1 + (N - n + 0.5) / (n + 0.5))``, summed over the distinct query
terms. It is computed with the standard-library ``decimal`` module in a fixed context (34 digits,
ROUND_HALF_EVEN; ``ln`` is correctly rounded by specification), so no platform ``libm`` result decides a
score, and scores are quantized to 6 decimals. Ties keep document order. Parameters (``query``, ``top_k``,
``k1``, ``b``) come from the stage parameters only.

The ranking artifact names passages by block id, section id, page and offsets, with scores and matched
term counts; it never contains document or query text (only a hash of the query terms). Errors are closed
``RETRIEVAL_*`` codes raised outside the handler. Output is a candidate (``semantic_authority = NONE``).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactIntegrityError, ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    Modality,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.packs.algorithms.contracts import AlgorithmError, canonical_json, parse_json_document, validated
from pados.packs.ocr.contracts import (
    JSON_MEDIA_TYPE,
    KIND_DOCUMENT,
    OCR_DOCUMENT_CONTRACT,
    BlockKind,
    OcrContractError,
    OcrDocument,
)

PASSAGE_RETRIEVAL = "PASSAGE_RETRIEVAL"
RANKING_CONTRACT = "retrieval.ranking/v1"
RANKING_SCHEMA: Final = "pados.retrieval.ranking.v1"
RANKING_KIND = "retrieval.ranking"
LEXICAL_ADAPTER_ID = "retrieval.lexical-bm25"
TOKENIZER_ID: Final = "pados.lexical-tokenizer.v1"
TEXT_VIEW: Final = "pados.retrieval.reading-order-view.v1"
SEPARATOR = "\n\n"
SCORER: Final = "bm25"
MAX_PASSAGES = 20_000
MAX_QUERY_TERMS = 64
MAX_TOP_K = 64
MAX_RANKING_BYTES = 1024 * 1024
RANKING_KEYS = frozenset({"schema", "semantic_authority", "source_document_sha256", "source_sha256", "query_sha256",
                          "query_terms", "tokenizer", "unicode_version", "text_view", "scorer", "k1", "b", "top_k",
                          "passages_total", "results"})
_SCORE_QUANTUM = Decimal("0.000001")
_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_TOKEN = re.compile(r"[^\W_]+")
_SHA256 = r"^[0-9a-f]{64}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid")

CAPABILITY = CapabilityDescriptor(
    capability_id=PASSAGE_RETRIEVAL, version="1", family=CapabilityFamily.RETRIEVAL, operation="RANK_PASSAGES",
    input_contract=OCR_DOCUMENT_CONTRACT, output_contract=RANKING_CONTRACT, determinism=Determinism.DETERMINISTIC,
    modalities=(Modality.TEXT,), max_classification=DataClassification.RESTRICTED,
    network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
    failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE,
    description="Lexical ranking of document passages for a query (fixed tokenizer)")


class RetrievalError(ValueError):
    """Retrieval failure with a closed code (never echoes document or query text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def lexical_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=LEXICAL_ADAPTER_ID, capability_id=PASSAGE_RETRIEVAL, version="1", backend="python",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo(spdx="AGPL-3.0-only"))


class RetrievalParameters(BaseModel):
    model_config = _FROZEN

    query: str = Field(min_length=1, max_length=256)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    k1: float = Field(default=1.2, ge=0.0, le=3.0)
    b: float = Field(default=0.75, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def no_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(isinstance(item, bool) for item in value.values()):
            raise ValueError("RETRIEVAL_PARAMETERS_INVALID")
        return value


def tokenize(text: str) -> list[str]:
    """``pados.lexical-tokenizer.v1``: NFKC, casefold, maximal runs of letters and digits."""
    return _TOKEN.findall(unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Passage:
    passage_id: str
    section_id: str | None
    page: int
    text: str
    char_start: int
    char_end: int


def passages(document: OcrDocument) -> list[Passage]:
    """Top-level reading-order units of every page, with offsets in the canonical text view."""
    section_of = {block_id: section.section_id for section in document.sections for block_id in section.block_ids}
    items: list[Passage] = []
    offset = 0
    for page in document.pages:
        blocks = {block.block_id: block for block in page.blocks}
        order = page.reading_order or tuple(b.block_id for b in page.blocks if b.kind is BlockKind.LINE)
        for block_id in order:
            if len(items) >= MAX_PASSAGES:
                raise RetrievalError("RETRIEVAL_TOO_MANY_PASSAGES")
            text = blocks[block_id].text
            items.append(Passage(block_id, section_of.get(block_id), page.page_number, text, offset,
                                 offset + len(text)))
            offset += len(text) + len(SEPARATOR)
    return items


def _decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def bm25_scores(query_terms: Sequence[str], documents: Sequence[Sequence[str]], *, k1: float,
                b: float) -> list[tuple[Decimal, int]]:
    """(quantized score, distinct matched query terms) per document; exact decimal arithmetic."""
    terms = sorted(set(query_terms))
    counts = [Counter(tokens) for tokens in documents]
    with localcontext(_CONTEXT):
        total = Decimal(len(documents))
        average = Decimal(sum(len(tokens) for tokens in documents)) / total if documents else Decimal(0)
        k1_d, b_d = _decimal(k1), _decimal(b)
        idf: dict[str, Decimal] = {}
        for term in terms:
            frequency = sum(1 for count in counts if term in count)
            if frequency:
                idf[term] = (1 + (total - frequency + Decimal("0.5")) / (frequency + Decimal("0.5"))).ln()
        scored: list[tuple[Decimal, int]] = []
        for tokens, count in zip(documents, counts, strict=True):
            score, matched = Decimal(0), 0
            norm = 1 - b_d + (b_d * Decimal(len(tokens)) / average if average else Decimal(0))
            for term, weight in idf.items():
                tf = count.get(term, 0)
                if tf:
                    matched += 1
                    score += weight * Decimal(tf) * (k1_d + 1) / (Decimal(tf) + k1_d * norm)
            scored.append((score.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN), matched))
    return scored


class RankedPassage(BaseModel):
    model_config = _FROZEN

    rank: int = Field(ge=1, le=MAX_TOP_K)
    passage_id: str = Field(pattern=r"^[a-z][a-z0-9.-]{0,63}$")
    section_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9.-]{0,63}$")
    page: int = Field(ge=1, le=100_000)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    score: float = Field(gt=0.0)
    matched_terms: int = Field(ge=1, le=MAX_QUERY_TERMS)


class RankingDocument(BaseModel):
    """``retrieval.ranking/v1``: passage references and scores (no text)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_version: Literal["pados.retrieval.ranking.v1"] = Field(default=RANKING_SCHEMA, alias="schema")
    semantic_authority: Literal["NONE"] = "NONE"
    source_document_sha256: str = Field(pattern=_SHA256)
    source_sha256: str = Field(pattern=_SHA256)
    query_sha256: str = Field(pattern=_SHA256)
    query_terms: int = Field(ge=1, le=MAX_QUERY_TERMS)
    tokenizer: Literal["pados.lexical-tokenizer.v1"] = TOKENIZER_ID
    unicode_version: str = Field(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
    text_view: Literal["pados.retrieval.reading-order-view.v1"] = TEXT_VIEW
    scorer: Literal["bm25"] = SCORER
    k1: float
    b: float
    top_k: int = Field(ge=1, le=MAX_TOP_K)
    passages_total: int = Field(ge=1, le=MAX_PASSAGES)
    results: tuple[RankedPassage, ...] = Field(max_length=MAX_TOP_K)

    @model_validator(mode="after")
    def ordered(self) -> RankingDocument:
        if len(self.results) > self.top_k or len(self.results) > self.passages_total:
            raise ValueError("RANKING_RESULTS_INVALID")
        if [item.rank for item in self.results] != list(range(1, len(self.results) + 1)):
            raise ValueError("RANKING_RESULTS_INVALID")
        if any(a.score < b.score for a, b in zip(self.results, self.results[1:], strict=False)):
            raise ValueError("RANKING_RESULTS_INVALID")
        if any(item.char_end < item.char_start for item in self.results):
            raise ValueError("RANKING_RESULTS_INVALID")
        if len({item.passage_id for item in self.results}) != len(self.results):
            raise ValueError("RANKING_RESULTS_INVALID")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def rank(document: OcrDocument, document_sha256: str, parameters: RetrievalParameters) -> RankingDocument:
    query_terms = tokenize(parameters.query)
    if not query_terms:
        raise RetrievalError("RETRIEVAL_QUERY_EMPTY")
    if len(query_terms) > MAX_QUERY_TERMS:
        raise RetrievalError("RETRIEVAL_QUERY_TOO_LONG")
    units = passages(document)
    if not units:
        raise RetrievalError("RETRIEVAL_NO_PASSAGES")
    scores = bm25_scores(query_terms, [tokenize(unit.text) for unit in units], k1=parameters.k1, b=parameters.b)
    order = sorted((index for index, (score, _) in enumerate(scores) if score > 0),
                   key=lambda index: (-scores[index][0], index))[:parameters.top_k]
    results = tuple(RankedPassage(rank=position, passage_id=units[index].passage_id,
                                  section_id=units[index].section_id, page=units[index].page,
                                  char_start=units[index].char_start, char_end=units[index].char_end,
                                  score=float(scores[index][0]), matched_terms=scores[index][1])
                    for position, index in enumerate(order, start=1))
    return RankingDocument(source_document_sha256=document_sha256, source_sha256=document.source.sha256,
                           query_sha256=canonical_hash({"tokenizer": TOKENIZER_ID, "terms": query_terms}),
                           query_terms=len(query_terms), unicode_version=unicodedata.unidata_version,
                           k1=parameters.k1, b=parameters.b, top_k=parameters.top_k, passages_total=len(units),
                           results=results)


class LexicalRetrievalAdapter:
    """Deterministic BM25 PASSAGE_RETRIEVAL adapter (invoked only through ``run_bound_stage``)."""

    def __init__(self) -> None:
        self._descriptor = lexical_descriptor()

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except ArtifactAccessDenied:
            raise
        except RetrievalError as exc:
            code = exc.code
        except AlgorithmError:
            code = "RETRIEVAL_PARAMETERS_INVALID"
        except OcrContractError:
            code = "RETRIEVAL_DOCUMENT_INVALID"
        except ArtifactIntegrityError:
            code = "RETRIEVAL_INPUT_UNREADABLE"
        except Exception:  # noqa: BLE001 - closed-error boundary
            code = "RETRIEVAL_UNEXPECTED_ERROR"
        raise CapabilityAdapterError(code) from None  # outside the handler: empty exception chain

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.capability_id != PASSAGE_RETRIEVAL:
            raise RetrievalError("RETRIEVAL_CAPABILITY_MISMATCH")
        if request.seed is not None:
            raise RetrievalError("RETRIEVAL_SEED_NOT_APPLICABLE")
        if len(request.inputs) != 1:
            raise RetrievalError("RETRIEVAL_INPUT_COUNT_INVALID")
        source = request.inputs[0]
        if source.kind != KIND_DOCUMENT or source.media_type != JSON_MEDIA_TYPE:
            raise RetrievalError("RETRIEVAL_INPUT_NOT_DOCUMENT")
        parameters = validated(RetrievalParameters, dict(request.parameters), "RETRIEVAL_PARAMETERS_INVALID")
        document = OcrDocument.from_bytes(context.artifacts.get(source))
        ranking = rank(document, source.sha256, parameters)
        record = ranking.canonical()
        try:
            assert_minimized(record, allowed_keys=RANKING_KEYS, max_string=256, max_items=MAX_TOP_K,
                             max_total_bytes=MAX_RANKING_BYTES)
        except DataMinimizationViolation:
            record = {}
        if not record:  # outside the handler: no minimization path in the exception chain
            raise RetrievalError("RETRIEVAL_RANKING_NOT_MINIMIZED")
        payload = canonical_json(record)
        if len(payload) > MAX_RANKING_BYTES:
            raise RetrievalError("RETRIEVAL_RANKING_TOO_LARGE")
        ref = context.artifacts.put(payload, media_type=JSON_MEDIA_TYPE, kind=RANKING_KIND)
        evidence: dict[str, str | int | float | bool | None] = {
            "output_contract": RANKING_CONTRACT, "input_document_sha256": source.sha256,
            "source_sha256": document.source.sha256, "ranking_sha256": ref.sha256,
            "passages_total": ranking.passages_total, "results": len(ranking.results),
            "query_terms": ranking.query_terms, "tokenizer": TOKENIZER_ID, "scorer": SCORER,
            "adapter_id": LEXICAL_ADAPTER_ID,
        }
        return StageOutput(outputs=(ref,), metrics={"passages": float(ranking.passages_total),
                                                    "results": float(len(ranking.results))}, evidence=evidence)


def load_ranking(payload: bytes) -> RankingDocument:
    """Strict, canonical load of a ranking artifact (closed ``RANKING_*`` codes)."""
    try:
        document = parse_json_document(payload, max_bytes=MAX_RANKING_BYTES, code="RANKING")
        ranking = validated(RankingDocument, document, "RANKING_SCHEMA_INVALID")
    except AlgorithmError as exc:
        code = exc.code
    else:
        if canonical_json(ranking.canonical()) != payload:
            raise RetrievalError("RANKING_NOT_CANONICAL")
        return ranking
    raise RetrievalError(code)


def ranking_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``retrieval.ranking/v1``: one canonical ranking artifact."""
    if len(outputs) != 1 or outputs[0][0].kind != RANKING_KIND or outputs[0][0].media_type != JSON_MEDIA_TYPE:
        return False
    try:
        load_ranking(outputs[0][1])
    except RetrievalError:
        return False
    return True
