# SPDX-License-Identifier: AGPL-3.0-only
"""Live authority adapters for Remote Operations.

Binds the remote service to existing PADOS authority only:

* START_TASK / UPLOAD_AND_INGEST -> ``control_work_requests`` (existing
  capture path; the worker router and product loop take it from there);
* run projections -> the product-loop durable read model;
* human decisions -> existing ``control_approvals`` rows whose
  ``proposal_hash`` pins the exact candidate + ScopeVersion presented;
* acceptance -> ``StateProofService.commit_result`` (CAS on
  ``expected_version``), never a direct write;
* verified work-protocol ResultCandidates (``pados.loop.acceptance``) are
  presented the same way and accepted only through ResultReceipt -> the same
  CAS, after a consumed fresh step-up proof.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.control.schemas import MAX_WORK_REQUEST_BODY_CHARS
from pados.execution.context import canonical_hash
from pados.enterprise.classification import DataClassification, parse_classification
from pados.loop import acceptance as result_acceptance
from pados.product_loop.state import P6_19BStateGateway
from pados.remote.approval import (
    PendingDecision,
    RemoteApprovalBinding,
    artifacts_of,
    pending_from_read,
)
from pados.remote.context import ReconstructedContext
from pados.remote.intent import RemoteIntentEnvelope
from pados.work_protocol.intake import capture_task_envelope, project_create_task_intent
from pados.work_protocol.compatibility import parse_protocol
from pados.work_protocol.models import RemoteIntent, TaskEnvelope, TaskPriority

REMOTE_ACTOR_PREFIX = "REMOTE:"
CANDIDATE_PREFIX = "candidate:"
_MAX_RUNS = 50
_MAX_CAPTURED_TASK_BODY_CHARS = MAX_WORK_REQUEST_BODY_CHARS
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _presentation_payload(pending: PendingDecision) -> dict[str, Any]:
    return {
        "run_id": pending.run_id,
        "candidate_receipt_id": pending.candidate_receipt_id,
        "scope_id": pending.scope_id,
        "scope_version": pending.scope_version,
        "manifest_hash": pending.manifest_hash,
    }


class LiveRemotePorts:
    def __init__(
        self,
        *,
        gateway: P6_19BStateGateway,
        operational_engine: Engine,
        presentation_dir: Path,
    ) -> None:
        self.gateway = gateway
        self.operational_engine = operational_engine
        self.presentation_dir = Path(presentation_dir)
        self.presentation_dir.mkdir(parents=True, exist_ok=True)

    # -- intake -------------------------------------------------------------
    async def submit_task(
        self, envelope: RemoteIntentEnvelope, context: ReconstructedContext
    ) -> str:
        classification = self._project_classification(envelope.project_id)
        task_envelope = task_envelope_for_intent(envelope, context, classification)
        pados_intent = project_create_task_intent(intent_id=envelope.intent_id, task=task_envelope)
        body = {
            "remote_intent": envelope.to_dict(),
            "pados_intent": pados_intent.model_dump(mode="json"),
            "context_fingerprint": context.context_fingerprint,
            "context_scope_version": context.context.scope_version,
            "task_envelope": task_envelope.model_dump(mode="json"),
            "task_envelope_hash": task_envelope.content_hash,
        }
        serialized_body = json.dumps(body, sort_keys=True, ensure_ascii=False)
        if len(serialized_body) > _MAX_CAPTURED_TASK_BODY_CHARS:
            raise ValueError("TASK_ENVELOPE_STORAGE_SIZE_EXCEEDED")
        with Session(self.operational_engine) as session, session.begin():
            row = control_service.create_work_request(
                session,
                {
                    "project_id": envelope.project_id,
                    "title": (envelope.human_message or envelope.intent_type.value)[:300],
                    "body": serialized_body,
                    "idempotency_key": f"remote:{envelope.intent_id}"[:120],
                },
            )
        return str(row["id"])

    def _project_classification(self, project_id: str) -> DataClassification:
        with Session(self.operational_engine) as session:
            value = session.execute(
                text("SELECT classification FROM projects WHERE id::text = :project_id"),
                {"project_id": project_id},
            ).scalar_one_or_none()
        return parse_classification(value)


    # -- projections --------------------------------------------------------
    def _summary(self, run_id: str, scope_id: str) -> dict[str, Any] | None:
        read = self.gateway.read_run(run_id)
        if read is None:
            return None
        row: dict[str, Any] = {
            "run_id": read.run_id,
            "project_id": scope_id,
            "goal": read.goal,
            "status": read.status,
            "progress_status": read.progress_status.value,
            "semantic_commit_status": read.semantic_commit_status,
            "human_action_required": read.human_action_required,
            "started_at": read.started_at,
            "finished_at": read.finished_at,
            "artifacts": artifacts_of(read, self.gateway.proof_service.object_store),
        }
        pending = pending_from_read(read, scope_id=scope_id, approval_ref="unassigned")
        if pending is not None:
            row["approval_ref"] = self._present(pending)
        return row

    def _candidate_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        candidate = json.loads(row["candidate_json"]) if isinstance(row["candidate_json"], str) else row["candidate_json"]
        commit = None
        if row["decision"] == "APPROVE":
            with self.gateway.proof_service._session() as session:
                commit = session.execute(
                    text(
                        "SELECT c.status, c.new_version, r.id::text AS receipt_id FROM state_proof_results r "
                        "JOIN state_proof_commits c ON c.result_id = r.id "
                        "WHERE r.operation_id = :op AND r.attempt = 1"
                    ),
                    {"op": result_acceptance.acceptance_operation_id(str(row["candidate_id"]))},
                ).mappings().first()
        pending = row["verification_status"] == "PASSED" and row["decision"] is None
        committed = commit is not None and commit["status"] == "COMMITTED"
        if pending:
            status, semantic, progress = "HUMAN_REVIEW_REQUIRED", "ACCEPTED_PENDING_HUMAN_DECISION", "VERIFIED"
        elif committed:
            status, semantic, progress = "ACCEPTED", "COMMITTED", "ACCEPTED"
        elif row["decision"] == "REJECT":
            status, semantic, progress = "REJECTED", "NOT_COMMITTED", "BLOCKED"
        elif row["verification_status"] == "FAILED":
            status, semantic, progress = "VERIFICATION_FAILED", "NOT_COMMITTED", "FAILED"
        else:
            status, semantic, progress = "APPROVED_NOT_COMMITTED", str(commit["status"]) if commit else "NOT_COMMITTED", "STALE"
        return {
            "run_id": str(row["candidate_id"]),
            "kind": "RESULT_CANDIDATE",
            "project_id": str(row["project_ref"]),
            "task_id": str(row["task_id"]),
            "goal": f"Result for {row['task_id']}",
            "status": status,
            "progress_status": progress,
            "semantic_commit_status": semantic,
            "human_action_required": pending,
            "started_at": str(row["created_at"]),
            "finished_at": None if pending else str(row["decided_at"] or row["created_at"]),
            "approval_ref": row["approval_ref"] if pending else None,
            "artifacts": [
                {"receipt_id": None if commit is None else commit["receipt_id"],
                 "candidate_digest": str(row["candidate_digest"]),
                 "verification_digest": str(row["verification_digest"]),
                 "sections": len(candidate.get("sections", []))}
            ],
        }

    def _candidate_rows(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        with Session(self.operational_engine) as session:
            ids = session.execute(
                text(
                    "SELECT candidate_id FROM result_acceptance_candidates "
                    "WHERE project_id::text = ANY(:ids) ORDER BY created_at DESC LIMIT :limit"
                ),
                {"ids": list(project_ids), "limit": _MAX_RUNS},
            ).scalars().all()
            rows = [result_acceptance.candidate_row(session, candidate_id=str(item)) for item in ids]
        return [self._candidate_summary(row) for row in rows if row is not None]

    def run_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not project_ids:
            return []
        candidates = self._candidate_rows(project_ids)
        with self.gateway.proof_service._session() as session:
            rows = session.execute(
                text(
                    "SELECT run_id::text AS run_id, scope_id FROM p6_21h_product_read_index "
                    "WHERE scope_id = ANY(:ids) ORDER BY updated_at DESC LIMIT :limit"
                ),
                {"ids": list(project_ids), "limit": _MAX_RUNS},
            ).mappings().all()
        out = [self._summary(str(r["run_id"]), str(r["scope_id"])) for r in rows]
        return candidates + [row for row in out if row is not None]

    def run_summary(self, run_id: str) -> dict[str, Any] | None:
        if run_id.startswith(CANDIDATE_PREFIX):
            with Session(self.operational_engine) as session:
                row = result_acceptance.candidate_row(session, candidate_id=run_id)
            return None if row is None else self._candidate_summary(row)
        scope = self.gateway.read_run_scope(run_id)
        return None if scope is None else self._summary(run_id, scope)

    def task_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not project_ids:
            return []
        with Session(self.operational_engine) as session:
            rows = session.execute(
                text(
                    "SELECT id::text AS request_id, project_id::text AS project_id, body, status, "
                    "created_at::text AS created_at FROM control_work_requests "
                    "WHERE project_id::text = ANY(:project_ids) "
                    "AND body LIKE :task_marker "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"project_ids": list(project_ids), "task_marker": '%"task_envelope"%', "limit": _MAX_RUNS * 2},
            ).mappings().all()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            try:
                body = json.loads(str(row["body"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(body, dict) or not isinstance(body.get("remote_intent"), dict):
                continue
            try:
                raw_task = body["task_envelope"]
                task = TaskEnvelope.model_validate(raw_task)
                pados_intent = parse_protocol(
                    body["pados_intent"], RemoteIntent
                )
                expected_intent = project_create_task_intent(
                    intent_id=str(body["remote_intent"]["intent_id"]), task=task
                )
                if any(
                    getattr(pados_intent, field) != getattr(expected_intent, field)
                    for field in (
                        "intent_id", "project_ref", "intent_type", "task_id", "goal_id",
                        "priority", "target_scope_version_ref",
                    )
                ):
                    raise ValueError("TASK_ENVELOPE_INTENT_BINDING_MISMATCH")
                if task.content_hash != body.get("task_envelope_hash"):
                    raise ValueError("TASK_ENVELOPE_HASH_MISMATCH")
                if task.project_ref != str(row["project_id"]):
                    raise ValueError("TASK_ENVELOPE_PROJECT_MISMATCH")
            except KeyError as exc:
                raise ValueError("TASK_ENVELOPE_MISSING") from exc
            summaries.append(
                {
                    "request_id": str(row["request_id"]),
                    "task_id": task.task_id,
                    "project_id": task.project_ref,
                    "original_intent": task.original_intent,
                    "classification": task.classification.value,
                    "priority": task.priority.value,
                    "deadline": None if task.deadline is None else task.deadline.isoformat(),
                    "scope_version_ref": task.scope_version_ref,
                    "decomposition_status": task.decomposition_status.value,
                    "work_request_status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                    "goals": [
                        {
                            "goal_id": goal.goal_id,
                            "title": goal.title,
                            "objective": goal.objective,
                            "state": goal.state.value,
                            "required_capabilities": list(goal.required_capabilities),
                            "dependencies": list(goal.dependencies),
                        }
                        for goal in task.normalized_goals
                    ],
                    "assignments": [],
                    "artifacts": [],
                    "evidence": [],
                    "receipts": [],
                }
            )
        return summaries

    def run_graph(self, run_id: str) -> dict[str, Any] | None:
        """Rebuild the full lineage projection for a result candidate from authority."""
        if not run_id.startswith(CANDIDATE_PREFIX):
            return None
        with Session(self.operational_engine) as session:
            row = result_acceptance.candidate_row(session, candidate_id=run_id)
            if row is None:
                return None
            graph = result_acceptance.rebuild_task_work_graph(
                session, self.gateway.proof_service, str(row["loop_ref"])
            )
        return {"project_id": str(row["project_ref"]), **graph.model_dump(mode="json")}

    # -- decisions ----------------------------------------------------------
    def _present(self, pending: PendingDecision) -> str:
        """Create (idempotently) the control approval that pins this candidate."""
        payload = _presentation_payload(pending)
        digest = canonical_hash(payload)
        with Session(self.operational_engine) as session, session.begin():
            row = control_service.create_approval(
                session,
                {
                    "project_id": pending.scope_id,
                    "proposal_hash": digest,
                    "proposal_summary": f"Remote decision for run {pending.run_id}",
                    "idempotency_key": f"remote-decision:{digest[:64]}",
                },
            )
        approval_ref = str(row["id"])
        (self.presentation_dir / f"{approval_ref}.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        return approval_ref

    def _approval_row(self, approval_ref: str) -> dict[str, Any] | None:
        with Session(self.operational_engine) as session:
            row = session.execute(
                text(
                    "SELECT id::text AS id, project_id::text AS project_id, proposal_hash, status, version "
                    "FROM control_approvals WHERE id::text = :id"
                ),
                {"id": approval_ref},
            ).mappings().first()
        return dict(row) if row is not None else None

    def _candidate_decision(self, row: dict[str, Any]) -> PendingDecision:
        return PendingDecision(
            approval_ref=str(row["approval_ref"]),
            run_id=str(row["candidate_id"]),
            candidate_receipt_id=str(row["candidate_id"]),
            scope_id=str(row["project_ref"]),
            scope_version=int(row["expected_scope_version"]),
            manifest_hash=str(row["candidate_digest"]),
            semantic_state="ACCEPTED_PENDING_HUMAN_DECISION",
            classification=str(row["classification"]),
            kind="RESULT_CANDIDATE",
            task_id=str(row["task_id"]),
        )

    def presented_decision(self, approval_ref: str) -> PendingDecision | None:
        path = self.presentation_dir / f"{approval_ref}.json"
        row = self._approval_row(approval_ref)
        if row is not None and not path.is_file():
            with Session(self.operational_engine) as session:
                candidate = result_acceptance.pending_candidate(session, approval_id=approval_ref)
            return None if candidate is None else self._candidate_decision(candidate)
        if row is None or not path.is_file() or path.parent != self.presentation_dir:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if canonical_hash(payload) != str(row["proposal_hash"]).lower():
            return None  # presentation tampered or does not match the approval
        if row["status"] != "PENDING" or row["project_id"] != payload["scope_id"]:
            return None
        return PendingDecision(
            approval_ref=approval_ref,
            run_id=payload["run_id"],
            candidate_receipt_id=payload["candidate_receipt_id"],
            scope_id=payload["scope_id"],
            scope_version=int(payload["scope_version"]),
            manifest_hash=payload["manifest_hash"],
            semantic_state="ACCEPTED_PENDING_HUMAN_DECISION",
            classification=self._project_classification(payload["scope_id"]).value,
            task_id=payload["run_id"],
        )

    def current_decision(self, run_id: str) -> PendingDecision | None:
        if run_id.startswith(CANDIDATE_PREFIX):
            with Session(self.operational_engine) as session:
                row = result_acceptance.candidate_row(session, candidate_id=run_id)
                if row is None:
                    return None
                # Recompose from the sealed results now: a changed candidate is not decidable.
                # ScopeVersion staleness is decided separately against the live scope head.
                current = result_acceptance.pending_candidate(
                    session, candidate_id=run_id, reverify=True,
                    current_scope_version=int(row["expected_scope_version"]),
                )
            return None if current is None else self._candidate_decision(current)
        scope = self.gateway.read_run_scope(run_id)
        read = self.gateway.read_run(run_id) if scope else None
        if read is None or scope is None:
            return None
        pending = pending_from_read(read, scope_id=scope, approval_ref="current")
        if pending is None:
            return None
        return PendingDecision(
            approval_ref=self._present(pending),
            run_id=pending.run_id,
            candidate_receipt_id=pending.candidate_receipt_id,
            scope_id=pending.scope_id,
            scope_version=pending.scope_version,
            manifest_hash=pending.manifest_hash,
            semantic_state=pending.semantic_state,
            classification=self._project_classification(scope).value,
            task_id=pending.run_id,
        )

    def scope_version(self, scope_id: str) -> int:
        return int(self.gateway.semantic_context(scope_id)["scope_version"])

    def decide(self) -> Callable[[RemoteApprovalBinding], Awaitable[None]]:
        async def record(binding: RemoteApprovalBinding) -> None:
            row = self._approval_row(binding.approval_ref)
            if row is None:
                raise ValueError("APPROVAL_REF_NOT_FOUND")
            candidate: dict[str, Any] | None = None
            with Session(self.operational_engine) as session, session.begin():
                control_service.decide_approval(
                    session,
                    binding.approval_ref,
                    version=int(row["version"]),
                    status="APPROVED" if binding.decision.value == "APPROVE" else "REJECTED",
                    reason=f"{REMOTE_ACTOR_PREFIX}{binding.actor_id} binding={binding.binding_hash} "
                           f"proof={binding.proof_ref}",
                    idempotency_key=f"remote-{binding.binding_hash[:48]}",
                )
                if binding.kind == "RESULT_CANDIDATE":
                    # Same transaction: one terminal decision per candidate, bound to the consumed proof.
                    result_acceptance.record_candidate_decision(
                        session,
                        candidate_id=binding.candidate_receipt_id,
                        decision=binding.decision.value,
                        actor_id=binding.actor_id,
                        session_id=binding.session_id,
                        proof_id=binding.proof_ref.removeprefix("step-up-proof:"),
                        binding_hash=binding.binding_hash,
                        step_up_binding_digest=binding.step_up_binding_digest,
                        next_action="REQUEST_REPLAN" if binding.decision.value == "REJECT" else None,
                    )
                    candidate = result_acceptance.candidate_row(session, candidate_id=binding.candidate_receipt_id)
            if candidate is not None and binding.decision.value == "REJECT":
                result_acceptance.reject_result_candidate(
                    proof_service=self.gateway.proof_service,
                    row=candidate,
                    actor_id=binding.actor_id,
                    proof_ref=binding.proof_ref,
                    binding_hash=binding.binding_hash,
                )

        return record

    def commit(self) -> Callable[..., Awaitable[Any]]:
        async def cas_commit(
            *,
            scope_id: str,
            expected_version: int,
            candidate_ref: str,
            manifest_hash: str,
            approval_ref: str,
            actor: str,
            binding: RemoteApprovalBinding | None = None,
        ) -> dict[str, Any]:
            if binding is not None and binding.kind == "RESULT_CANDIDATE":
                return await result_acceptance.accept_result_candidate(
                    operational_engine=self.operational_engine,
                    proof_service=self.gateway.proof_service,
                    candidate_id=candidate_ref,
                    expected_version=expected_version,
                    actor_id=actor,
                )
            return self.gateway.proof_service.commit_result(
                result_id=candidate_ref,
                scope_id=scope_id,
                expected_version=expected_version,
                candidate_ref=candidate_ref,
                manifest_hash=manifest_hash,
                idempotency_key=f"remote-commit:{candidate_ref}",
                actor=f"{REMOTE_ACTOR_PREFIX}{actor}",
                reason="remote human approval",
                approval_ref=approval_ref,
            )

        return cas_commit


def task_envelope_for_intent(
    envelope: RemoteIntentEnvelope,
    context: ReconstructedContext,
    classification: DataClassification,
) -> TaskEnvelope:
    try:
        created_at = datetime.fromisoformat(envelope.created_at)
        deadline = None if envelope.deadline is None else datetime.fromisoformat(envelope.deadline)
    except (TypeError, ValueError) as exc:
        raise ValueError("REMOTE_TASK_TIMESTAMP_INVALID") from exc
    capability_hint = None
    if envelope.requested_capability is not None:
        capability_hint = envelope.requested_capability.upper()
        if not _CAPABILITY_ID.fullmatch(capability_hint):
            raise ValueError("REMOTE_CAPABILITY_HINT_INVALID")
    task_id = "task:" + canonical_hash(
        {"project_id": envelope.project_id, "intent_id": envelope.intent_id}
    )[:40]
    return capture_task_envelope(
        task_id=task_id,
            original_intent=envelope.human_message or envelope.intent_type.value,
        project_ref=envelope.project_id,
        classification=classification,
        priority=TaskPriority(envelope.priority.value),
        deadline=deadline,
        scope_version_ref=f"scope:{context.context.scope_version}",
        created_by=envelope.actor_id,
        created_at=created_at,
        capability_hint=capability_hint,
    )
