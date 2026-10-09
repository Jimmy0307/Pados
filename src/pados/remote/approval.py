# SPDX-License-Identifier: AGPL-3.0-only
"""Remote approval binding.

A remote approval is bound to the authenticated actor, the exact ResultReceipt
candidate, the ScopeVersion observed when the decision was presented, and an
explicit decision with timestamp, and to the single-use step-up proof that a
fresh, user-verified presence check produced for exactly this decision
(``pados.remote.step_up``). It never commits directly: acceptance still flows
through the existing state gateway / StateProofService CAS commit with
``expected_version`` equal to the bound ScopeVersion. A changed ScopeVersion
fails closed.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pados.execution.context import canonical_hash
from pados.remote.intent import RemoteIntentEnvelope, RemoteIntentError, RemoteIntentType
from pados.remote.step_up import ApprovalTarget


class RemoteDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class RemoteApprovalOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"
    SCOPE_VERSION_CHANGED = "SCOPE_VERSION_CHANGED"
    CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
    NOT_PENDING_HUMAN_DECISION = "NOT_PENDING_HUMAN_DECISION"
    ACTOR_NOT_AUTHORIZED = "ACTOR_NOT_AUTHORIZED"
    COMMIT_CONFLICT = "COMMIT_CONFLICT"


@dataclass(frozen=True)
class PendingDecision:
    """What PADOS presented to the human (read from PADOS, not the client)."""

    approval_ref: str
    run_id: str
    candidate_receipt_id: str
    scope_id: str
    scope_version: int
    manifest_hash: str
    semantic_state: str
    classification: str = ""
    kind: str = "PRODUCT_RUN"  # PRODUCT_RUN | RESULT_CANDIDATE
    task_id: str | None = None
    goal_id: str | None = None

    def target(self) -> ApprovalTarget:
        """The authoritative step-up target for this decision."""
        return ApprovalTarget(
            approval_ref=self.approval_ref,
            kind=self.kind,
            project_id=self.scope_id,
            task_id=self.task_id or self.run_id,
            goal_id=self.goal_id,
            candidate_id=self.candidate_receipt_id,
            candidate_digest=self.manifest_hash,
            classification=self.classification,
            expected_scope_version=self.scope_version,
            target_run_id=self.run_id,
        )


@dataclass(frozen=True)
class RemoteApprovalBinding:
    actor_id: str
    session_id: str
    approval_ref: str
    run_id: str
    candidate_receipt_id: str
    scope_id: str
    bound_scope_version: int
    manifest_hash: str
    decision: RemoteDecision
    proof_ref: str = ""
    step_up_binding_digest: str = ""
    kind: str = "PRODUCT_RUN"
    decided_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def binding_hash(self) -> str:
        return canonical_hash(
            {
                "actor_id": self.actor_id,
                "session_id": self.session_id,
                "approval_ref": self.approval_ref,
                "run_id": self.run_id,
                "candidate_receipt_id": self.candidate_receipt_id,
                "scope_id": self.scope_id,
                "bound_scope_version": self.bound_scope_version,
                "manifest_hash": self.manifest_hash,
                "decision": self.decision.value,
                "proof_ref": self.proof_ref,
                "step_up_binding_digest": self.step_up_binding_digest,
                "kind": self.kind,
                "decided_at": self.decided_at,
            }
        )


def bind_remote_decision(
    envelope: RemoteIntentEnvelope,
    pending: PendingDecision,
    *,
    candidate_receipt_id: str,
    proof_ref: str = "",
    step_up_binding_digest: str = "",
) -> RemoteApprovalBinding:
    if envelope.intent_type not in {RemoteIntentType.APPROVE, RemoteIntentType.REJECT}:
        raise RemoteIntentError("NOT_A_DECISION_INTENT")
    if envelope.approval_ref != pending.approval_ref or envelope.target_run_id != pending.run_id:
        raise RemoteIntentError("APPROVAL_TARGET_MISMATCH")
    return RemoteApprovalBinding(
        actor_id=envelope.actor_id,
        session_id=envelope.session_id,
        approval_ref=pending.approval_ref,
        run_id=pending.run_id,
        candidate_receipt_id=candidate_receipt_id,
        scope_id=pending.scope_id,
        bound_scope_version=pending.scope_version,
        manifest_hash=pending.manifest_hash,
        decision=RemoteDecision(envelope.intent_type.value),
        proof_ref=proof_ref,
        step_up_binding_digest=step_up_binding_digest,
        kind=pending.kind,
    )


# commit(scope_id, expected_version, candidate_ref, manifest_hash, approval_ref, actor)
CommitFn = Callable[..., Awaitable[Any]]
ActorCheck = Callable[[str, str], bool]  # (actor_id, scope_id) -> may approve


@dataclass(frozen=True)
class RemoteApprovalResult:
    outcome: RemoteApprovalOutcome
    binding_hash: str
    commit: Any = None


async def apply_remote_decision(
    binding: RemoteApprovalBinding,
    *,
    current: PendingDecision,
    current_scope_version: int,
    actor_may_approve: ActorCheck,
    commit: CommitFn,
    decide: Callable[[RemoteApprovalBinding], Awaitable[None]] | None = None,
) -> RemoteApprovalResult:
    """Re-verify the binding against current PADOS state, then use the CAS path."""
    digest = binding.binding_hash
    if not actor_may_approve(binding.actor_id, binding.scope_id):
        return RemoteApprovalResult(RemoteApprovalOutcome.ACTOR_NOT_AUTHORIZED, digest)
    if current.semantic_state != "ACCEPTED_PENDING_HUMAN_DECISION":
        return RemoteApprovalResult(RemoteApprovalOutcome.NOT_PENDING_HUMAN_DECISION, digest)
    if (
        current.candidate_receipt_id != binding.candidate_receipt_id
        or current.manifest_hash != binding.manifest_hash
        or current.approval_ref != binding.approval_ref
    ):
        return RemoteApprovalResult(RemoteApprovalOutcome.CANDIDATE_MISMATCH, digest)
    if current_scope_version != binding.bound_scope_version:
        return RemoteApprovalResult(RemoteApprovalOutcome.SCOPE_VERSION_CHANGED, digest)
    try:
        if decide is not None:
            # Record the human decision on the existing control approval first.
            await decide(binding)
        if binding.decision is RemoteDecision.REJECT:
            return RemoteApprovalResult(RemoteApprovalOutcome.REJECTED, digest)
        result = await commit(
            scope_id=binding.scope_id,
            expected_version=binding.bound_scope_version,
            candidate_ref=binding.candidate_receipt_id,
            manifest_hash=binding.manifest_hash,
            approval_ref=binding.approval_ref,
            actor=binding.actor_id,
            binding=binding,
        )
    except (ValueError, OSError, PermissionError):  # CAS conflict, refusal or store failure: never success
        return RemoteApprovalResult(RemoteApprovalOutcome.COMMIT_CONFLICT, digest)
    status = result.get("status") if isinstance(result, dict) else None
    if status != "COMMITTED":
        # e.g. STALE_CANDIDATE from the CAS boundary: acceptance did not happen.
        return RemoteApprovalResult(RemoteApprovalOutcome.COMMIT_CONFLICT, digest, result)
    return RemoteApprovalResult(RemoteApprovalOutcome.COMMITTED, digest, result)


def pending_from_read(read: Any, *, scope_id: str, approval_ref: str) -> PendingDecision | None:
    """Derive the pending human decision from a ``DurableProductRead``.

    Only a run the product loop itself stopped at HUMAN_REVIEW_REQUIRED, with a
    validated candidate receipt and no semantic commit, is decidable.
    """
    if read.status != "HUMAN_REVIEW_REQUIRED" or read.semantic_commit_status == "COMMITTED":
        return None
    passing = [a for a in read.attempts if a.validation_passed and a.receipt_id]
    if not passing:
        return None
    latest = passing[-1]
    if latest.input_scope_version is None or not latest.input_manifest_hash:
        return None
    return PendingDecision(
        approval_ref=approval_ref,
        run_id=read.run_id,
        candidate_receipt_id=str(latest.receipt_id),
        scope_id=scope_id,
        scope_version=int(latest.input_scope_version),
        manifest_hash=str(latest.input_manifest_hash),
        semantic_state="ACCEPTED_PENDING_HUMAN_DECISION",
    )



def _completion_times(read: Any, store: Any) -> dict[int, str]:
    """Attempt completion times from the persisted, hash-verified ProductLoopReport object."""
    ref = getattr(read, "report_object_ref", None)
    if store is None or not ref:
        return {}
    from pados.product_loop.schemas import ProductLoopReport
    from pados.state_proof.object_store import ObjectRef

    try:
        report = ProductLoopReport.model_validate_json(store.read(ObjectRef(**ref)))
    except (OSError, ValueError, TypeError):
        return {}
    return {a.attempt: a.runtime_result.completed_at for a in report.attempts}


def artifacts_of(read: Any, store: Any = None) -> list[dict[str, Any]]:
    """Original artifact/receipt identities from the durable read model (never regenerated)."""
    completed = _completion_times(read, store)
    return [
        {"receipt_id": a.receipt_id, "output_content_hash": a.output_content_hash,
         "input_manifest_hash": a.input_manifest_hash, "input_scope_version": a.input_scope_version,
         "changed_paths": list(a.changed_paths), "attempt": a.attempt,
         "completed_at": completed.get(a.attempt) or read.finished_at}
        for a in read.attempts if a.receipt_id
    ]
