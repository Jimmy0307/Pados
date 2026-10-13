# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise knowledge pipeline.

Source -> identity -> classification -> extraction/OCR -> normalized document
-> chunks -> exact seed -> optional graph expansion -> vector -> authorization
(presentation) -> rerank -> budget -> bounded evidence package.

The package is evidence for a governed Work Contract's context. The pipeline
never writes PADOS semantic state.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pados.corpus.ingestion import chunk_text_pages
from pados.enterprise import observability
from pados.enterprise.authorization import (
    AccessContext,
    CapabilityAuthorization,
    presentation_filter,
    retrieval_scope,
)
from pados.enterprise.budget import apply_budget
from pados.enterprise.classification import DataClassification
from pados.enterprise.extraction import (
    ExtractionResult,
    ExtractorSet,
    PdfTextExtractor,
    PlainTextExtractor,
    UnconfiguredOcrExtractor,
)
from pados.enterprise.lineage import (
    EnterpriseSource,
    LineageGraph,
    LineageKind,
    ProcessingVersions,
    derived_id,
    register_source,
)
from pados.enterprise.manifest import CapabilityHealth
from pados.enterprise.rerank import NoOpReranker, RerankCandidate, Reranker, RerankMode, rerank
from pados.enterprise.retrieval import (
    AdjacentChunkExpansion,
    ChunkRecord,
    EvidenceItem,
    GraphExpander,
    InMemoryExactBackend,
    InMemoryVectorBackend,
    NoGraphExpansion,
    RetrievalRequest,
    RetrievalResponse,
    RetrievalStrategy,
)

CHUNKING_VERSION = "pados-chunk-text-pages-v1:1800/250"


@dataclass(frozen=True)
class IngestReport:
    source: EnterpriseSource
    normalized_id: str
    chunk_ids: tuple[str, ...]
    extractor_id: str
    extractor_version: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source.lineage(), "normalized_id": self.normalized_id,
                "chunk_count": len(self.chunk_ids), "chunk_ids": list(self.chunk_ids),
                "extractor_id": self.extractor_id, "extractor_version": self.extractor_version}


class KnowledgePipeline:
    def __init__(
        self, *, extractors: ExtractorSet, exact: InMemoryExactBackend | None = None,
        vector: InMemoryVectorBackend | None = None, graph: GraphExpander | None = None,
        reranker: Reranker | None = None, rerank_mode: RerankMode = RerankMode.ACTIVE,
        chunk_size: int = 1800, overlap: int = 250,
    ) -> None:
        self.extractors = extractors
        self.exact = exact or InMemoryExactBackend()
        self.vector = vector or InMemoryVectorBackend()
        self.graph = graph or NoGraphExpansion()
        self.reranker = reranker or NoOpReranker()
        self.rerank_mode = rerank_mode
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.lineage = LineageGraph()

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY

    # -- ingestion ---------------------------------------------------------
    def ingest(
        self, content: bytes, *, media_type: str, source_type: str, location_ref: str,
        classification: DataClassification, tenant_id: str, project_id: str,
        allowed_principals: Iterable[str] = (), allowed_roles: Iterable[str] = (),
        provenance: Iterable[str] = (),
    ) -> IngestReport:
        with observability.timed() as elapsed:
            extraction = self.extractors.extract(content, media_type=media_type)
        observability.emit("extraction.completed", extractor_id=extraction.extractor_id,
                           latency_ms=elapsed(), classification=classification.value, outcome="OK")
        source = register_source(
            content, source_type=source_type, location_ref=location_ref, classification=classification,
            tenant_id=tenant_id, project_id=project_id,
            parser_version=f"{extraction.extractor_id}:{extraction.extractor_version}",
            allowed_principals=allowed_principals, allowed_roles=allowed_roles, provenance=provenance,
        )
        if extraction.source_hash != source.identity.content_hash:
            raise ValueError("EXTRACTION_SOURCE_HASH_MISMATCH")
        source = dataclasses.replace(source, versions=ProcessingVersions(
            parser_version=source.versions.parser_version, chunking_version=CHUNKING_VERSION,
            embedding_version=self.vector.embedding_version,
        ))
        records = self._chunks(source, extraction)
        self.lineage.add(source.source_id, LineageKind.SOURCE)
        normalized_id = records[0].normalized_id if records else derived_id(
            "NORMALIZED", source.source_id, extraction.normalized_hash)
        self.lineage.add(normalized_id, LineageKind.NORMALIZED_ARTIFACT, [source.source_id])
        for record in records:
            self.lineage.add(record.chunk_id, LineageKind.CHUNK, [normalized_id])
            embedding_id = derived_id("EMBEDDING", record.chunk_id, self.vector.embedding_version)
            self.lineage.add(embedding_id, LineageKind.EMBEDDING, [record.chunk_id])
            self.lineage.add(derived_id("PROJECTION", record.chunk_id, self.exact.backend_id),
                             LineageKind.RETRIEVAL_PROJECTION, [record.chunk_id])
            self.lineage.add(derived_id("PROJECTION", embedding_id, self.vector.backend_id),
                             LineageKind.RETRIEVAL_PROJECTION, [embedding_id])
        self.exact.index(records)
        self.vector.index(records)
        return IngestReport(source, normalized_id, tuple(r.chunk_id for r in records),
                            extraction.extractor_id, extraction.extractor_version)

    def _chunks(self, source: EnterpriseSource, extraction: ExtractionResult) -> list[ChunkRecord]:
        normalized_id = derived_id("NORMALIZED", source.source_id, extraction.normalized_hash)
        records = []
        for chunk in chunk_text_pages(extraction.pages, chunk_size=self.chunk_size, overlap=self.overlap):
            chunk_id = derived_id("CHUNK", normalized_id, CHUNKING_VERSION, chunk.page_number,
                                  chunk.char_start, chunk.char_end)
            records.append(ChunkRecord(
                chunk_id=chunk_id, normalized_id=normalized_id, source=source,
                page_number=chunk.page_number, char_start=chunk.char_start, char_end=chunk.char_end,
                text=chunk.normalized_text, token_estimate=chunk.token_estimate,
                chunking_version=CHUNKING_VERSION, embedding=self.vector.embed(chunk.normalized_text),
                embedding_version=self.vector.embedding_version,
                extractor_id=extraction.extractor_id, extractor_version=extraction.extractor_version,
            ))
        return records

    # -- revocation --------------------------------------------------------
    def plan_revocation(self, source_id: str, *, reason: str) -> dict[str, Any]:
        return self.lineage.plan_revocation(source_id, reason=reason)

    def apply_revocation(self, plan: dict[str, Any]) -> list[str]:
        """Mask revoked projections from retrieval. Human-review items stay untouched."""
        return self.lineage.apply_revocation(plan)

    # -- retrieval ---------------------------------------------------------
    def retrieve(
        self, request: RetrievalRequest, access: AccessContext, authorization: CapabilityAuthorization,
    ) -> RetrievalResponse:
        if (request.project_id, request.tenant_id, request.principal_id, request.session_id) != (
                access.project_id, access.tenant_id, access.principal_id, access.session_id):
            raise PermissionError("RETRIEVAL_REQUEST_ACCESS_MISMATCH")
        scope = retrieval_scope(authorization, access, source_ids=request.source_ids,
                                source_types=request.source_types, excluded=self.lineage.revoked)
        scope = dataclasses.replace(scope, ceiling=min(
            (scope.ceiling, request.classification_ceiling), key=lambda item: item.rank))
        pool = request.top_k * 4
        with observability.timed() as elapsed:
            exact_hits = (self.exact.search(request.query, scope, pool)
                          if request.strategy is not RetrievalStrategy.VECTOR else [])
            expanded = self.graph.expand([cid for cid, _ in exact_hits], scope, request.top_k)
            vector_hits = (self.vector.search(request.query, scope, pool)
                           if request.strategy is not RetrievalStrategy.EXACT else [])
        latency = elapsed()
        fused = _fuse(exact_hits, vector_hits, expanded)
        candidates = [self._candidate(chunk_id, scores) for chunk_id, scores in fused]
        decision = presentation_filter(candidates, access, scope)
        presented: list[EvidenceItem] = decision.presented
        # Authorization precedes reranking: a reranker never sees withheld text.
        ranked, rerank_report = rerank(self.reranker, self.rerank_mode, request.query, [
            RerankCandidate(item.chunk_id, item.text, item.retrieval_scores["fused"]) for item in presented])
        by_id = {item.chunk_id: item for item in presented}
        ordered = [dataclasses.replace(by_id[cid], rerank_score=score) for cid, score in ranked]
        kept, budget_report = apply_budget(ordered[:request.top_k], request.budget)
        response = RetrievalResponse(
            request_fingerprint=request.fingerprint, evidence=tuple(kept),
            authorization={
                "capability_id": authorization.capability_id, "decision": "ALLOW",
                "ceiling": scope.ceiling.value, "grant_source": authorization.grant.get("authorization_source"),
                "coarse_scope": {"tenant_id": scope.tenant_id, "project_id": scope.project_id,
                                 "revoked_excluded": len(scope.excluded)},
                "withheld": dict(sorted(decision.withheld.items())),
                "withheld_count": sum(decision.withheld.values()),
            },
            backends=tuple(
                {"backend_id": backend_id, "backend_version": version} for backend_id, version in (
                    (self.exact.backend_id, self.exact.backend_version),
                    (self.graph.expander_id, "1"),
                    (self.vector.backend_id, self.vector.backend_version))),
            reranker=rerank_report, budget=budget_report,
            versions={"retrieval_contract": "pados.enterprise.retrieval.v1", "chunking": CHUNKING_VERSION,
                      "embedding": self.vector.embedding_version},
        )
        observability.emit("retrieval.completed", capability_id=authorization.capability_id,
                           strategy=request.strategy.value, latency_ms=latency,
                           evidence_count=len(kept), withheld_count=response.authorization["withheld_count"],
                           classification=scope.ceiling.value, project_id=access.project_id)
        observability.emit("evidence.packaged", evidence_count=len(kept), package_hash=response.package_hash,
                           project_id=access.project_id)
        return response

    def _candidate(self, chunk_id: str, scores: dict[str, float]) -> EvidenceItem:
        record = self.exact.get(chunk_id)
        return EvidenceItem(
            chunk_id=chunk_id, source=record.source, text=record.text, page_number=record.page_number,
            char_start=record.char_start, char_end=record.char_end, token_estimate=record.token_estimate,
            retrieval_scores=scores, rerank_score=None,
            provenance={
                **record.source.lineage(), "normalized_id": record.normalized_id,
                "extractor_id": record.extractor_id, "extractor_version": record.extractor_version,
                "chunking_version": record.chunking_version, "embedding_version": record.embedding_version,
            },
        )

    def unauthorized_texts(self, access: AccessContext, authorization: CapabilityAuthorization) -> list[str]:
        """Texts this caller may not see (for output leak checks); never logged.

        Revoked sources are included: once revoked, their text must not reappear in output.
        """
        scope = retrieval_scope(authorization, access, excluded=self.lineage.revoked)
        candidates = [self._candidate(record.chunk_id, {"fused": 0.0}) for record in self.exact.records()]
        return presentation_filter(candidates, access, scope).withheld_texts


def _fuse(exact: list[tuple[str, float]], vector: list[tuple[str, float]],
          expanded: list[str], *, k: int = 60) -> list[tuple[str, dict[str, float]]]:
    """Reciprocal-rank fusion; every component score is kept for provenance."""
    scores: dict[str, dict[str, float]] = {}
    for name, hits in (("exact", exact), ("vector", vector)):
        for rank, (chunk_id, score) in enumerate(hits):
            entry = scores.setdefault(chunk_id, {"fused": 0.0})
            entry[name] = score
            entry["fused"] = round(entry["fused"] + 1.0 / (k + rank + 1), 8)
    for chunk_id in expanded:
        entry = scores.setdefault(chunk_id, {"fused": 0.0})
        entry["graph"] = 1.0
        entry["fused"] = round(entry["fused"] + 0.5 / (k + len(exact) + 1), 8)
    return sorted(scores.items(), key=lambda pair: (-pair[1]["fused"], pair[0]))


def local_pipeline() -> KnowledgePipeline:
    """Local-only default: text/PDF extraction, OCR not configured, lexical rerank."""
    from pados.enterprise.rerank import DeterministicLexicalReranker

    exact = InMemoryExactBackend()
    return KnowledgePipeline(
        extractors=ExtractorSet([PlainTextExtractor(), PdfTextExtractor(), UnconfiguredOcrExtractor()]),
        exact=exact, graph=AdjacentChunkExpansion(exact), reranker=DeterministicLexicalReranker(),
    )
