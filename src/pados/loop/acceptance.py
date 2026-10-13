# SPDX-License-Identifier: AGPL-3.0-only
"""Governed acceptance of P6.10-produced work: ResultCandidate -> verification -> human decision.

* ``submit_result_candidate`` composes the ResultCandidate from the loop's sealed
  CapabilityResults, verifies it, persists it (append-only) and, only when
  verification PASSED, presents it for a human decision (PENDING control approval).
  Scheduler completion therefore stops at WAITING_APPROVAL and never commits.
* ``accept_result_candidate`` requires a CONSUMED step-up proof bound to exactly
  this candidate, writes the canonical ResultReceipt through the existing
  ``StateProofService.execute`` (no auto-commit) and performs the existing
  ``StateProofService.commit_result`` CAS against the expected ScopeVersion.
* ``reject_result_candidate`` records the governed rejection on the same
  semantic audit chain; nothing is committed and no execution history changes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.execution.context import canonical_hash
from pados.loop.amendments import _task_envelope_from_body, rebuild_p6_10_lineage_projection
from pados.state_proof.service import StateProofService
from pados.work_protocol.acceptance import (
    AdvisoryEvidence,
    CandidateVerification,
    result_receipt_document,
    result_receipt_specification,
    scope_version_number,
    verified_candidate,
    verify_result_candidate,
)
from pados.work_protocol.composition import compose_results
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import (
    AcceptanceLineageProjection,
    project_acceptance_lineage,
    project_p6_10_lineage,
    project_work_graph,
)
from pados.work_protocol.models import (
    CapabilityResult,
    CoverageMap,
    ResultCandidate,
    ResultReceiptRef,
    TaskEnvelope,
    VerificationStatus,
    WorkGraph,
)

CANDIDATE_SCHEMA = "pados.result-acceptance-candidate.v1"
# A consumed proof authorizes exactly one prompt acceptance attempt, not a later one.
ACCEPTANCE_WINDOW = timedelta(minutes=5)
ACCEPTANCE_OPERATION = "RESULT_ACCEPTANCE"
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class LoopTaskState:
    loop: dict[str, Any]
    envelope: TaskEnvelope
    results: tuple[CapabilityResult, ...]
    receipts: dict[str, dict[str, Any]]
    artifact_digests: dict[str, str]

    @property
    def coverage(self) -> CoverageMap:
        return build_coverage(self.envelope, self.results)

    def compose(self) -> ResultCandidate:
        return compose_results(self.envelope, self.results, self.coverage)


def acceptance_operation_id(candidate_id: str) -> str:
    return f"result-acceptance:{candidate_id}"


def load_loop_task_state(session: Session, loop_id: str) -> LoopTaskState:
    """Reload task, sealed results, receipts and recomputed artifact digests from authority."""
    loop_row = session.execute(
        text("SELECT * FROM task_loops WHERE id = CAST(:id AS uuid)"), {"id": loop_id}
    ).mappings().first()
    if loop_row is None:
        raise ValueError("TASK_LOOP_NOT_FOUND")
    loop = dict(loop_row)
    body = session.execute(
        text("SELECT body FROM control_work_requests WHERE id = CAST(:id AS uuid)"),
        {"id": str(loop["request_id"])},
    ).scalar_one()
    envelope = _task_envelope_from_body(body)
    if envelope is None:
        raise ValueError("TASK_ENVELOPE_REQUIRED_FOR_ACCEPTANCE")
    events = session.execute(
        text(
            """
            SELECT payload_json FROM task_loop_events
            WHERE loop_id = CAST(:id AS uuid) AND event_type = 'CAPABILITY_RESULT_NORMALIZED'
            ORDER BY sequence_number
            """
        ),
        {"id": loop_id},
    ).scalars().all()
    results: list[CapabilityResult] = []
    receipts: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event if isinstance(event, dict) else json.loads(str(event))
        result = CapabilityResult.model_validate(payload["capability_result"])
        if result.content_hash != payload.get("capability_result_hash"):
            raise ValueError("PERSISTED_CAPABILITY_RESULT_HASH_MISMATCH")
        receipt = payload.get("execution_receipt")
        if not isinstance(receipt, dict):
            raise ValueError("PERSISTED_EXECUTION_RECEIPT_MISSING")
        results.append(result)
        receipts[str(receipt.get("receipt_id"))] = receipt
    artifact_digests: dict[str, str] = {}
    declared = {artifact.artifact_id for result in results for artifact in result.artifacts}
    for artifact_id in sorted(declared):
        prefix, _, row_id = artifact_id.partition(":")
        if prefix != "worker-artifact":
            continue
        row = session.execute(
            text("SELECT path, project_id::text AS project_id FROM worker_job_artifacts "
                 "WHERE id = CAST(:id AS uuid)"),
            {"id": row_id},
        ).mappings().first()
        if row is None or str(row["project_id"]) != envelope.project_ref:
            continue
        path = Path(str(row["path"]))
        try:
            if path.is_file() and path.stat().st_size <= _MAX_ARTIFACT_BYTES:
                artifact_digests[artifact_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return LoopTaskState(loop, envelope, tuple(results), receipts, artifact_digests)


def verify_loop_candidate(
    state: LoopTaskState,
    *,
    current_scope_version: int | None,
    advisory_evidence: Sequence[AdvisoryEvidence] = (),
) -> tuple[ResultCandidate, CandidateVerification]:
    coverage = state.coverage
    candidate = compose_results(state.envelope, state.results, coverage)
    verification = verify_result_candidate(
        state.envelope,
        state.results,
        coverage,
        candidate,
        execution_receipts=state.receipts,
        artifact_digests=state.artifact_digests,
        current_scope_version=current_scope_version,
        advisory_evidence=advisory_evidence,
    )
    if verification.status is VerificationStatus.PASSED:
        candidate = verified_candidate(candidate, verification)
    return candidate, verification


def presentation_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "RESULT_CANDIDATE",
        "candidate_id": str(row["candidate_id"]),
        "candidate_digest": str(row["candidate_digest"]),
        "task_id": str(row["task_id"]),
        "project_id": str(row["project_id"]),
        "classification": str(row["classification"]),
        "expected_scope_version": int(row["expected_scope_version"]),
        "verification_digest": str(row["verification_digest"]),
        "loop_id": str(row["loop_id"]),
    }


def submit_result_candidate(
    session: Session,
    loop_id: str,
    *,
    current_scope_version: int | None,
    advisory_evidence: Sequence[AdvisoryEvidence] = (),
) -> dict[str, Any]:
    """Compose + verify the completed loop's result; present it only if verification PASSED."""
    locked = session.execute(
        text("SELECT status FROM task_loops WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": loop_id}
    ).scalar_one_or_none()
    if locked is None:
        raise ValueError("TASK_LOOP_NOT_FOUND")
    if locked != "COMPLETED":
        raise PermissionError("RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP")
    state = load_loop_task_state(session, loop_id)
    candidate, verification = verify_loop_candidate(
        state, current_scope_version=current_scope_version, advisory_evidence=advisory_evidence
    )
    existing = session.execute(
        text("SELECT * FROM result_acceptance_candidates WHERE candidate_id = :id"),
        {"id": candidate.candidate_id},
    ).mappings().first()
    if existing is not None:
        if str(existing["candidate_digest"]) != candidate.content_hash:
            raise ValueError("RESULT_CANDIDATE_IDEMPOTENCY_CONFLICT")
        return dict(existing)
    row: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "candidate_digest": candidate.content_hash,
        "task_id": state.envelope.task_id,
        "project_id": state.envelope.project_ref,
        "request_id": str(state.loop["request_id"]),
        "loop_id": loop_id,
        "plan_id": str(state.loop["active_plan_id"]),
        "classification": state.envelope.classification.value,
        "expected_scope_version": scope_version_number(state.envelope.scope_version_ref),
        "candidate_json": json.dumps(candidate.model_dump(mode="json"), sort_keys=True),
        "verification_json": json.dumps(verification.model_dump(mode="json"), sort_keys=True),
        "verification_digest": verification.digest,
        "verification_status": verification.status.value,
        "approval_id": None,
    }
    if verification.status is VerificationStatus.PASSED:
        approval = control_service.create_approval(
            session,
            {
                "project_id": state.envelope.project_ref,
                "proposal_hash": canonical_hash(presentation_payload(row)),
                "proposal_summary": f"Result acceptance for task {state.envelope.task_id}"[:300],
                "idempotency_key": f"result-candidate:{candidate.content_hash[:64]}",
            },
        )
        row["approval_id"] = str(approval["id"])
    inserted = session.execute(
        text(
            """
            INSERT INTO result_acceptance_candidates(
                candidate_id, candidate_digest, task_id, project_id, request_id, loop_id, plan_id,
                classification, expected_scope_version, candidate_json, verification_json,
                verification_digest, verification_status, approval_id, protocol_version, schema_version)
            VALUES (
                :candidate_id, :candidate_digest, :task_id, CAST(:project_id AS uuid),
                CAST(:request_id AS uuid), CAST(:loop_id AS uuid), CAST(:plan_id AS uuid),
                :classification, :expected_scope_version, CAST(:candidate_json AS jsonb),
                CAST(:verification_json AS jsonb), :verification_digest, :verification_status,
                CAST(:approval_id AS uuid), '1.0', :schema_version)
            RETURNING *
            """
        ),
        {**row, "schema_version": CANDIDATE_SCHEMA},
    ).mappings().one()
    session.execute(
        text(
            """
            INSERT INTO task_loop_events(loop_id, project_id, event_type, payload_json)
            VALUES (CAST(:loop_id AS uuid), CAST(:project_id AS uuid), 'RESULT_CANDIDATE_SUBMITTED',
                    CAST(:payload AS jsonb))
            """
        ),
        {
            "loop_id": loop_id,
            "project_id": state.envelope.project_ref,
            "payload": json.dumps(
                {"candidate_id": candidate.candidate_id, "candidate_digest": candidate.content_hash,
                 "verification_status": verification.status.value,
                 "verification_digest": verification.digest,
                 "failure_codes": list(verification.failure_codes),
                 "semantic_state": "WAITING_APPROVAL"
                 if verification.status is VerificationStatus.PASSED else "VERIFICATION_FAILED"},
                sort_keys=True,
            ),
        },
    )
    return dict(inserted)


def candidate_row(
    session: Session, *, candidate_id: str | None = None, approval_id: str | None = None
) -> dict[str, Any] | None:
    """Candidate + presentation approval + terminal decision (if any)."""
    if (candidate_id is None) == (approval_id is None):
        raise ValueError("CANDIDATE_LOOKUP_REQUIRES_ONE_KEY")
    clause = "c.candidate_id = :key" if candidate_id is not None else "c.approval_id::text = :key"
    row = session.execute(
        text(
            f"""
            SELECT c.*, c.project_id::text AS project_ref, c.loop_id::text AS loop_ref,
                   c.approval_id::text AS approval_ref, a.status AS approval_status,
                   a.proposal_hash, a.version AS approval_version,
                   d.decision, d.actor_id AS decided_by, d.proof_id::text AS decision_proof_id,
                   d.decided_at, d.next_action
            FROM result_acceptance_candidates c
            LEFT JOIN control_approvals a ON a.id = c.approval_id
            LEFT JOIN result_acceptance_decisions d ON d.candidate_id = c.candidate_id
            WHERE {clause}
            """
        ),
        {"key": candidate_id if candidate_id is not None else approval_id},
    ).mappings().first()
    return None if row is None else dict(row)


def pending_candidate(
    session: Session, *, candidate_id: str | None = None, approval_id: str | None = None,
    current_scope_version: int | None = None, reverify: bool = False,
) -> dict[str, Any] | None:
    """A decidable candidate: verified, presented, untampered, undecided, and (optionally)
    still exactly what the loop's sealed results compose to right now."""
    row = candidate_row(session, candidate_id=candidate_id, approval_id=approval_id)
    if row is None or row["verification_status"] != "PASSED" or row["decision"] is not None:
        return None
    if row["approval_status"] != "PENDING":
        return None
    if canonical_hash(presentation_payload(row)) != str(row["proposal_hash"]).lower():
        return None
    candidate = ResultCandidate.model_validate(row["candidate_json"])
    if candidate.content_hash != row["candidate_digest"] or candidate.candidate_id != row["candidate_id"]:
        return None
    if reverify:
        state = load_loop_task_state(session, str(row["loop_ref"]))
        current, verification = verify_loop_candidate(state, current_scope_version=current_scope_version)
        if (
            verification.status is not VerificationStatus.PASSED
            or current.candidate_id != candidate.candidate_id
            or current.content_hash != row["candidate_digest"]
        ):
            return None
    return row


def record_candidate_decision(
    session: Session,
    *,
    candidate_id: str,
    decision: str,
    actor_id: str,
    session_id: str,
    proof_id: str,
    binding_hash: str,
    step_up_binding_digest: str,
    next_action: str | None = None,
) -> dict[str, Any]:
    proof = session.execute(
        text(
            """
            SELECT * FROM approval_step_up_challenges
            WHERE proof_id = CAST(:proof AS uuid) FOR UPDATE
            """
        ),
        {"proof": proof_id},
    ).mappings().first()
    if (
        proof is None
        or proof["status"] != "CONSUMED"
        or proof["kind"] != "RESULT_CANDIDATE"
        or proof["candidate_id"] != candidate_id
        or proof["action"] != decision
        or proof["actor_id"] != actor_id
        or proof["session_id"] != session_id
        or proof["binding_digest"] != step_up_binding_digest
    ):
        raise PermissionError("RESULT_DECISION_REQUIRES_CONSUMED_BOUND_PROOF")
    row = session.execute(
        text(
            """
            INSERT INTO result_acceptance_decisions(
                candidate_id, decision, actor_id, session_id, proof_id, binding_hash,
                step_up_binding_digest, next_action)
            VALUES (:candidate_id, :decision, :actor_id, :session_id, CAST(:proof_id AS uuid),
                    :binding_hash, :step_up_binding_digest, :next_action)
            ON CONFLICT (candidate_id) DO NOTHING
            RETURNING *
            """
        ),
        {"candidate_id": candidate_id, "decision": decision, "actor_id": actor_id,
         "session_id": session_id, "proof_id": proof_id, "binding_hash": binding_hash,
         "step_up_binding_digest": step_up_binding_digest, "next_action": next_action},
    ).mappings().first()
    if row is None:
        raise PermissionError("RESULT_CANDIDATE_ALREADY_DECIDED")
    return dict(row)


class _DocumentProvider:
    """Records an already-composed, already-approved result document verbatim."""

    def __init__(self, document: str) -> None:
        self._document = document

    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        del specification
        return self._document


async def accept_result_candidate(
    *,
    operational_engine: Engine,
    proof_service: StateProofService,
    candidate_id: str,
    expected_version: int,
    actor_id: str,
) -> dict[str, Any]:
    """ResultReceipt + existing CAS. Requires the recorded APPROVE decision and its consumed proof."""
    with Session(operational_engine) as session:
        row = candidate_row(session, candidate_id=candidate_id)
        if row is None or row["decision"] != "APPROVE" or row["decided_by"] != actor_id:
            raise PermissionError("RESULT_ACCEPTANCE_REQUIRES_RECORDED_APPROVAL")
        proof = session.execute(
            text(
                """
                SELECT * FROM approval_step_up_challenges
                WHERE proof_id = CAST(:proof AS uuid)
                """
            ),
            {"proof": row["decision_proof_id"]},
        ).mappings().one()
        if (
            proof["status"] != "CONSUMED"
            or proof["action"] != "APPROVE"
            or proof["candidate_id"] != candidate_id
            or proof["candidate_digest"] != row["candidate_digest"]
            or int(proof["expected_scope_version"]) != int(row["expected_scope_version"])
            or int(row["expected_scope_version"]) != expected_version
        ):
            raise PermissionError("RESULT_ACCEPTANCE_PROOF_BINDING_MISMATCH")
        consumed_at = proof["proof_consumed_at"]
        state = load_loop_task_state(session, str(row["loop_ref"]))
        # Same session: the sealed results must still compose to exactly the approved candidate.
        current, current_verification = verify_loop_candidate(
            state, current_scope_version=int(row["expected_scope_version"]))
        if (current_verification.status is not VerificationStatus.PASSED
                or current.candidate_id != candidate_id or current.content_hash != row["candidate_digest"]):
            raise PermissionError("RESULT_ACCEPTANCE_CANDIDATE_CHANGED")
    candidate = ResultCandidate.model_validate(row["candidate_json"])
    verification = CandidateVerification.model_validate(row["verification_json"])
    if candidate.content_hash != row["candidate_digest"] or verification.digest != row["verification_digest"]:
        raise ValueError("RESULT_CANDIDATE_EVIDENCE_TAMPERED")
    proof_ref = f"step-up-proof:{proof['proof_id']}"
    already_sealed = proof_service.get_result_for_operation(acceptance_operation_id(candidate_id))
    if already_sealed is None and (consumed_at is None or datetime.now(UTC) - consumed_at > ACCEPTANCE_WINDOW):
        raise PermissionError("RESULT_ACCEPTANCE_WINDOW_EXPIRED")
    specification = result_receipt_specification(
        state.envelope,
        candidate,
        verification,
        state.results,
        approval={
            "proof_ref": proof_ref,
            "binding_digest": str(proof["binding_digest"]),
            "actor_id": actor_id,
            "session_id": str(proof["session_id"]),
            "approval_ref": str(row["approval_ref"]),
            "decided_at": str(row["decided_at"]),
            "presence_method": str(proof["presence_method"]),
            "assertion_digest": str(proof["assertion_digest"]),
        },
    )
    scope_id = str(row["project_ref"])
    operation_id = acceptance_operation_id(candidate_id)
    outcome = await proof_service.execute(
        scope_id=scope_id,
        operation_id=operation_id,
        specification=specification,
        context_receipt={"fingerprint": verification.digest, "candidate_digest": candidate.content_hash},
        provider=_DocumentProvider(result_receipt_document(candidate, state.coverage)),
        operation_type=ACCEPTANCE_OPERATION,
        expected_version=expected_version,
        correlation_id=candidate_id,
        provider_id="PADOS_RESULT_ACCEPTANCE",
        runtime_id="pados.loop.acceptance",
        model_id="NONE",
        contract_id="pados.result-receipt.v1",
        attempt=1,
        auto_commit=False,
        materialize_projection=False,
    )
    receipt = outcome["result"]
    if receipt.get("status") != "SUCCEEDED":
        raise ValueError("RESULT_RECEIPT_NOT_SEALED")
    commit = proof_service.commit_result(
        result_id=str(receipt["id"]),
        scope_id=scope_id,
        expected_version=expected_version,
        candidate_ref=str(receipt["id"]),
        manifest_hash=str(receipt["input_manifest_hash"]),
        request_id=candidate_id,
        idempotency_key=operation_id,
        actor=f"REMOTE:{actor_id}",
        reason="verified result candidate approved with fresh step-up",
        approval_ref=proof_ref,
    )
    return {**commit, "result_receipt_id": str(receipt["id"]),
            "result_receipt_manifest_hash": str(receipt["input_manifest_hash"])}


def result_receipt_ref(
    candidate: ResultCandidate, receipt: Mapping[str, Any]
) -> ResultReceiptRef:
    return ResultReceiptRef(
        protocol_version="1.0",
        schema_version=ResultReceiptRef.SCHEMA,
        receipt_id=str(receipt["id"]),
        candidate_id=candidate.candidate_id,
        task_id=candidate.task_id,
        project_ref=candidate.project_ref,
        classification=candidate.classification,
        digest=str(receipt["input_manifest_hash"]),
        scope_version_ref=candidate.scope_version_ref,
    )


def reject_result_candidate(
    *,
    proof_service: StateProofService,
    row: Mapping[str, Any],
    actor_id: str,
    proof_ref: str,
    binding_hash: str,
) -> dict[str, Any]:
    """Governed rejection: audit only; evidence stays immutable; no CAS."""
    return proof_service.record_semantic_rejection(
        scope_id=str(row["project_ref"]),
        target=f"result_candidate:{row['candidate_id']}",
        actor=f"REMOTE:{actor_id}",
        reason="result candidate rejected with fresh step-up",
        request_id=str(row["candidate_id"]),
        idempotency_key=f"result-rejection:{row['candidate_id']}",
        approval_ref=proof_ref,
        evidence={"candidate_digest": str(row["candidate_digest"]),
                  "verification_digest": str(row["verification_digest"]),
                  "binding_hash": binding_hash, "next_action": "REQUEST_REPLAN"},
    )


def acceptance_projection(
    session: Session, proof_service: StateProofService | None, candidate_id: str
) -> AcceptanceLineageProjection | None:
    """Acceptance lineage read only from persisted records and the proof-database commit."""
    row = candidate_row(session, candidate_id=candidate_id)
    if row is None:
        return None
    receipt_id = receipt_digest = commit_status = None
    committed_version: int | None = None
    if proof_service is not None:
        with proof_service._session() as proof_session:
            receipt = proof_session.execute(
                text(
                    """
                    SELECT r.id::text AS id, r.input_manifest_hash, c.status, c.new_version
                    FROM state_proof_results r
                    LEFT JOIN state_proof_commits c ON c.result_id = r.id
                    WHERE r.operation_id = :op AND r.attempt = 1
                    """
                ),
                {"op": acceptance_operation_id(candidate_id)},
            ).mappings().first()
        if receipt is not None:
            receipt_id, receipt_digest = str(receipt["id"]), str(receipt["input_manifest_hash"])
            commit_status = None if receipt["status"] is None else str(receipt["status"])
            committed_version = None if receipt["new_version"] is None else int(receipt["new_version"])
    return AcceptanceLineageProjection(
        task_id=str(row["task_id"]),
        project_ref=str(row["project_ref"]),
        candidate_id=candidate_id,
        candidate_digest=str(row["candidate_digest"]),
        verification_status=row["verification_status"],
        verification_digest=str(row["verification_digest"]),
        approval_ref=None if row["approval_ref"] is None else str(row["approval_ref"]),
        decision=row["decision"],
        decided_by=None if row["decided_by"] is None else str(row["decided_by"]),
        proof_ref=None if row["decision_proof_id"] is None else f"step-up-proof:{row['decision_proof_id']}",
        next_action=row["next_action"],
        result_receipt_id=receipt_id,
        result_receipt_digest=receipt_digest,
        commit_status=commit_status,
        committed_scope_version=committed_version,
    )


def rebuild_task_work_graph(
    session: Session, proof_service: StateProofService | None, loop_id: str
) -> WorkGraph:
    """Rebuild the complete lineage projection for one loop from authority and receipts only."""
    state = load_loop_task_state(session, loop_id)
    coverage = state.coverage
    candidate_id = session.execute(
        text("SELECT candidate_id FROM result_acceptance_candidates WHERE loop_id = CAST(:id AS uuid) "
             "ORDER BY created_at DESC LIMIT 1"),
        {"id": loop_id},
    ).scalar_one_or_none()
    candidate: ResultCandidate | None = None
    acceptance = None
    if candidate_id is not None:
        row = candidate_row(session, candidate_id=str(candidate_id))
        if row is not None:
            candidate = ResultCandidate.model_validate(row["candidate_json"])
            if candidate.content_hash != row["candidate_digest"]:
                raise ValueError("RESULT_CANDIDATE_EVIDENCE_TAMPERED")
        acceptance = acceptance_projection(session, proof_service, str(candidate_id))
    if candidate is not None and acceptance is not None and acceptance.result_receipt_id is not None:
        candidate = candidate.model_copy(update={"result_receipt_ref": ResultReceiptRef(
            protocol_version="1.0", schema_version=ResultReceiptRef.SCHEMA,
            receipt_id=acceptance.result_receipt_id, candidate_id=candidate.candidate_id,
            task_id=candidate.task_id, project_ref=candidate.project_ref,
            classification=candidate.classification, digest=str(acceptance.result_receipt_digest),
            scope_version_ref=candidate.scope_version_ref)})
    graph = project_work_graph(state.envelope, state.results, candidate, coverage=coverage)
    graph = project_p6_10_lineage(
        graph,
        rebuild_p6_10_lineage_projection(
            session, task_envelope=state.envelope, plan_id=str(state.loop["active_plan_id"]), loop_id=loop_id
        ),
    )
    if acceptance is not None:
        graph = project_acceptance_lineage(graph, acceptance)
    return graph
