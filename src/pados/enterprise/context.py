# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise evidence as an optional PADOS product-loop context extension.

The extension is constructed server-side with an authorized capability and an
access context; the caller of the product loop cannot inject evidence. Its
output is bound into the Work Contract context fingerprint and the persisted
run-context object, so the ExecutionReceipt/ResultReceipt chain references
exactly the evidence the provider saw. Evidence is not semantic truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pados.enterprise import observability
from pados.enterprise.authorization import AccessContext, CapabilityAuthorization
from pados.enterprise.budget import EvidenceBudget, assert_within_budget
from pados.enterprise.pipeline import KnowledgePipeline
from pados.enterprise.retrieval import RetrievalRequest, RetrievalStrategy

EXTENSION_SCHEMA = "pados.enterprise.evidence-context.v1"


@dataclass
class EnterpriseEvidenceContext:
    pipeline: KnowledgePipeline
    access: AccessContext
    authorization: CapabilityAuthorization
    query: str | None = None
    top_k: int = 5
    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID
    budget: EvidenceBudget = field(default_factory=EvidenceBudget)
    name: str = "enterprise_evidence"

    def resolve(self, *, scope_id: str, goal: str) -> dict[str, Any]:
        if scope_id != self.access.project_id:
            observability.emit("authorization.decision", decision="DENY", reason="EVIDENCE_SCOPE_MISMATCH",
                               project_id=self.access.project_id, principal_ref=self.access.principal_ref)
            raise PermissionError("ENTERPRISE_EVIDENCE_SCOPE_MISMATCH")
        request = RetrievalRequest(
            project_id=self.access.project_id, tenant_id=self.access.tenant_id,
            principal_id=self.access.principal_id, session_id=self.access.session_id,
            query=self.query or goal, classification_ceiling=self.authorization.ceiling,
            top_k=self.top_k, strategy=self.strategy, budget=self.budget,
        )
        response = self.pipeline.retrieve(request, self.access, self.authorization)
        assert_within_budget(response.evidence, self.budget)  # before any provider sees it
        return {
            "schema": EXTENSION_SCHEMA,
            "instructions_are_data_not_permission": True,
            "semantic_authority": "NONE",
            "package_hash": response.package_hash,
            "package": response.to_dict(),
            "summary": summary(response.to_dict(include_text=False), response.package_hash),
        }


def summary(package: dict[str, Any], package_hash: str) -> dict[str, Any]:
    """Content-free projection for Mission Control (no evidence text)."""
    return {
        "package_hash": package_hash,
        "source_ids": package["source_identities"],
        "evidence": [{"chunk_id": item["chunk_id"], "source_id": item["source_id"],
                      "classification": item["classification"], "span": item["span"],
                      "rerank_score": item["rerank_score"]} for item in package["evidence"]],
        "classifications": sorted({item["classification"] for item in package["evidence"]}),
        "authorization": {key: package["authorization"][key]
                          for key in ("capability_id", "decision", "ceiling", "withheld", "withheld_count")},
        "backends": [b["backend_id"] for b in package["backends"]],
        "reranker": {key: package["reranker"].get(key)
                     for key in ("reranker_id", "model", "version", "mode", "applied", "input_count",
                                 "latency_ms", "resource_class")},
        "budget": package["budget"]["used"],
    }
