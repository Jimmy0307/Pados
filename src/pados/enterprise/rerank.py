# SPDX-License-Identifier: AGPL-3.0-only
"""Reranker capability adapters.

PADOS is not bound to one model. Every reranker reports its model/version,
input count, output ranking, latency, resource class and provenance.
Modes follow the validated donor contract: DISABLED (not called), SHADOW
(scored and reported, order unchanged) and ACTIVE (order applied; a failure
fails closed instead of silently falling back).
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pados.enterprise import observability
from pados.enterprise.compute import ResourceClass
from pados.enterprise.manifest import CapabilityHealth
from pados.enterprise.secrets import SecretValue, redact


class RerankMode(StrEnum):
    DISABLED = "DISABLED"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: str
    text: str
    prior_score: float


class RerankError(RuntimeError):
    pass


class Reranker(Protocol):
    reranker_id: str
    model: str
    version: str
    resource_class: ResourceClass

    def health(self) -> CapabilityHealth: ...

    def score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]: ...


class NoOpReranker:
    """Deterministic test reranker: keeps the prior order and scores."""

    reranker_id = "pados.rerank.noop"
    model = "none"
    version = "1"
    resource_class = ResourceClass.NONE

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY

    def score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]:
        return [candidate.prior_score for candidate in candidates]


_WORD = re.compile(r"[\w-]+", re.UNICODE)


class DeterministicLexicalReranker:
    """Local rule reranker (generalized from the donor deterministic rule reranker).

    Score = query-term coverage, with a bonus for the exact normalized phrase
    and a small tie-break from the prior retrieval score. No model, no network.
    """

    reranker_id = "pados.rerank.lexical"
    model = "deterministic-lexical-coverage"
    version = "1"
    resource_class = ResourceClass.CPU_LIGHT

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY

    def score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]:
        terms = {word.casefold() for word in _WORD.findall(query)}
        phrase = " ".join(word.casefold() for word in _WORD.findall(query))
        scores = []
        for candidate in candidates:
            words = [word.casefold() for word in _WORD.findall(candidate.text)]
            coverage = len(terms & set(words)) / len(terms) if terms else 0.0
            exact = 0.25 if phrase and phrase in " ".join(words) else 0.0
            scores.append(round(coverage + exact + 0.01 * _squash(candidate.prior_score), 6))
        return scores


def _squash(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


Transport = Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]]


class HttpReranker:
    """Generic HTTP reranker (``{query, documents}`` -> ``{results:[{index, relevance_score}]}``).

    The transport is injected; the credential is a resolved SecretRef handed in
    only for the call and redacted from any error. Responses that do not match
    the schema fail closed with ``RERANK_SCHEMA_INVALID``.
    """

    resource_class = ResourceClass.NONE

    def __init__(self, *, endpoint: str, model: str, version: str, transport: Transport | None,
                 credential: SecretValue | None = None) -> None:
        self.reranker_id = "pados.rerank.http"
        self.endpoint = endpoint
        self.model = model
        self.version = version
        self._transport = transport
        self._credential = credential

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY if self._transport is not None else CapabilityHealth.NOT_CONFIGURED

    def score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]:
        if self._transport is None:
            raise RerankError("RERANK_TRANSPORT_NOT_CONFIGURED")
        body = json.dumps({"model": self.model, "query": query,
                           "documents": [candidate.text for candidate in candidates]}).encode("utf-8")
        headers = {"content-type": "application/json"}
        secrets = {}
        if self._credential is not None:
            headers["authorization"] = f"Bearer {self._credential.reveal()}"
            secrets["credential"] = self._credential
        try:
            status, payload = self._transport(self.endpoint, body, headers)
        except Exception as exc:  # noqa: BLE001 - transport errors are normalized and redacted
            raise RerankError(redact(f"RERANK_TRANSPORT_FAILED:{type(exc).__name__}:{exc}", secrets)) from None
        if status != 200:
            raise RerankError(f"RERANK_HTTP_{status}")
        return _parse_scores(payload, len(candidates))


def _parse_scores(payload: bytes, count: int) -> list[float]:
    try:
        results = json.loads(payload)["results"]
        scores: list[float | None] = [None] * count
        for entry in results:
            index, score = int(entry["index"]), float(entry["relevance_score"])
            if not 0 <= index < count or scores[index] is not None or not math.isfinite(score):
                raise ValueError
            scores[index] = score
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise RerankError("RERANK_SCHEMA_INVALID") from None
    if any(score is None for score in scores):
        raise RerankError("RERANK_SCHEMA_INVALID")
    return [float(score) for score in scores if score is not None]


def rerank(
    reranker: Reranker, mode: RerankMode, query: str, candidates: Sequence[RerankCandidate],
) -> tuple[list[tuple[str, float | None]], dict[str, Any]]:
    """Return ``[(chunk_id, rerank_score)]`` in final order plus the report."""
    report: dict[str, Any] = {
        "reranker_id": reranker.reranker_id, "model": reranker.model, "version": reranker.version,
        "mode": mode.value, "input_count": len(candidates), "resource_class": reranker.resource_class.value,
        "latency_ms": 0.0, "output_ranking": [c.chunk_id for c in candidates], "applied": False,
        "provenance": {"scored_chunk_ids": [], "prior_order": [c.chunk_id for c in candidates]},
    }
    if mode is RerankMode.DISABLED or not candidates:
        return [(c.chunk_id, None) for c in candidates], report
    with observability.timed() as elapsed:
        try:
            scores = reranker.score(query, candidates)
            if len(scores) != len(candidates):
                raise RerankError("RERANK_SCHEMA_INVALID")
        except RerankError as exc:
            observability.emit("rerank.completed", reranker_id=reranker.reranker_id, mode=mode.value,
                               outcome="FAILED", reason=str(exc)[:128], input_count=len(candidates))
            if mode is RerankMode.ACTIVE:
                raise
            report["error"] = str(exc)
            return [(c.chunk_id, None) for c in candidates], report
        latency = elapsed()
    ordered = sorted(zip(candidates, scores, strict=True), key=lambda pair: (-pair[1], pair[0].chunk_id))
    report.update(latency_ms=latency, provenance={"scored_chunk_ids": [c.chunk_id for c in candidates],
                                                  "prior_order": [c.chunk_id for c in candidates]})
    observability.emit("rerank.completed", reranker_id=reranker.reranker_id, mode=mode.value,
                       outcome="OK", latency_ms=latency, input_count=len(candidates))
    if mode is RerankMode.SHADOW:
        report["shadow_ranking"] = [c.chunk_id for c, _ in ordered]
        by_id = dict(zip((c.chunk_id for c in candidates), scores, strict=True))
        return [(c.chunk_id, by_id[c.chunk_id]) for c in candidates], report
    report.update(output_ranking=[c.chunk_id for c, _ in ordered], applied=True)
    return [(c.chunk_id, score) for c, score in ordered], report
