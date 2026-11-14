# SPDX-License-Identifier: AGPL-3.0-only
"""The document -> grounded ResultCandidate hand-off, built only from existing PADOS functions.

``ExtractiveClaimDrafter`` is a deterministic, UNTRUSTED claim provider for the Structured Problem Solving pack: it
proposes one claim per structured evidence item, filed under the profile section that can carry its subject. It
is a reference provider (a model reached through the Worker Router can replace it); its output is parsed
strictly and grounded by the existing pipeline like any other provider's, so it can never promote what the
evidence does not state.

``to_result_candidate`` projects a verified report onto a ``ResultCandidate`` through the EXISTING
``result_projection`` -> ``normalize_capability_result`` -> ``build_coverage`` -> ``compose_results`` path. Every
authority field (authorization, receipt reference, provenance) is supplied by the caller (PADOS); the candidate
stays ``PENDING_GOVERNED_ACCEPTANCE`` and nothing here can accept it or commit semantic state.
"""
from __future__ import annotations

import hashlib
from typing import Any

from pados.capability.artifacts import ArtifactRef, ArtifactStore
from pados.capability.executor import ExecutionResult, ExecutionStatus
from pados.capability.planner import ExecutionPlan
from pados.capability.provenance import ProvenanceRefused, resolved_provenance
from pados.capability.receipts import VerificationStatus
from pados.capability.task import TaskContract
from pados.packs.document_intelligence.ranking import DocumentIntelligenceError
from pados.packs.intelligence.report import ProvenanceRegistrar, derived_classification
from pados.packs.problem_solving import pack as ps
from pados.packs.problem_solving.contracts import ClaimSet, EvidencePack, ProblemSolvingProfile, ProblemSolvingReport
from pados.packs.problem_solving.pipeline import result_projection
from pados.work_protocol.compatibility import normalize_capability_result
from pados.work_protocol.composition import compose_results
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.models import (
    AuthorizationMetadata,
    CapabilityRequest,
    ExecutionReceiptRef,
    Provenance,
    ResourceKind,
    TaskEnvelope,
)


EVIDENCE_STAGE = "evidence"


def admit_verified_evidence(task: TaskContract, plan: ExecutionPlan, result: ExecutionResult, *,
                            store: ArtifactStore, provenance: ProvenanceRegistrar) -> tuple[ArtifactRef, EvidencePack]:
    """Operator step between the two tasks (the pattern of ``build_report_task``): the evidence pack of ONE
    completed, structurally verified run whose evidence stage PASSED verification is registered as an input of the
    task's own project, at the classification its inputs resolve to, all or nothing. A pack registered to another
    project or at another classification is refused; nothing is pinned or authorized here -- the next task does
    that with the returned pack's digest."""
    receipt = result.plan_receipt
    if (plan.task_hash != task.task_hash or receipt.plan_id != plan.plan_id or receipt.plan_hash != plan.plan_hash
            or result.status is not ExecutionStatus.COMPLETED or not receipt.verify_structure()):
        raise DocumentIntelligenceError("DI_EVIDENCE_RUN_NOT_COMPLETED")
    stages = [stage for stage in receipt.stages if stage.stage_id == EVIDENCE_STAGE]
    if len(stages) != 1 or stages[0].verification is not VerificationStatus.PASSED:
        raise DocumentIntelligenceError("DI_EVIDENCE_NOT_VERIFIED")
    found = [ref for ref in stages[0].outputs if ref.kind == ps.KIND_EVIDENCE]
    if len(found) != 1:
        raise DocumentIntelligenceError("DI_EVIDENCE_OUTPUT_AMBIGUOUS")
    ref = found[0]
    pack = ps.load_evidence_pack(bytes(store.get(ref)))
    try:
        classification = derived_classification(task, provenance)
    except ValueError:
        classification = None
    if classification is None:
        raise DocumentIntelligenceError("DI_EVIDENCE_PROVENANCE_REFUSED")
    try:
        existing = resolved_provenance(provenance, ref.sha256)
    except ProvenanceRefused as exc:
        if exc.code != "INPUT_PROVENANCE_UNKNOWN":
            raise DocumentIntelligenceError("DI_EVIDENCE_PROVENANCE_REFUSED") from None
        existing = None
    if existing is None:
        try:
            provenance.register_many([(ref.sha256, task.project_id, classification)])
        except ProvenanceRefused:
            raise DocumentIntelligenceError("DI_EVIDENCE_PROVENANCE_REFUSED") from None
    elif (existing.project_id, existing.classification) != (task.project_id, classification):
        raise DocumentIntelligenceError("DI_EVIDENCE_REGISTERED_ELSEWHERE")
    return ref, pack


class ExtractiveClaimDrafter:
    """Deterministic reference ``ClaimProvider``: one claim per structured evidence item (untrusted output)."""

    provider_id = "document-intelligence.extractive-drafter"

    def draft_claims(self, pack: EvidencePack, profile: ProblemSolvingProfile) -> object:
        section_of = {subject: section.section_id for section in profile.sections for subject in section.subjects}
        claims: list[dict[str, Any]] = []
        for item in pack.items:
            if item.subject is None or item.assertion is None or item.subject not in section_of:
                continue
            claims.append({"claim_id": f"cl.{item.evidence_id}"[:64], "case_ids": [item.case_id],
                           "section": section_of[item.subject], "subject": item.subject,
                           "assertion": item.assertion, "text": item.statement, "citations": [item.evidence_id]})
        return {"schema_version": "pados.problem_solving.claims.v1", "claims": claims}


def to_result_candidate(*, pack: EvidencePack, claims: ClaimSet, report: ProblemSolvingReport,
                        envelope: TaskEnvelope, goal_id: str, authorization: AuthorizationMetadata,
                        request_id: str, result_id: str, worker_id: str, receipt_hash: str) -> Any:
    """A ``ResultCandidate`` for a verified report (``receipt_hash`` is the solve stage's receipt hash)."""
    projection = result_projection(pack, claims, report, goal_id=goal_id)
    request = CapabilityRequest(protocol_version="1.0", schema_version=CapabilityRequest.SCHEMA,
                                request_id=request_id, task_id=envelope.task_id, goal_id=goal_id,
                                capability=ps.CAPABILITY_ID, authorization=authorization)
    result = normalize_capability_result(
        projection, request=request, result_id=result_id, resource_id=ps.ADAPTER_ID,
        resource_kind=ResourceKind.TOOL, provider=None,
        provenance=Provenance(worker_id=worker_id, resource_id=ps.ADAPTER_ID, resource_kind=ResourceKind.TOOL,
                              adapter_id=ps.ADAPTER_ID, adapter_version=ps.ADAPTER.version,
                              normalized_from=ps.PACK_ID,
                              provider_response_digest=hashlib.sha256(repr(projection).encode()).hexdigest(),
                              input_digests=(pack.digest,)),
        authorization=authorization,
        execution_receipt_ref=ExecutionReceiptRef(protocol_version="1.0", schema_version=ExecutionReceiptRef.SCHEMA,
                                                  receipt_id=f"execution-sha256:{receipt_hash}",
                                                  digest=receipt_hash))
    return compose_results(envelope, (result,), build_coverage(envelope, (result,)))
