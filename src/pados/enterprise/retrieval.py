# SPDX-License-Identifier: AGPL-3.0-only
"""Unified enterprise retrieval contract and composable backends.

A response is evidence. It carries ``semantic_authority = NONE`` and is never
promoted to PADOS truth by this module; it can only enter a governed Work
Contract's bounded context.

Backends are interfaces. V1 ships in-process exact/term and deterministic
vector backends over the PADOS deterministic embedding; PostgreSQL FTS /
pgvector, Elasticsearch, Milvus or a graph store can implement the same
protocol later without changing the pipeline.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pados.corpus.ingestion import cosine_similarity, deterministic_embedding, normalize_text
from pados.enterprise.authorization import RetrievalScope
from pados.enterprise.budget import EvidenceBudget
from pados.enterprise.classification import DataClassification
from pados.enterprise.lineage import EnterpriseSource

RETRIEVAL_CONTRACT_VERSION = "pados.enterprise.retrieval.v1"


class RetrievalStrategy(StrEnum):
    EXACT = "EXACT"
    VECTOR = "VECTOR"
    HYBRID = "HYBRID"


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    normalized_id: str
    source: EnterpriseSource
    page_number: int
    char_start: int
    char_end: int
    text: str
    token_estimate: int
    chunking_version: str
    embedding: tuple[float, ...] = ()
    embedding_version: str | None = None
    extractor_id: str = ""
    extractor_version: str = ""

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def classification(self) -> DataClassification:
        return self.source.classification


@dataclass(frozen=True)
class RetrievalRequest:
    project_id: str
    tenant_id: str
    principal_id: str
    session_id: str
    query: str
    classification_ceiling: DataClassification
    top_k: int = 5
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID
    budget: EvidenceBudget = field(default_factory=EvidenceBudget)
    source_ids: frozenset[str] = frozenset()
    source_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.query.strip() or len(self.query) > 2_000:
            raise ValueError("RETRIEVAL_QUERY_INVALID")
        if not 1 <= self.top_k <= 50:
            raise ValueError("RETRIEVAL_TOP_K_OUT_OF_BOUNDS")

    @property
    def fingerprint(self) -> str:
        payload = {
            "project_id": self.project_id, "tenant_id": self.tenant_id, "principal_id": self.principal_id,
            "query_sha256": hashlib.sha256(self.query.encode("utf-8")).hexdigest(),
            "ceiling": self.classification_ceiling.value, "top_k": self.top_k,
            "strategy": self.strategy.value, "budget": self.budget.as_dict(),
            "source_ids": sorted(self.source_ids), "source_types": sorted(self.source_types),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceItem:
    chunk_id: str
    source: EnterpriseSource
    text: str
    page_number: int
    char_start: int
    char_end: int
    token_estimate: int
    retrieval_scores: dict[str, float]
    rerank_score: float | None
    provenance: dict[str, Any]

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def classification(self) -> DataClassification:
        return self.source.classification

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "chunk_id": self.chunk_id, "source_id": self.source_id,
            "classification": self.classification.value,
            "span": {"page": self.page_number, "char_start": self.char_start, "char_end": self.char_end},
            "text_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            "token_estimate": self.token_estimate,
            "retrieval_scores": self.retrieval_scores, "rerank_score": self.rerank_score,
            "provenance": self.provenance,
        }
        if include_text:
            value["text"] = self.text
        return value


@dataclass(frozen=True)
class RetrievalResponse:
    request_fingerprint: str
    evidence: tuple[EvidenceItem, ...]
    authorization: dict[str, Any]
    backends: tuple[dict[str, str], ...]
    reranker: dict[str, Any]
    budget: dict[str, Any]
    versions: dict[str, str]
    semantic_authority: str = "NONE"
    promotable_to_truth: bool = False

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        body = {
            "contract": RETRIEVAL_CONTRACT_VERSION,
            "request_fingerprint": self.request_fingerprint,
            "evidence": [item.to_dict(include_text=include_text) for item in self.evidence],
            "source_identities": sorted({item.source_id for item in self.evidence}),
            "authorization": self.authorization, "backends": list(self.backends),
            "reranker": self.reranker, "budget": self.budget, "versions": self.versions,
            "semantic_authority": self.semantic_authority,
            "promotable_to_truth": self.promotable_to_truth,
        }
        return body

    @property
    def package_hash(self) -> str:
        """Hash of the evidence content and decisions; timing is excluded so identical
        requests over identical sources produce identical packages."""
        body = self.to_dict()
        body["reranker"] = {key: value for key, value in body["reranker"].items() if key != "latency_ms"}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class RetrievalBackend(Protocol):
    backend_id: str
    backend_version: str

    def index(self, records: Iterable[ChunkRecord]) -> None: ...

    def search(self, query: str, scope: RetrievalScope, limit: int) -> list[tuple[str, float]]: ...

    def remove(self, chunk_ids: Iterable[str]) -> None: ...


class GraphExpander(Protocol):
    expander_id: str

    def expand(self, seeds: Sequence[str], scope: RetrievalScope, limit: int) -> list[str]: ...


_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _terms(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(normalize_text(text))]


class InMemoryExactBackend:
    """Exact/term seed: phrase match plus term overlap, scope-filtered first."""

    backend_id = "pados.retrieval.exact.in-memory"
    backend_version = "1"

    def __init__(self) -> None:
        self._records: dict[str, ChunkRecord] = {}

    def index(self, records: Iterable[ChunkRecord]) -> None:
        for record in records:
            self._records[record.chunk_id] = record

    def remove(self, chunk_ids: Iterable[str]) -> None:
        for chunk_id in chunk_ids:
            self._records.pop(chunk_id, None)

    def search(self, query: str, scope: RetrievalScope, limit: int) -> list[tuple[str, float]]:
        terms = set(_terms(query))
        phrase = " ".join(_terms(query))
        scored = []
        for record in self._records.values():
            if not scope.admits(record.source, record.chunk_id, record.normalized_id):
                continue
            words = _terms(record.text)
            overlap = len(terms & set(words))
            if not overlap:
                continue
            score = overlap / len(terms) + (1.0 if phrase and phrase in " ".join(words) else 0.0)
            scored.append((record.chunk_id, round(score, 6)))
        return sorted(scored, key=lambda pair: (-pair[1], pair[0]))[:limit]

    def get(self, chunk_id: str) -> ChunkRecord:
        return self._records[chunk_id]

    def records(self) -> list[ChunkRecord]:
        return [self._records[key] for key in sorted(self._records)]


class InMemoryVectorBackend:
    """Vector retrieval over the PADOS deterministic local embedding."""

    backend_id = "pados.retrieval.vector.in-memory"
    backend_version = "1"
    embedding_version = "pados-deterministic-embedding-32"

    def __init__(self) -> None:
        self._records: dict[str, ChunkRecord] = {}

    @staticmethod
    def embed(text: str) -> tuple[float, ...]:
        return tuple(deterministic_embedding(text))

    def index(self, records: Iterable[ChunkRecord]) -> None:
        for record in records:
            if not record.embedding or record.embedding_version != self.embedding_version:
                raise ValueError("EMBEDDING_VERSION_MISMATCH")
            self._records[record.chunk_id] = record

    def remove(self, chunk_ids: Iterable[str]) -> None:
        for chunk_id in chunk_ids:
            self._records.pop(chunk_id, None)

    def search(self, query: str, scope: RetrievalScope, limit: int) -> list[tuple[str, float]]:
        vector = self.embed(query)
        scored = [
            (record.chunk_id, round(cosine_similarity(vector, record.embedding), 6))
            for record in self._records.values()
            if scope.admits(record.source, record.chunk_id, record.normalized_id)
        ]
        return sorted([pair for pair in scored if pair[1] > 0], key=lambda pair: (-pair[1], pair[0]))[:limit]


class NoGraphExpansion:
    expander_id = "pados.graph.none"

    def expand(self, seeds: Sequence[str], scope: RetrievalScope, limit: int) -> list[str]:
        return []


class AdjacentChunkExpansion:
    """Optional, non-authoritative expansion: neighbouring chunks of seed sources."""

    expander_id = "pados.graph.adjacent-chunks"

    def __init__(self, store: InMemoryExactBackend) -> None:
        self._store = store

    def expand(self, seeds: Sequence[str], scope: RetrievalScope, limit: int) -> list[str]:
        records = self._store.records()
        order = {record.chunk_id: index for index, record in enumerate(records)}
        found: list[str] = []
        for seed in seeds:
            if seed not in order:
                continue
            base = records[order[seed]]
            for offset in (-1, 1):
                index = order[seed] + offset
                if 0 <= index < len(records):
                    neighbour = records[index]
                    if (neighbour.normalized_id == base.normalized_id and neighbour.chunk_id not in seeds
                            and neighbour.chunk_id not in found
                            and scope.admits(neighbour.source, neighbour.chunk_id, neighbour.normalized_id)):
                        found.append(neighbour.chunk_id)
        return found[:limit]
