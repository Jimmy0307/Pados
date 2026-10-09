# SPDX-License-Identifier: AGPL-3.0-only
"""Remote Operations service: dispatch intents onto existing PADOS authority.

Every remote intent is authenticated, project-authorized, recorded in the
intent ledger and then handed to an existing PADOS path through a narrow port.
The service owns no semantic state and has no shell, SQL or model passthrough.
An intent_id is dispatched at most once, across processes: the id is claimed
in the ledger (actor + request fingerprint) before dispatch. A completed id
replays its ledgered outcome; an id claimed by another dispatch is refused
(``INTENT_IN_FLIGHT``), and a claim left without outcome by a crash past its
lease is reported (``INTENT_CLAIM_UNRESOLVED``) and never re-dispatched
automatically. Ledger
entries pass the shared minimization guard; a status read is ledgered as run
ids plus a digest, never the projection rows (goal text) themselves.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized
from pados.remote.approval import (
    PendingDecision,
    RemoteApprovalBinding,
    RemoteApprovalOutcome,
    apply_remote_decision,
    bind_remote_decision,
)
from pados.remote.auth import RemoteActorRegistry, RemoteSession, authorize_project, may_approve
from pados.remote.context import CONTEXT_SUMMARY_KEYS, ReconstructedContext, RemoteContextReconstructor
from pados.remote.intent import (
    RemoteIntentEnvelope,
    RemoteIntentError,
    RemoteIntentType,
    parse_client_intent,
)
from pados.remote.jobs import ControlAction, RemoteJobController
from pados.remote.ledger import IntentLedger
from pados.remote.staging import StagingArea, StagingState
from pados.remote.step_up import StepUpAction, StepUpBinding, StepUpError, StepUpService


class RemoteAuthorityPorts(Protocol):
    """Narrow adapters onto existing PADOS authority (product loop, CAS, approvals)."""

    async def submit_task(
        self, envelope: RemoteIntentEnvelope, context: ReconstructedContext
    ) -> str: ...
    def run_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]: ...
    def task_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]: ...
    def run_summary(self, run_id: str) -> dict[str, Any] | None: ...
    def run_graph(self, run_id: str) -> dict[str, Any] | None: ...
    def presented_decision(self, approval_ref: str) -> PendingDecision | None: ...
    def current_decision(self, run_id: str) -> PendingDecision | None: ...
    def scope_version(self, scope_id: str) -> int: ...
    def commit(self) -> Callable[..., Awaitable[Any]]: ...
    def decide(self) -> Callable[[RemoteApprovalBinding], Awaitable[None]] | None: ...


# Why a claim refused to dispatch (the replay case returns the original outcome instead).
_CLAIM_REFUSALS = {"IN_FLIGHT": "INTENT_IN_FLIGHT", "UNRESOLVED": "INTENT_CLAIM_UNRESOLVED",
                   "REUSED": "INTENT_ID_REUSED"}

FAILED_PROGRESS = frozenset({"FAILED", "BLOCKED", "STALE", "INCOMPLETE", "ITERATION_LIMIT"})
DONE_PROGRESS = frozenset({"ACCEPTED", "VERIFIED"})


def bucket_for(summary: Mapping[str, Any]) -> str:
    if summary.get("semantic_commit_status") != "COMMITTED" and (
        summary.get("status") == "HUMAN_REVIEW_REQUIRED"
        or summary.get("semantic_commit_status") == "ACCEPTED_PENDING_HUMAN_DECISION"
    ):
        return "WAITING_APPROVAL"
    progress = str(summary.get("progress_status", ""))
    if progress in FAILED_PROGRESS:
        return "FAILED"
    if progress in DONE_PROGRESS and summary.get("finished_at"):
        return "COMPLETED"
    return "RUNNING"


_SUMMARY_KEYS = (
    "run_id",
    "project_id",
    "goal",
    "status",
    "progress_status",
    "semantic_commit_status",
    "human_action_required",
    "started_at",
    "finished_at",
    "approval_ref",
)


def compact(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Mobile projection: bounded fields only, never context or payloads."""
    row = {key: summary.get(key) for key in _SUMMARY_KEYS}
    goal = row.get("goal")
    if isinstance(goal, str) and len(goal) > 200:
        row["goal"] = goal[:197] + "..."
    row["bucket"] = bucket_for(summary)
    return row


def _ledger_view(body: Mapping[str, Any]) -> dict[str, Any]:
    """What the intent ledger keeps: a status read records which runs were read, not their rows.

    READ outcomes are never replayed (QUERY_STATUS skips the replay guard), so the client
    response may carry rows the ledger does not.
    """
    view = {key: value for key, value in body.items() if key not in {"runs", "run"}}
    if "runs" in body:
        view["runs_read"] = {"run_ids": [row.get("run_id") for row in body["runs"]],
                             "digest": canonical_hash(body["runs"])}
    if "run" in body:
        view["run_read"] = {"run_id": body["run"].get("run_id"), "digest": canonical_hash(body["run"])}
    return view


@dataclass
class RemoteOperationsService:
    registry: RemoteActorRegistry
    reconstructor: RemoteContextReconstructor
    staging: StagingArea
    jobs: RemoteJobController
    ledger: IntentLedger
    ports: RemoteAuthorityPorts
    # Fresh human-presence step-up; absent means every decision fails closed.
    step_up: StepUpService | None = None
    _in_flight: set[str] = field(default_factory=set, init=False, repr=False)

    def _record(self, envelope: RemoteIntentEnvelope, outcome: str, **extra: Any) -> dict[str, Any]:
        body = {
            "intent_id": envelope.intent_id,
            "intent_fingerprint": envelope.fingerprint,
            "request_fingerprint": envelope.request_fingerprint,
            "intent_type": envelope.intent_type.value,
            "project_id": envelope.project_id,
            "actor_id": envelope.actor_id,
            "session_id": envelope.session_id,
            "outcome": outcome,
            **extra,
        }
        entry = self._append(_ledger_view(body))
        return {**body, "ledger_entry_hash": entry["entry_hash"]}

    def _append(self, record: dict[str, Any]) -> dict[str, Any]:
        # ``context`` is the fingerprint summary (ReconstructedContext.summary), never context itself:
        # it is checked against exactly that schema, and the rest of the record on its own.
        summary = record.get("context")
        if summary is not None:
            if not isinstance(summary, dict):
                raise ValueError("LEDGER_CONTEXT_SUMMARY_INVALID")
            assert_minimized(summary, allowed_keys=CONTEXT_SUMMARY_KEYS)
        assert_minimized({key: value for key, value in record.items() if key != "context"})
        return self.ledger.append(record)

    def _check_attachments(self, envelope: RemoteIntentEnvelope) -> None:
        for ref in envelope.attachment_refs:
            record = self.staging.get(ref.removeprefix("staged:"))
            if record is None:
                raise RemoteIntentError("ATTACHMENT_NOT_FOUND")
            if record["project_id"] != envelope.project_id:
                raise RemoteIntentError("ATTACHMENT_PROJECT_MISMATCH")
            if record["state"] != StagingState.STAGED.value:
                raise RemoteIntentError("ATTACHMENT_QUARANTINED")

    def _owned_run(self, envelope: RemoteIntentEnvelope) -> dict[str, Any]:
        run = self.ports.run_summary(str(envelope.target_run_id))
        if run is None or run.get("project_id") != envelope.project_id:
            raise RemoteIntentError("TARGET_RUN_NOT_FOUND")
        return run

    async def submit(self, session: RemoteSession, payload: Mapping[str, Any]) -> dict[str, Any]:
        envelope = parse_client_intent(
            payload, actor_id=session.actor.actor_id, session_id=session.session_id
        )
        authorize_project(session, envelope.project_id)
        if envelope.intent_type is RemoteIntentType.QUERY_STATUS:
            return await self._dispatch(session, envelope, [])
        prior = self.ledger.find(envelope.intent_id)
        if prior is not None:
            return self._replay(envelope, prior)
        if envelope.intent_id in self._in_flight:
            raise RemoteIntentError("INTENT_IN_FLIGHT")
        # Atomic, persisted claim under the ledger lock; the lock is not held across the dispatch.
        claim = self.ledger.claim(intent_id=envelope.intent_id, actor_id=envelope.actor_id,
                                  request_fingerprint=envelope.request_fingerprint)
        if claim.status == "OUTCOME" and claim.record is not None:
            return self._replay(envelope, claim.record)
        if claim.status != "CLAIMED" or claim.claim_id is None:
            raise RemoteIntentError(_CLAIM_REFUSALS[claim.status])
        effects: list[str] = []
        self._in_flight.add(envelope.intent_id)
        try:
            return await self._dispatch(session, envelope, effects)
        except BaseException:
            if not effects:  # refused before any authority port was called: the id is reusable
                self.ledger.release(intent_id=envelope.intent_id, claim_id=claim.claim_id,
                                    reason="REFUSED_BEFORE_DISPATCH")
            raise
        finally:
            self._in_flight.discard(envelope.intent_id)

    async def _dispatch(
        self, session: RemoteSession, envelope: RemoteIntentEnvelope, effects: list[str]
    ) -> dict[str, Any]:
        """``effects`` is marked right before the first call that may change state outside PADOS'
        own validation; after that point an interrupted dispatch keeps its claim (at most once)."""
        kind = envelope.intent_type
        if kind in {RemoteIntentType.START_TASK, RemoteIntentType.UPLOAD_AND_INGEST}:
            self._check_attachments(envelope)
            context = self.reconstructor.reconstruct(envelope)
            effects.append("submit_task")
            try:
                run_id = await self.ports.submit_task(envelope, context)
            except ValueError as exc:
                if str(exc) != "TASK_ENVELOPE_STORAGE_SIZE_EXCEEDED":
                    raise
                effects.pop()
                return self._record(envelope, "REFUSED", reason=str(exc))
            return self._record(envelope, "SUBMITTED", run_id=run_id, context=context.summary())
        if kind is RemoteIntentType.QUERY_STATUS:
            if envelope.target_run_id is None:
                rows = self.ports.run_summaries([envelope.project_id])
                return self._record(envelope, "READ", runs=[compact(r) for r in rows])
            return self._record(envelope, "READ", run=compact(self._owned_run(envelope)))
        if kind in {RemoteIntentType.APPROVE, RemoteIntentType.REJECT}:
            return await self._decide(session, envelope, effects)
        if not session.actor.may_control_jobs:
            raise PermissionError("REMOTE_JOB_CONTROL_NOT_ALLOWED")
        self._owned_run(envelope)
        effects.append("job_control")
        record = self.jobs.control(
            run_id=str(envelope.target_run_id),
            action=ControlAction(kind.value),
            actor_id=envelope.actor_id,
        )
        return self._record(envelope, record.state, control=record.to_dict())

    def _replay(self, envelope: RemoteIntentEnvelope, prior: dict[str, Any]) -> dict[str, Any]:
        """Duplicate-execution guard: a retried intent_id never dispatches again.

        Same request -> the original outcome is returned (and the replay is ledgered);
        a different request reusing the id -> INTENT_ID_REUSED (fail closed).
        """
        if prior.get("request_fingerprint") != envelope.request_fingerprint or prior.get("actor_id") != envelope.actor_id:
            raise RemoteIntentError("INTENT_ID_REUSED")
        self._append({"intent_id": envelope.intent_id, "replayed": True,
                      "original_entry_hash": prior["ledger_entry_hash"], "session_id": envelope.session_id})
        return {**prior, "replayed": True}

    def job_detail(self, session: RemoteSession, run_id: str) -> dict[str, Any] | None:
        """Authoritative run detail including the original artifacts and receipts."""
        row = self.ports.run_summary(run_id)
        if row is None or row.get("project_id") not in session.actor.projects:
            return None
        return {**compact(row), "artifacts": list(row.get("artifacts") or [])}

    def _step_up_service(self) -> StepUpService:
        if self.step_up is None:
            raise StepUpError("APPROVAL_STEP_UP_NOT_CONFIGURED")
        return self.step_up

    def _authoritative_decision(self, session: RemoteSession, approval_ref: str) -> PendingDecision:
        """Presented decision re-derived from current authority; every field is server-side."""
        presented = self.ports.presented_decision(approval_ref)
        if presented is None:
            raise RemoteIntentError("APPROVAL_REF_NOT_FOUND")
        authorize_project(session, presented.scope_id)
        current = self.ports.current_decision(presented.run_id)
        if current is None:
            raise RemoteIntentError("TARGET_RUN_NOT_FOUND")
        if current.approval_ref != presented.approval_ref or current.target() != presented.target():
            raise StepUpError("APPROVAL_CANDIDATE_CHANGED")
        return presented

    def _binding(self, session: RemoteSession, action: StepUpAction, decision: PendingDecision) -> StepUpBinding:
        return StepUpBinding(actor_id=session.actor.actor_id, session_id=session.session_id,
                             action=action, target=decision.target())

    def open_session(self, session: RemoteSession) -> dict[str, Any]:
        """Issue a server-bound session (binding context for step-up; never an auth factor)."""
        return self._step_up_service().open_session(session.actor.actor_id)

    def issue_step_up(self, session: RemoteSession, approval_ref: str, action: str) -> dict[str, Any]:
        step_up = self._step_up_service()
        if not session.server_bound:
            raise StepUpError("APPROVAL_STEP_UP_SESSION_REQUIRED")
        try:
            decision_action = StepUpAction(str(action).upper())
        except ValueError as exc:
            raise RemoteIntentError("STEP_UP_ACTION_INVALID") from exc
        decision = self._authoritative_decision(session, approval_ref)
        if not may_approve(self.registry, session.actor.actor_id, decision.scope_id):
            raise PermissionError("REMOTE_APPROVAL_NOT_ALLOWED")
        if self.ports.scope_version(decision.scope_id) != decision.scope_version:
            raise StepUpError("APPROVAL_SCOPE_VERSION_STALE")
        return step_up.issue_challenge(self._binding(session, decision_action, decision),
                                       session.actor.presence_credentials)

    def verify_step_up(
        self, session: RemoteSession, approval_ref: str, challenge_id: str, assertion: Mapping[str, Any]
    ) -> dict[str, Any]:
        step_up = self._step_up_service()
        if not session.server_bound:
            raise StepUpError("APPROVAL_STEP_UP_SESSION_REQUIRED")
        return step_up.verify_presence(
            challenge_id=challenge_id,
            actor_id=session.actor.actor_id,
            session_id=session.session_id,
            approval_ref=approval_ref,
            assertion=assertion,
            credentials=session.actor.presence_credentials,
        )

    def run_graph(self, session: RemoteSession, run_id: str) -> dict[str, Any] | None:
        """Rebuildable lineage projection (never authority) for an authorized project."""
        graph = self.ports.run_graph(run_id)
        if graph is None or graph.get("project_id") not in session.actor.projects:
            return None
        return {**graph, "projection": "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY"}

    async def _decide(
        self, session: RemoteSession, envelope: RemoteIntentEnvelope, effects: list[str]
    ) -> dict[str, Any]:
        first = self.ports.presented_decision(str(envelope.approval_ref))
        if first is None:
            raise RemoteIntentError("APPROVAL_REF_NOT_FOUND")
        if first.scope_id != envelope.project_id or envelope.target_run_id != first.run_id:
            raise RemoteIntentError("APPROVAL_TARGET_MISMATCH")
        if not may_approve(self.registry, session.actor.actor_id, first.scope_id):
            # Ledgered refusal; a non-approver never reaches step-up or any authority port.
            return self._record(envelope, RemoteApprovalOutcome.ACTOR_NOT_AUTHORIZED.value)
        step_up = self._step_up_service()
        if not session.server_bound:
            raise StepUpError("APPROVAL_STEP_UP_SESSION_REQUIRED")
        presented = self._authoritative_decision(session, str(envelope.approval_ref))
        action = StepUpAction(envelope.intent_type.value)
        step_up_binding = self._binding(session, action, presented)
        # Atomic single-use consumption against the binding recomputed from current authority.
        consumed = step_up.consume(envelope.step_up_proof, step_up_binding)
        effects.append("decision")
        binding = bind_remote_decision(
            envelope,
            presented,
            candidate_receipt_id=presented.candidate_receipt_id,
            proof_ref=str(consumed.proof_ref),
            step_up_binding_digest=consumed.binding_digest,
        )
        current = self.ports.current_decision(presented.run_id)
        if current is None:
            raise RemoteIntentError("TARGET_RUN_NOT_FOUND")
        result = await apply_remote_decision(
            binding,
            current=current,
            current_scope_version=self.ports.scope_version(presented.scope_id),
            actor_may_approve=lambda actor, scope: may_approve(self.registry, actor, scope),
            commit=self.ports.commit(),
            decide=self.ports.decide(),
        )
        return self._record(
            envelope,
            result.outcome.value,
            binding_hash=result.binding_hash,
            bound_scope_version=binding.bound_scope_version,
            candidate_receipt_id=binding.candidate_receipt_id,
            step_up_proof_ref=binding.proof_ref,
            step_up_binding_digest=binding.step_up_binding_digest,
            presence_method=consumed.presence_method,
        )

    def attention(self, session: RemoteSession) -> dict[str, Any]:
        rows = [compact(r) for r in self.ports.run_summaries(sorted(session.actor.projects))]
        buckets: dict[str, list[dict[str, Any]]] = {
            name: [] for name in ("WAITING_APPROVAL", "FAILED", "RUNNING", "COMPLETED")
        }
        for row in rows:
            buckets[row["bucket"]].append(row)
        return {
            "needs_attention": buckets["WAITING_APPROVAL"] + buckets["FAILED"],
            "running": buckets["RUNNING"],
            "waiting_approval": buckets["WAITING_APPROVAL"],
            "failed": buckets["FAILED"],
            "completed": buckets["COMPLETED"],
            "projection": "PADOS_READ_MODEL_NOT_AUTHORITY",
        }

    def work_map(self, session: RemoteSession) -> dict[str, Any]:
        """Return authorized task and run projections for the Work Map UI."""
        project_ids = sorted(session.actor.projects)
        return {
            "projects": project_ids,
            "tasks": self.ports.task_summaries(project_ids),
            "runs": [compact(row) for row in self.ports.run_summaries(project_ids)],
            "projection": "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY",
            "semantic_authority": "PADOS_HOME_BASE",
        }
