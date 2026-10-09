# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic in-memory ports for CI and evidence runs.

Mirrors ``LiveRemotePorts`` over ``InMemoryP6_19BStateGateway``: same pending
decision derivation, same presentation hash pinning and a CAS commit that
advances the scope version only when ``expected_version`` matches.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from pados.execution.context import canonical_hash
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.remote.approval import (
    PendingDecision,
    RemoteApprovalBinding,
    artifacts_of,
    pending_from_read,
)
from pados.remote.context import ReconstructedContext
from pados.remote.intent import RemoteIntentEnvelope
from pados.enterprise.classification import DataClassification
from pados.work_protocol.intake import capture_task_envelope, project_create_task_intent
from pados.work_protocol.models import TaskEnvelope


class InMemoryRemotePorts:
    def __init__(self, gateway: InMemoryP6_19BStateGateway, *, classification: str = "CONFIDENTIAL") -> None:
        self.gateway = gateway
        self.classification = classification
        self.work_requests: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.commit_calls: list[dict[str, Any]] = []

    async def submit_task(self, envelope: RemoteIntentEnvelope, context: ReconstructedContext) -> str:
        key = f"remote:{envelope.intent_id}"
        for request_id, row in self.work_requests.items():
            if row["idempotency_key"] == key:
                return request_id
        request_id = str(uuid.uuid4())
        self.work_requests[request_id] = {
            "idempotency_key": key,
            "project_id": envelope.project_id,
            "status": "PENDING_WORKER_ROUTER",
            "remote_intent": envelope.to_dict(),
            "context_fingerprint": context.context_fingerprint,
        }
        task = capture_task_envelope(
            task_id="task:" + canonical_hash({"project_id": envelope.project_id, "intent_id": envelope.intent_id})[:40],
            original_intent=envelope.human_message or envelope.intent_type.value,
            project_ref=envelope.project_id,
            classification=DataClassification.INTERNAL,
            priority=envelope.priority.value,
            deadline=None if envelope.deadline is None else datetime.fromisoformat(envelope.deadline),
            scope_version_ref=f"scope:{context.context.scope_version}",
            created_by=envelope.actor_id,
            created_at=datetime.fromisoformat(envelope.created_at),
            capability_hint=None if envelope.requested_capability is None else envelope.requested_capability.upper(),
        )
        self.work_requests[request_id]["task_envelope"] = task.model_dump(mode="json")
        self.work_requests[request_id]["task_envelope_hash"] = task.content_hash
        self.work_requests[request_id]["pados_intent"] = project_create_task_intent(
            intent_id=envelope.intent_id, task=task
        ).model_dump(mode="json")
        return request_id

    def task_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for request_id, row in self.work_requests.items():
            if row["project_id"] not in project_ids or "task_envelope" not in row:
                continue
            task = TaskEnvelope.model_validate(row["task_envelope"])
            if task.content_hash != row["task_envelope_hash"]:
                raise ValueError("TASK_ENVELOPE_HASH_MISMATCH")
            summaries.append(
                {
                    "request_id": request_id,
                    "task_id": task.task_id,
                    "project_id": task.project_ref,
                    "original_intent": task.original_intent,
                    "classification": task.classification.value,
                    "priority": task.priority.value,
                    "deadline": None if task.deadline is None else task.deadline.isoformat(),
                    "scope_version_ref": task.scope_version_ref,
                    "decomposition_status": task.decomposition_status.value,
                    "work_request_status": row["status"],
                    "created_at": task.created_at.isoformat(),
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

    def _present(self, pending: PendingDecision) -> str:
        payload = {
            "run_id": pending.run_id,
            "candidate_receipt_id": pending.candidate_receipt_id,
            "scope_id": pending.scope_id,
            "scope_version": pending.scope_version,
            "manifest_hash": pending.manifest_hash,
        }
        digest = canonical_hash(payload)
        for ref, row in self.approvals.items():
            if row["proposal_hash"] == digest:
                return ref
        ref = str(uuid.uuid4())
        self.approvals[ref] = {"proposal_hash": digest, "payload": payload, "status": "PENDING"}
        return ref

    def _summary(self, run_id: str) -> dict[str, Any] | None:
        scope = self.gateway.read_run_scope(run_id)
        read = self.gateway.read_run(run_id)
        if scope is None or read is None:
            return None
        row: dict[str, Any] = {
            "run_id": read.run_id,
            "project_id": scope,
            "goal": read.goal,
            "status": read.status,
            "progress_status": read.progress_status.value,
            "semantic_commit_status": read.semantic_commit_status,
            "human_action_required": read.human_action_required,
            "started_at": read.started_at,
            "finished_at": read.finished_at,
            "artifacts": artifacts_of(read, self.gateway.object_store),
        }
        if self._committed(run_id):
            row["semantic_commit_status"] = "COMMITTED"
        pending = pending_from_read(read, scope_id=scope, approval_ref="unassigned")
        if pending is not None and not self._committed(run_id):
            row["approval_ref"] = self._present(pending)
        return row

    def _committed(self, run_id: str) -> bool:
        return any(c["run_id"] == run_id and c["status"] == "COMMITTED" for c in self.commit_calls)

    def run_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        rows = [self._summary(run_id) for run_id in self.gateway.read_index]
        return [r for r in rows if r is not None and r["project_id"] in project_ids]

    def run_summary(self, run_id: str) -> dict[str, Any] | None:
        return self._summary(run_id)

    def run_graph(self, run_id: str) -> dict[str, Any] | None:
        del run_id
        return None

    def presented_decision(self, approval_ref: str) -> PendingDecision | None:
        row = self.approvals.get(approval_ref)
        if row is None or row["status"] != "PENDING" or canonical_hash(row["payload"]) != row["proposal_hash"]:
            return None
        p = row["payload"]
        return PendingDecision(
            approval_ref=approval_ref,
            run_id=p["run_id"],
            candidate_receipt_id=p["candidate_receipt_id"],
            scope_id=p["scope_id"],
            scope_version=p["scope_version"],
            manifest_hash=p["manifest_hash"],
            semantic_state="ACCEPTED_PENDING_HUMAN_DECISION",
            classification=self.classification,
            task_id=p["run_id"],
        )

    def current_decision(self, run_id: str) -> PendingDecision | None:
        scope = self.gateway.read_run_scope(run_id)
        read = self.gateway.read_run(run_id)
        if scope is None or read is None or self._committed(run_id):
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
            classification=self.classification,
            task_id=pending.run_id,
        )

    def scope_version(self, scope_id: str) -> int:
        return int(self.gateway.semantic_context(scope_id)["scope_version"])

    def decide(self) -> Callable[[RemoteApprovalBinding], Awaitable[None]]:
        async def record(binding: RemoteApprovalBinding) -> None:
            row = self.approvals[binding.approval_ref]
            if row["status"] != "PENDING":
                raise ValueError("approval is not pending")
            row["status"] = "APPROVED" if binding.decision.value == "APPROVE" else "REJECTED"
            row["decided_by"] = binding.actor_id
            row["binding_hash"] = binding.binding_hash
            row["proof_ref"] = binding.proof_ref

        return record

    def commit(self) -> Callable[..., Awaitable[Any]]:
        async def cas_commit(
            *, scope_id: str, expected_version: int, candidate_ref: str,
            manifest_hash: str, approval_ref: str, actor: str, binding: Any = None,
        ) -> dict[str, Any]:
            current = self.gateway.versions.get(scope_id, 0)
            run_id = self.approvals[approval_ref]["payload"]["run_id"]
            if current != expected_version:
                result: dict[str, Any] = {"status": "STALE_CANDIDATE", "new_version": None}
            else:
                self.gateway.versions[scope_id] = current + 1
                result = {"status": "COMMITTED", "new_version": current + 1}
            self.commit_calls.append(
                {"run_id": run_id, "scope_id": scope_id, "expected_version": expected_version,
                 "candidate_ref": candidate_ref, "manifest_hash": manifest_hash,
                 "approval_ref": approval_ref, "actor": actor,
                 "proof_ref": None if binding is None else binding.proof_ref, **result}
            )
            return result

        return cas_commit
