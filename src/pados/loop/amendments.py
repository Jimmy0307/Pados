# SPDX-License-Identifier: AGPL-3.0-only
"""Governed translation from product residual work to existing P6.10 steps."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.execution.context import canonical_hash
from pados.execution.receipts import execution_receipt
from pados.loop import scope_authority
from pados.loop.runtime_bindings import insert_goal_runtime_binding
from pados.loop.schemas import LoopStatus
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import RuntimeLineageProjection
from pados.work_protocol.models import (
    AmendmentAuthorityDecision,
    ArtifactReference,
    AuthorizationMetadata,
    CapabilityResult,
    CoverageMap,
    CoverageStatus,
    EvidenceReference,
    ExecutionReceiptRef,
    GoalRelationType,
    Provenance,
    ResidualPlanAmendment,
    ResidualTask,
    ResourceKind,
    TaskEnvelope,
    UnresolvedReason,
)
from pados.work_protocol.residual import create_residual_tasks
from pados.worker.schemas import WorkerType


class AmendmentAuthority(StrEnum):
    AUTO_AMENDABLE = "AUTO_AMENDABLE"
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"


@dataclass(frozen=True)
class ApprovedExecutionEnvelope:
    """A bounded view of authority derived from an approved P6.10 plan."""

    task_id: str
    project_ref: str
    classification: str
    scope_version_ref: str
    goal_ids: frozenset[str]
    allowed_capabilities: frozenset[str]
    allowed_paths: frozenset[str]
    allowed_actions: frozenset[str]
    worker_types: frozenset[str]
    network_allowed: bool
    approval_valid: bool
    current_scope_version_ref: str


@dataclass(frozen=True)
class ResidualExecutionRequest:
    task_id: str
    residual_id: str
    goal_id: str
    parent_goal_id: str
    project_ref: str
    classification: str
    scope_version_ref: str
    required_capability: str
    dependencies: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    worker_type: str
    network_required: bool
    fresh_approval_required: bool
    authorization_valid: bool


@dataclass(frozen=True)
class RuntimeGoalBinding:
    task_id: str
    goal_id: str
    plan_id: str
    loop_id: str
    step_id: str
    sequence: int
    status: str


def classify_residual_authority(
    envelope: ApprovedExecutionEnvelope,
    residual: ResidualExecutionRequest,
) -> AmendmentAuthority:
    """Return AUTO only when every residual field stays inside current authority."""
    exact_bindings = (
        residual.task_id == envelope.task_id
        and residual.project_ref == envelope.project_ref
        and residual.classification == envelope.classification
        and residual.scope_version_ref == envelope.scope_version_ref
        and envelope.current_scope_version_ref == envelope.scope_version_ref
        and residual.goal_id in envelope.goal_ids
        and residual.parent_goal_id in envelope.goal_ids
    )
    authorized_execution = (
        envelope.approval_valid
        and residual.authorization_valid
        and residual.required_capability in envelope.allowed_capabilities
        and set(residual.allowed_paths).issubset(envelope.allowed_paths)
        and set(residual.allowed_actions).issubset(envelope.allowed_actions)
        and residual.worker_type in envelope.worker_types
        and (not residual.network_required or envelope.network_allowed)
        and not residual.fresh_approval_required
    )
    if exact_bindings and authorized_execution:
        return AmendmentAuthority.AUTO_AMENDABLE
    return AmendmentAuthority.REAPPROVAL_REQUIRED


def resolve_runtime_dependencies(
    dependencies: Sequence[str],
    bindings: Mapping[str, RuntimeGoalBinding],
    *,
    plan_id: str,
    loop_id: str,
    new_sequence: int,
) -> tuple[int, ...]:
    """Translate opaque product goal IDs to prior same-loop integer step sequences."""
    if new_sequence < 1:
        raise ValueError("RUNTIME_SEQUENCE_INVALID")
    result: set[int] = set()
    for goal_id in dependencies:
        binding = bindings.get(goal_id)
        if binding is None:
            raise ValueError("RUNTIME_BINDING_MISSING")
        if binding.plan_id != plan_id:
            raise ValueError("RUNTIME_BINDING_PLAN_MISMATCH")
        if binding.loop_id != loop_id:
            raise ValueError("RUNTIME_BINDING_LOOP_MISMATCH")
        if binding.sequence < 1 or binding.sequence >= new_sequence:
            raise ValueError("RUNTIME_DEPENDENCY_CYCLE")
        result.add(binding.sequence)
    return tuple(sorted(result))


def residual_amendment_idempotency_key(
    task_id: str, residual_id: str, plan_id: str, goal_id: str | None = None
) -> str:
    """Stable key for one immutable residual under one authorized TaskPlan."""
    payload = "\x00".join((task_id, residual_id, goal_id or "", plan_id)).encode("utf-8")
    return "residual-amendment:" + hashlib.sha256(payload).hexdigest()


def _assert_immutable_amendment_content(existing_hash: str, requested_hash: str) -> None:
    if existing_hash != requested_hash:
        raise ValueError("RESIDUAL_AMENDMENT_IDEMPOTENCY_CONTENT_CONFLICT")


def normalize_p6_10_capability_outcome(
    output_contract: Mapping[str, Any],
    stdout: bytes,
    *,
    execution_succeeded: bool,
    execution_status: str | None = None,
) -> CoverageStatus:
    """Normalize only the declared PADOS outcome field; native success is not semantic completion."""
    if execution_status == "BLOCKED":
        return CoverageStatus.BLOCKED
    if not execution_succeeded or output_contract.get("pados_capability_outcome") != "pados.capability-outcome.v1":
        return CoverageStatus.UNRESOLVED
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return CoverageStatus.UNRESOLVED
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "outcome"}
        or value.get("schema_version") != "pados.capability-outcome.v1"
    ):
        return CoverageStatus.UNRESOLVED
    try:
        outcome = CoverageStatus(str(value["outcome"]))
    except ValueError:
        return CoverageStatus.UNRESOLVED
    if outcome is CoverageStatus.REJECTED:
        return CoverageStatus.UNRESOLVED
    return outcome


def _p6_10_resource_kind(worker_type: str) -> tuple[ResourceKind, str | None]:
    try:
        worker = WorkerType(worker_type)
    except ValueError as exc:
        raise ValueError("P6_10_WORKER_TYPE_UNSUPPORTED") from exc
    if worker is WorkerType.OLLAMA:
        return ResourceKind.LOCAL_MODEL, "ollama"
    return ResourceKind.TOOL, None


def record_p6_10_execution_result(
    session: Session,
    *,
    loop: dict[str, Any],
    step: dict[str, Any],
    iteration: dict[str, Any],
    job: dict[str, Any],
    artifact_root: Path,
    task_envelope: TaskEnvelope | None,
) -> CapabilityResult | None:
    """Adapt one P6.10 native result into a sealed receipt and CapabilityResult."""
    binding = session.execute(
        text(
            """
            SELECT * FROM goal_runtime_bindings
            WHERE step_id = CAST(:step_id AS uuid)
              AND plan_id = CAST(:plan_id AS uuid)
              AND loop_id = CAST(:loop_id AS uuid)
            """
        ),
        {"step_id": str(step["id"]), "plan_id": str(loop["active_plan_id"]), "loop_id": str(loop["id"])},
    ).mappings().first()
    if binding is None:
        return None
    if task_envelope is None:
        raise ValueError("TASK_ENVELOPE_REQUIRED_FOR_CAPABILITY_RESULT")
    if (
        str(binding["task_id"]) != task_envelope.task_id
        or str(binding["goal_id"]) not in {goal.goal_id for goal in task_envelope.normalized_goals}
        or task_envelope.project_ref != str(loop["project_id"])
    ):
        raise ValueError("GOAL_RUNTIME_BINDING_AUTHORITY_MISMATCH")

    artifacts, stdout_bytes = _receipt_artifacts(
        session, str(job["id"]), artifact_root=artifact_root
    )
    output_contract = step["output_contract"] or {}
    if not isinstance(output_contract, dict):
        output_contract = {}
    outcome = normalize_p6_10_capability_outcome(
        output_contract,
        stdout_bytes,
        execution_succeeded=str(job["status"]) == "SUCCEEDED",
        execution_status=str(job["status"]),
    )
    response_digest = hashlib.sha256(stdout_bytes).hexdigest() if stdout_bytes else canonical_hash(
        {"job_id": str(job["id"]), "status": str(job["status"]), "summary": str(job.get("result_summary") or "")}
    )
    worker_type = str(job["worker_type"])
    resource_kind, provider = _p6_10_resource_kind(worker_type)
    provenance = Provenance(
        worker_id=str(job["worker_id"]),
        resource_id=str(job["worker_id"]),
        resource_kind=resource_kind,
        adapter_id="pados.p6_10.taskloop",
        adapter_version="1.0",
        normalized_from="p6.10-native-result",
        provider_response_digest=response_digest,
        input_digests=(task_envelope.content_hash,),
    )
    project = session.execute(
        text("SELECT classification FROM projects WHERE id = CAST(:id AS uuid)"),
        {"id": str(loop["project_id"])},
    ).mappings().one()
    started_at_value = iteration.get("started_at") or datetime.now(UTC)
    completed_at_value = job.get("completed_at") or datetime.now(UTC)
    started_at = _iso_timestamp(started_at_value)
    completed_at = _iso_timestamp(completed_at_value)
    execution_status = _receipt_status(str(job["status"]))
    binding_payload: dict[str, Any] = {
        "task_id": task_envelope.task_id,
        "goal_id": str(binding["goal_id"]),
        "plan_id": str(binding["plan_id"]),
        "step_id": str(binding["step_id"]),
        "loop_id": str(binding["loop_id"]),
        "worker_id": str(job["worker_id"]),
        "worker_type": worker_type,
        "capability": str(step["required_capability"]),
        "provider_resource": f"{worker_type}:{job['worker_id']}",
        "project_ref": task_envelope.project_ref,
        "classification": str(project["classification"]).upper(),
        "scope_version_ref": task_envelope.scope_version_ref,
        "authorization_ref": str(loop["approval_id"]),
        "execution_status": execution_status,
        "started_at": started_at,
        "completed_at": completed_at,
        "provenance_hash": canonical_hash(provenance.model_dump(mode="json")),
        "protocol_version": "1.0",
        "schema_version": "pados.execution-receipt.v1",
    }
    if binding["residual_id"] is not None:
        binding_payload["residual_id"] = str(binding["residual_id"])
    attempt = int(step.get("attempt_count") or 0) + 1
    receipt = execution_receipt(
        run_id=f"p6.10-loop:{loop['id']}",
        attempt=attempt,
        contract={
            "work_contract_id": f"p6.10-step:{step['id']}",
            "plan_id": str(loop["active_plan_id"]),
            "sequence": int(step["sequence"]),
            "capability": str(step["required_capability"]),
        },
        artifacts=artifacts,
        provider_id=f"worker:{worker_type}:{job['worker_id']}",
        evidence_kind="DETERMINISTIC_HOST"
        if str(job["worker_type"]) == "DETERMINISTIC_TOOL"
        else "LIVE_PROVIDER",
        ok=str(job["status"]) == "SUCCEEDED",
        execution_binding=binding_payload,
    )
    receipt_ref = ExecutionReceiptRef(
        protocol_version="1.0",
        schema_version=ExecutionReceiptRef.SCHEMA,
        receipt_id=str(receipt["receipt_id"]),
        digest=str(receipt["content_hash"]),
    )
    product_goal_id = str(binding["goal_id"])
    completed_goals = (product_goal_id,) if outcome is CoverageStatus.COMPLETE else ()
    partial_goals = (product_goal_id,) if outcome is CoverageStatus.PARTIAL else ()
    unresolved_goals = (product_goal_id,) if outcome is CoverageStatus.UNRESOLVED else ()
    blocked_goals = (product_goal_id,) if outcome is CoverageStatus.BLOCKED else ()
    capability_result = CapabilityResult(
        protocol_version="1.0",
        schema_version=CapabilityResult.SCHEMA,
        result_id="result:" + hashlib.sha256(f"{receipt_ref.digest}:{product_goal_id}".encode()).hexdigest()[:40],
        task_id=task_envelope.task_id,
        goal_id=product_goal_id,
        capability=str(step["required_capability"]),
        resource_id=str(job["worker_id"]),
        resource_kind=resource_kind,
        provider=provider,
        completed_goals=completed_goals,
        partial_goals=partial_goals,
        unresolved_goals=unresolved_goals,
        blocked_goals=blocked_goals,
        rejected_goals=(),
        contributions=(),
        artifacts=tuple(
            ArtifactReference(
                artifact_id=f"worker-artifact:{item['id']}",
                digest=str(item["sha256"]),
                project_ref=task_envelope.project_ref,
                classification=task_envelope.classification,
            )
            for item in _artifact_rows(session, str(job["id"]))
            if str(item["artifact_type"]) == "stdout"
        ),
        evidence=(
            EvidenceReference(
                evidence_id="evidence:" + str(receipt_ref.digest)[:40],
                digest=receipt_ref.digest,
                source_ref=receipt_ref.receipt_id,
            ),
        ),
        metrics={},
        confidence=None,
        failure_reason=(
            UnresolvedReason.DEPENDENCY_BLOCKED
            if outcome is CoverageStatus.BLOCKED
            else UnresolvedReason.QUALITY_INSUFFICIENT
            if outcome is CoverageStatus.UNRESOLVED
            else None
        ),
        policy_refusal=None,
        execution_receipt_ref=receipt_ref,
        authorization=AuthorizationMetadata(
            project_ref=task_envelope.project_ref,
            classification=task_envelope.classification,
            scope_version_ref=task_envelope.scope_version_ref,
            grant_ref=str(loop["approval_id"]),
            policy_decision_ref=str(loop["approval_id"]),
            capability=str(step["required_capability"]),
        ),
        provenance=provenance,
    )
    payload = {
        "execution_receipt": receipt,
        "execution_receipt_hash": receipt["content_hash"],
        "capability_result": capability_result.model_dump(mode="json"),
        "capability_result_hash": capability_result.content_hash,
        "runtime_binding_id": str(binding["id"]),
        "task_id": task_envelope.task_id,
        "goal_id": product_goal_id,
        "residual_id": binding["residual_id"],
        "step_status": str(job["status"]),
    }
    session.execute(
        text(
            """
            INSERT INTO task_loop_events(loop_id, project_id, event_type, payload_json)
            VALUES (
              CAST(:loop_id AS uuid), CAST(:project_id AS uuid),
              'CAPABILITY_RESULT_NORMALIZED', CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "loop_id": str(loop["id"]),
            "project_id": str(loop["project_id"]),
            "payload": json.dumps(payload, sort_keys=True),
        },
    )
    return capability_result


def rebuild_p6_10_lineage_projection(
    session: Session,
    *,
    task_envelope: TaskEnvelope,
    plan_id: str,
    loop_id: str,
) -> tuple[RuntimeLineageProjection, ...]:
    """Rebuild live graph lineage only from P6.10 bindings and sealed result events."""
    binding_rows = session.execute(
        text(
            """
            SELECT b.goal_id, b.residual_id, b.plan_id::text AS plan_id, b.step_id::text AS step_id,
                   b.loop_id::text AS loop_id, s.sequence, s.status AS step_status,
                   s.required_capability, l.approval_id::text AS authorization_ref,
                   a.amendment_id, a.parent_goal_id, a.authority_decision
            FROM goal_runtime_bindings b
            JOIN task_plan_steps s ON s.id = b.step_id AND s.task_plan_id = b.plan_id
            JOIN task_loops l ON l.id = b.loop_id
            LEFT JOIN residual_plan_amendments a ON a.id = b.amendment_id
            WHERE b.task_id = :task_id AND b.plan_id = CAST(:plan_id AS uuid)
              AND b.loop_id = CAST(:loop_id AS uuid)
            ORDER BY s.sequence
            """
        ),
        {"task_id": task_envelope.task_id, "plan_id": plan_id, "loop_id": loop_id},
    ).mappings().all()
    results_by_step: dict[str, tuple[CapabilityResult, dict[str, Any]]] = {}
    events: Sequence[Any] = session.execute(
        text(
            """
            SELECT payload_json FROM task_loop_events
            WHERE loop_id = CAST(:loop_id AS uuid)
              AND event_type = 'CAPABILITY_RESULT_NORMALIZED'
            ORDER BY sequence_number
            """
        ),
        {"loop_id": loop_id},
    ).scalars().all()
    for event in events:
        payload = event if isinstance(event, dict) else json.loads(str(event))
        raw_result = payload.get("capability_result")
        raw_receipt = payload.get("execution_receipt")
        if not isinstance(raw_result, dict) or not isinstance(raw_receipt, dict):
            raise TypeError("P6_10_LINEAGE_RECEIPT_EVENT_INVALID")
        capability_result = CapabilityResult.model_validate(raw_result)
        if capability_result.content_hash != payload.get("capability_result_hash"):
            raise ValueError("P6_10_LINEAGE_RESULT_HASH_MISMATCH")
        receipt_payload = {
            key: value for key, value in raw_receipt.items() if key not in {"receipt_id", "content_hash"}
        }
        receipt_hash = canonical_hash(receipt_payload)
        if (
            receipt_hash != raw_receipt.get("content_hash")
            or raw_receipt.get("receipt_id") != f"execution-sha256:{receipt_hash}"
            or receipt_hash != payload.get("execution_receipt_hash")
            or capability_result.execution_receipt_ref.digest != receipt_hash
            or capability_result.execution_receipt_ref.receipt_id != raw_receipt.get("receipt_id")
        ):
            raise ValueError("P6_10_LINEAGE_RECEIPT_HASH_MISMATCH")
        execution = raw_receipt.get("execution_binding")
        if not isinstance(execution, dict):
            raise TypeError("P6_10_LINEAGE_EXECUTION_BINDING_MISSING")
        if (
            execution.get("task_id") != task_envelope.task_id
            or execution.get("project_ref") != task_envelope.project_ref
            or execution.get("classification") != task_envelope.classification.value
            or execution.get("scope_version_ref") != task_envelope.scope_version_ref
            or execution.get("goal_id") != capability_result.goal_id
            or execution.get("capability") != capability_result.capability
            or execution.get("worker_id") != capability_result.provenance.worker_id
            or execution.get("authorization_ref") != capability_result.authorization.grant_ref
            or execution.get("plan_id") != plan_id
            or execution.get("loop_id") != loop_id
        ):
            raise ValueError("P6_10_LINEAGE_EXECUTION_BINDING_MISMATCH")
        results_by_step[str(execution["step_id"])] = (capability_result, execution)

    goal_ids = {goal.goal_id for goal in task_envelope.normalized_goals}
    projections: list[RuntimeLineageProjection] = []
    for row in binding_rows:
        goal_id = str(row["goal_id"])
        if goal_id not in goal_ids:
            raise ValueError("P6_10_LINEAGE_GOAL_BINDING_UNKNOWN")
        result_entry = results_by_step.get(str(row["step_id"]))
        row_result: CapabilityResult | None = None if result_entry is None else result_entry[0]
        execution = None if result_entry is None else result_entry[1]
        residual_id = None if row["residual_id"] is None else str(row["residual_id"])
        amendment_id = row["amendment_id"]
        parent_goal_id = row["parent_goal_id"]
        authority_decision = row["authority_decision"]
        if residual_id is not None and (
            amendment_id is None or parent_goal_id is None or authority_decision is None
        ):
            raise ValueError("P6_10_LINEAGE_AMENDMENT_BINDING_MISSING")
        if authority_decision not in {None, "AUTO_AMENDABLE", "REAPPROVAL_REQUIRED"}:
            raise ValueError("P6_10_LINEAGE_AMENDMENT_DECISION_INVALID")
        if row_result is not None and (
            row_result.goal_id != goal_id
            or row_result.capability != str(row["required_capability"])
            or row_result.authorization.grant_ref != str(row["authorization_ref"])
            or execution is None
            or execution.get("residual_id") != residual_id
            or str(execution.get("step_id")) != str(row["step_id"])
            or str(execution.get("plan_id")) != str(row["plan_id"])
            or str(execution.get("loop_id")) != str(row["loop_id"])
        ):
            raise ValueError("P6_10_LINEAGE_RESULT_RUNTIME_BINDING_MISMATCH")
        projections.append(
            RuntimeLineageProjection(
                task_id=task_envelope.task_id,
                goal_id=goal_id,
                project_ref=task_envelope.project_ref,
                classification=task_envelope.classification,
                residual_id=residual_id,
                parent_goal_id=None if parent_goal_id is None else str(parent_goal_id),
                amendment_id=None if amendment_id is None else str(amendment_id),
                authority_decision=cast(
                    Literal["AUTO_AMENDABLE", "REAPPROVAL_REQUIRED"] | None,
                    authority_decision,
                ),
                plan_id=str(row["plan_id"]),
                step_id=str(row["step_id"]),
                sequence=int(row["sequence"]),
                step_status=str(row["step_status"]),
                worker_id=None if row_result is None else row_result.provenance.worker_id,
                capability=str(row["required_capability"]),
                receipt_ref=None if row_result is None else row_result.execution_receipt_ref,
                artifacts=() if row_result is None else row_result.artifacts,
            )
        )
    return tuple(projections)


def _artifact_rows(session: Session, job_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text("SELECT * FROM worker_job_artifacts WHERE job_id = CAST(:id AS uuid) ORDER BY created_at, id"),
        {"id": job_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _receipt_artifacts(
    session: Session, job_id: str, *, artifact_root: Path
) -> tuple[list[dict[str, Any]], bytes]:
    root = artifact_root.resolve(strict=True)
    metadata: list[dict[str, Any]] = []
    stdout = b""
    for item in _artifact_rows(session, job_id):
        artifact_type = str(item["artifact_type"])
        if artifact_type not in {"stdout", "result_manifest"}:
            continue
        path = Path(str(item["path"])).resolve(strict=True)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("EXECUTION_ARTIFACT_OUTSIDE_RECEIPT_ROOT") from exc
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(item["sha256"]):
            raise ValueError("EXECUTION_ARTIFACT_DIGEST_MISMATCH")
        if len(content) > 400_000:
            raise ValueError("EXECUTION_ARTIFACT_TOO_LARGE_FOR_RECEIPT")
        metadata.append({"path": relative, "sha256": digest, "size_bytes": len(content)})
        if artifact_type == "stdout":
            stdout = content
    return metadata, stdout


def _iso_timestamp(value: object) -> str:
    timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None:
        raise ValueError("EXECUTION_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return timestamp.astimezone(UTC).isoformat()


def _receipt_status(worker_status: str) -> str:
    if worker_status == "SUCCEEDED":
        return "SUCCEEDED"
    if worker_status == "BLOCKED":
        return "BLOCKED"
    if worker_status == "TIMED_OUT":
        return "TIMED_OUT"
    return "FAILED"


def reconcile_residual_work(
    session: Session, loop_id: str, *, settings: Any = None
) -> tuple[dict[str, Any], ...]:
    """Compile persisted product residuals into P6.10 steps, under the loop lock."""
    loop_row = session.execute(
        text("SELECT * FROM task_loops WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": loop_id},
    ).mappings().one()
    loop = dict(loop_row)
    if loop["status"] != LoopStatus.RUNNING.value:
        raise PermissionError("RESIDUAL_AMENDMENT_REQUIRES_RUNNING_TASKLOOP")
    request = session.execute(
        text("SELECT body FROM control_work_requests WHERE id = CAST(:id AS uuid)"),
        {"id": str(loop["request_id"])},
    ).mappings().one()
    envelope = _task_envelope_from_body(request["body"])
    if envelope is None:
        return ()
    results = _persisted_capability_results(session, loop_id)
    if not results:
        return ()
    coverage = build_coverage(envelope, results)
    residuals = create_residual_tasks(envelope, coverage, results)
    amendments: list[dict[str, Any]] = []
    for residual in residuals:
        for residual_goal in residual.goals:
            if _goal_has_live_runtime_step(session, envelope.task_id, residual_goal.goal_id, str(loop["active_plan_id"])):
                continue
            parent_goal_id = _parent_goal_id(envelope, residual_goal.goal_id)
            if parent_goal_id is None:
                continue
            source_result = _source_result_for_goal(results, parent_goal_id, unresolved_only=True)
            if source_result is None:
                source_result = _source_result_for_goal(
                    results, residual_goal.goal_id, unresolved_only=True
                )
            if source_result is None:
                source_result = _source_result_for_goal(results, parent_goal_id, unresolved_only=False)
            if source_result is None:
                continue
            amendment = _request_one_amendment(
                session,
                loop=loop,
                envelope=envelope,
                residual=residual,
                residual_goal_id=residual_goal.goal_id,
                coverage=coverage,
                source_result=source_result,
                parent_goal_id=parent_goal_id,
                settings=settings,
            )
            amendments.append(amendment)
    return tuple(amendments)


def _task_envelope_from_body(body_value: object) -> TaskEnvelope | None:
    try:
        body = json.loads(str(body_value))
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("task_envelope"), dict):
        return None
    envelope = TaskEnvelope.model_validate(body["task_envelope"])
    if body.get("task_envelope_hash") != envelope.content_hash:
        raise ValueError("TASK_ENVELOPE_HASH_MISMATCH")
    return envelope


def _persisted_capability_results(session: Session, loop_id: str) -> tuple[CapabilityResult, ...]:
    rows: Any = session.execute(
        text(
            """
            SELECT payload_json FROM task_loop_events
            WHERE loop_id = CAST(:loop_id AS uuid)
              AND event_type = 'CAPABILITY_RESULT_NORMALIZED'
            ORDER BY sequence_number
            """
        ),
        {"loop_id": loop_id},
    ).scalars().all()
    results: list[CapabilityResult] = []
    for payload in rows:
        value = payload if isinstance(payload, dict) else json.loads(str(payload))
        result_value = value.get("capability_result")
        if not isinstance(result_value, dict):
            raise TypeError("PERSISTED_CAPABILITY_RESULT_INVALID")
        result = CapabilityResult.model_validate(result_value)
        if result.content_hash != value.get("capability_result_hash"):
            raise ValueError("PERSISTED_CAPABILITY_RESULT_HASH_MISMATCH")
        results.append(result)
    return tuple(results)


def _goal_has_live_runtime_step(
    session: Session, task_id: str, goal_id: str, plan_id: str
) -> bool:
    status = session.execute(
        text(
            """
            SELECT s.status
            FROM goal_runtime_bindings b
            JOIN task_plan_steps s ON s.id = b.step_id
            WHERE b.task_id = :task_id AND b.goal_id = :goal_id
              AND b.plan_id = CAST(:plan_id AS uuid)
              AND b.residual_id IS NULL
              AND s.status IN ('PENDING', 'READY', 'ROUTING', 'RUNNING', 'VALIDATING', 'BLOCKED')
            LIMIT 1
            """
        ),
        {"task_id": task_id, "goal_id": goal_id, "plan_id": plan_id},
    ).scalar_one_or_none()
    return status is not None


def _parent_goal_id(task: TaskEnvelope, goal_id: str) -> str | None:
    parents = {
        relation.source_goal_id
        for relation in task.goal_relations
        if relation.relation is GoalRelationType.PARENT_OF and relation.target_goal_id == goal_id
    }
    if len(parents) == 1:
        return next(iter(parents))
    if not parents:
        return goal_id
    return None


def _goal_has_other_live_residual_step(
    session: Session,
    *,
    task_id: str,
    goal_id: str,
    residual_id: str,
    plan_id: str,
) -> bool:
    step_id = session.execute(
        text(
            """
            SELECT s.id
            FROM goal_runtime_bindings b
            JOIN task_plan_steps s ON s.id = b.step_id
            WHERE b.task_id = :task_id AND b.goal_id = :goal_id
              AND b.plan_id = CAST(:plan_id AS uuid)
              AND b.residual_id IS NOT NULL AND b.residual_id <> :residual_id
              AND s.status IN ('PENDING', 'READY', 'ROUTING', 'RUNNING', 'VALIDATING', 'BLOCKED')
            LIMIT 1
            """
        ),
        {
            "task_id": task_id,
            "goal_id": goal_id,
            "residual_id": residual_id,
            "plan_id": plan_id,
        },
    ).scalar_one_or_none()
    return step_id is not None


def _source_result_for_goal(
    results: tuple[CapabilityResult, ...], goal_id: str, *, unresolved_only: bool
) -> CapabilityResult | None:
    for result in reversed(results):
        if result.goal_id != goal_id:
            continue
        if not unresolved_only or goal_id in result.partial_goals or goal_id in result.unresolved_goals:
            return result
    return None


def _request_one_amendment(
    session: Session,
    *,
    loop: dict[str, Any],
    envelope: TaskEnvelope,
    residual: ResidualTask,
    residual_goal_id: str,
    coverage: CoverageMap,
    source_result: CapabilityResult,
    parent_goal_id: str,
    settings: Any,
) -> dict[str, Any]:
    plan_id = str(loop["active_plan_id"])
    task_id = envelope.task_id
    key = residual_amendment_idempotency_key(task_id, residual.residual_task_id, plan_id, residual_goal_id)
    existing = session.execute(
        text(
            """
            SELECT * FROM residual_plan_amendments
            WHERE task_id = :task_id AND residual_id = :residual_id
              AND goal_id = :goal_id AND plan_id = CAST(:plan_id AS uuid)
            FOR UPDATE
            """
        ),
        {"task_id": task_id, "residual_id": residual.residual_task_id, "goal_id": residual_goal_id, "plan_id": plan_id},
    ).mappings().first()
    if existing is not None and str(existing["idempotency_key"]) != key:
        raise ValueError("RESIDUAL_AMENDMENT_IDEMPOTENCY_CONFLICT")
    parent = session.execute(
        text(
            """
            SELECT b.step_id::text AS step_id, b.plan_id::text AS plan_id, b.loop_id::text AS loop_id,
                   s.*
            FROM goal_runtime_bindings b
            JOIN task_plan_steps s ON s.id = b.step_id
            WHERE b.task_id = :task_id AND b.goal_id = :goal_id
              AND b.plan_id = CAST(:plan_id AS uuid)
              AND b.loop_id = CAST(:loop_id AS uuid)
              AND (CAST(:existing_parent_step_id AS uuid) IS NULL OR s.id = CAST(:existing_parent_step_id AS uuid))
            ORDER BY (b.residual_id IS NULL) DESC, s.sequence DESC
            LIMIT 1
            """
        ),
        {
            "task_id": task_id,
            "goal_id": parent_goal_id,
            "plan_id": plan_id,
            "loop_id": str(loop["id"]),
            "existing_parent_step_id": None if existing is None else str(existing["parent_step_id"]),
        },
    ).mappings().first()
    if parent is None:
        raise ValueError("RESIDUAL_PARENT_RUNTIME_BINDING_MISSING")
    parent_step = dict(parent)
    if str(parent_step["plan_id"]) != plan_id or str(parent_step["loop_id"]) != str(loop["id"]):
        raise ValueError("RESIDUAL_PARENT_RUNTIME_BINDING_MISMATCH")

    authority = session.execute(
        text(
            """
            SELECT g.project_id::text AS project_id, g.approval_id::text AS goal_approval_id,
                   g.status AS goal_status, p.status AS plan_status, p.approval_id::text AS plan_approval_id,
                   p.content_hash AS plan_content_hash, pr.classification, pr.status AS project_status,
                   a.project_id::text AS approval_project_id, a.status AS approval_status,
                   a.id::text AS approval_id, l.approval_id::text AS loop_approval_id,
                   l.current_iteration, l.max_iterations
            FROM task_loops l
            JOIN task_goals g ON g.id = l.task_goal_id
            JOIN task_plans p ON p.id = l.active_plan_id
            JOIN projects pr ON pr.id = l.project_id
            JOIN control_approvals a ON a.id = l.approval_id
            WHERE l.id = CAST(:loop_id AS uuid)
            FOR UPDATE OF l, g, p, pr, a
            """
        ),
        {"loop_id": str(loop["id"])},
    ).mappings().one()
    current_scope_version = scope_authority.read_current_scope_version(
        settings, str(authority["project_id"])
    )
    current_scope_ref = "" if current_scope_version is None else f"scope:{current_scope_version}"
    project_ref = str(authority["project_id"])
    classification = str(authority["classification"]).upper()
    approval_valid = (
        authority["approval_status"] == "APPROVED"
        and str(authority["approval_id"]) == str(loop["approval_id"])
        and str(authority["approval_project_id"]) == project_ref
        and str(authority["goal_approval_id"]) == str(loop["approval_id"])
        and str(authority["plan_approval_id"]) == str(loop["approval_id"])
        and str(authority["loop_approval_id"]) == str(loop["approval_id"])
        and authority["goal_status"] in {"APPROVED", "ACTIVE"}
        and authority["plan_status"] in {"APPROVED", "ACTIVE"}
        and str(authority["project_status"]).upper() == "ACTIVE"
    )
    # A child inherits only the exact capability authorized by its bound parent
    # step. Another plan step's capability is not transferable authority.
    approved_capabilities = frozenset({str(parent_step["required_capability"])})
    preferred_worker_types = tuple(str(value) for value in (parent_step["preferred_worker_types"] or []))
    parent_output_contract = parent_step["output_contract"] or {}
    if not isinstance(parent_output_contract, dict):
        parent_output_contract = {}
    network_allowed = parent_output_contract.get("network_requirement") == "REQUIRED"
    expected_scope_ref = envelope.scope_version_ref
    approved_envelope = ApprovedExecutionEnvelope(
        task_id=task_id,
        project_ref=project_ref,
        classification=classification,
        scope_version_ref=expected_scope_ref,
        goal_ids=frozenset(goal.goal_id for goal in envelope.normalized_goals),
        allowed_capabilities=approved_capabilities,
        allowed_paths=frozenset(str(value) for value in (parent_step["allowed_paths"] or [])),
        allowed_actions=frozenset(str(value) for value in (parent_step["allowed_actions"] or [])),
        worker_types=frozenset(preferred_worker_types),
        network_allowed=network_allowed,
        approval_valid=approval_valid,
        current_scope_version_ref=current_scope_ref,
    )
    residual_goal = next(item for item in residual.goals if item.goal_id == residual_goal_id)
    capabilities = tuple(residual_goal.required_capabilities)
    requested_capability = capabilities[0] if capabilities else str(parent_step["required_capability"])
    network_required = any(item.value == "NETWORK_UNAVAILABLE" for item in residual_goal.unresolved_reasons)
    read_only_actions = {"READ_REPOSITORY", "SEARCH_CONTEXT", "READ_ARTIFACT"}
    parent_actions = tuple(str(value) for value in (parent_step["allowed_actions"] or []))
    fresh_approval = (
        str(parent_step["risk_level"]) != "LOW"
        or str(parent_step["approval_requirement"]) != "PLAN_APPROVAL"
        or not set(parent_actions).issubset(read_only_actions)
        or len(capabilities) != 1
    )
    residual_request = ResidualExecutionRequest(
        task_id=task_id,
        residual_id=residual.residual_task_id,
        goal_id=residual_goal_id,
        parent_goal_id=parent_goal_id,
        project_ref=envelope.project_ref,
        classification=envelope.classification.value,
        scope_version_ref=envelope.scope_version_ref,
        required_capability=requested_capability,
        dependencies=tuple(dict.fromkeys((*residual_goal.dependencies, *((parent_goal_id,) if parent_goal_id != residual_goal_id else ())))),
        allowed_paths=tuple(str(value) for value in (parent_step["allowed_paths"] or [])),
        allowed_actions=parent_actions,
        worker_type=preferred_worker_types[0] if preferred_worker_types else "",
        network_required=network_required,
        fresh_approval_required=fresh_approval,
        authorization_valid=(
            approval_valid
            and source_result.task_id == task_id
            and source_result.goal_id in {parent_goal_id, residual_goal_id}
            and source_result.authorization.project_ref == project_ref
            and source_result.authorization.classification.value == classification
            and source_result.authorization.scope_version_ref == expected_scope_ref
            and source_result.authorization.grant_ref == str(loop["approval_id"])
            and source_result.authorization.policy_decision_ref == str(loop["approval_id"])
            and source_result.authorization.capability == source_result.capability
        ),
    )
    decision = classify_residual_authority(approved_envelope, residual_request)
    reason_codes: list[str] = []
    if not residual.automatic_routing_allowed:
        decision = AmendmentAuthority.REAPPROVAL_REQUIRED
        reason_codes.append("RESIDUAL_POLICY_OR_HUMAN_GATE")
    if current_scope_ref != expected_scope_ref:
        reason_codes.append(
            "SCOPE_VERSION_AUTHORITY_UNAVAILABLE" if current_scope_version is None else "SCOPE_VERSION_STALE"
        )
    if not approval_valid or not residual_request.authorization_valid:
        reason_codes.append("AUTHORIZATION_REVALIDATION_FAILED")
    if requested_capability not in approved_capabilities:
        reason_codes.append("CAPABILITY_EXPANSION")
    if network_required and not network_allowed:
        reason_codes.append("NETWORK_REQUIREMENT_EXPANSION")
    if residual_goal_id not in approved_envelope.goal_ids:
        reason_codes.append("GOAL_SCOPE_EXPANSION")
    if decision is AmendmentAuthority.REAPPROVAL_REQUIRED and not reason_codes:
        reason_codes.append("EXECUTION_ENVELOPE_EXPANSION")

    if not preferred_worker_types:
        decision = AmendmentAuthority.REAPPROVAL_REQUIRED
        reason_codes.append("NO_APPROVED_WORKER_RESOURCE")
    sequence: Any = session.execute(
        text("SELECT COALESCE(max(sequence), 0) + 1 FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
        {"id": plan_id},
    ).scalar_one()
    if int(sequence) > 50 or int(authority["current_iteration"]) + 1 >= int(authority["max_iterations"]):
        decision = AmendmentAuthority.REAPPROVAL_REQUIRED
        reason_codes.append("P6_10_EXECUTION_LIMIT_REACHED")

    dependency_goal_ids = residual_request.dependencies
    bindings_rows = session.execute(
        text(
            """
            SELECT b.task_id, b.goal_id, b.plan_id::text AS plan_id, b.loop_id::text AS loop_id,
                   b.step_id::text AS step_id, s.sequence, s.status
            FROM goal_runtime_bindings b
            JOIN task_plan_steps s ON s.id = b.step_id
            WHERE b.task_id = :task_id AND b.plan_id = CAST(:plan_id AS uuid)
              AND b.loop_id = CAST(:loop_id AS uuid)
            """
        ),
        {"task_id": task_id, "plan_id": plan_id, "loop_id": str(loop["id"])},
    ).mappings().all()
    bindings: dict[str, RuntimeGoalBinding] = {}
    for item in bindings_rows:
        goal_key = str(item["goal_id"])
        candidate_binding = RuntimeGoalBinding(
            task_id=str(item["task_id"]),
            goal_id=goal_key,
            plan_id=str(item["plan_id"]),
            loop_id=str(item["loop_id"]),
            step_id=str(item["step_id"]),
            sequence=int(item["sequence"]),
            status=str(item["status"]),
        )
        existing_binding = bindings.get(goal_key)
        if existing_binding is None or candidate_binding.sequence > existing_binding.sequence:
            bindings[goal_key] = candidate_binding
    runtime_dependencies: tuple[int, ...] = ()
    if decision is AmendmentAuthority.AUTO_AMENDABLE:
        try:
            runtime_dependencies = resolve_runtime_dependencies(
                dependency_goal_ids,
                bindings,
                plan_id=plan_id,
                loop_id=str(loop["id"]),
                new_sequence=int(sequence),
            )
        except ValueError as exc:
            decision = AmendmentAuthority.REAPPROVAL_REQUIRED
            reason_codes.append(str(exc))
    if existing is None and _goal_has_other_live_residual_step(
        session,
        task_id=task_id,
        goal_id=residual_goal_id,
        residual_id=residual.residual_task_id,
        plan_id=plan_id,
    ):
        decision = AmendmentAuthority.REAPPROVAL_REQUIRED
        reason_codes.append("RESIDUAL_GOAL_ALREADY_IN_FLIGHT")

    product_amendment = ResidualPlanAmendment(
        protocol_version="1.0",
        schema_version=ResidualPlanAmendment.SCHEMA,
        amendment_id="amendment:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:40],
        task_id=task_id,
        goal_id=residual_goal_id,
        residual_id=residual.residual_task_id,
        parent_goal_id=parent_goal_id,
        parent_plan_ref=plan_id,
        parent_step_ref=str(parent_step["step_id"]),
        project_ref=envelope.project_ref,
        classification=envelope.classification,
        priority=envelope.priority,
        deadline=envelope.deadline,
        dependencies=dependency_goal_ids,
        required_capability=requested_capability,
        network_requirement="REQUIRED" if network_required else "NONE",
        resource_constraints={
            "worker_types": ",".join(preferred_worker_types),
            "max_iterations": int(authority["max_iterations"]),
            "task_envelope_hash": envelope.content_hash,
            "residual_task_hash": residual.content_hash,
            "parent_step_envelope_hash": canonical_hash(
                {
                    "step_type": str(parent_step["step_type"]),
                    "required_capability": str(parent_step["required_capability"]),
                    "preferred_worker_types": list(parent_step["preferred_worker_types"] or []),
                    "dependencies": list(parent_step["dependencies"] or []),
                    "allowed_paths": list(parent_step["allowed_paths"] or []),
                    "allowed_actions": list(parent_step["allowed_actions"] or []),
                    "prohibited_actions": list(parent_step["prohibited_actions"] or []),
                    "context_requirements": list(parent_step["context_requirements"] or []),
                    "input_artifacts": list(parent_step["input_artifacts"] or []),
                    "output_contract": parent_output_contract,
                    "validation_policy_id": str(parent_step["validation_policy_id"]),
                    "command_id": parent_step["command_id"],
                    "success_condition": str(parent_step["success_condition"]),
                    "failure_policy": str(parent_step["failure_policy"]),
                    "timeout_seconds": int(parent_step["timeout_seconds"]),
                    "risk_level": str(parent_step["risk_level"]),
                    "approval_requirement": str(parent_step["approval_requirement"]),
                }
            ),
        },
        authorization_refs=tuple(
            dict.fromkeys(
                (
                    str(loop["approval_id"]),
                    source_result.authorization.grant_ref,
                    source_result.authorization.policy_decision_ref,
                )
            )
        ),
        expected_scope_version_ref=envelope.scope_version_ref,
        provenance=source_result.provenance,
        source_result_hash=source_result.content_hash,
        coverage_map_hash=coverage.content_hash,
        idempotency_key=key,
        authority_decision=AmendmentAuthorityDecision(decision.value),
    )

    if decision is AmendmentAuthority.AUTO_AMENDABLE:
        latest_scope_version = scope_authority.read_current_scope_version(
            settings, project_ref
        )
        if latest_scope_version is None or envelope.scope_version_ref != f"scope:{latest_scope_version}":
            decision = AmendmentAuthority.REAPPROVAL_REQUIRED
            reason_codes.append(
                "SCOPE_VERSION_AUTHORITY_UNAVAILABLE"
                if latest_scope_version is None
                else "SCOPE_VERSION_CHANGED_BEFORE_PLAN_APPEND"
            )
            product_amendment = product_amendment.model_copy(
                update={"authority_decision": AmendmentAuthorityDecision.REAPPROVAL_REQUIRED}
            )

    if existing is not None:
        _assert_immutable_amendment_content(str(existing["content_hash"]), product_amendment.content_hash)
        return dict(existing)

    step_id: str | None = None
    if decision is AmendmentAuthority.AUTO_AMENDABLE:
        goal = next(goal for goal in envelope.normalized_goals if goal.goal_id == residual_goal_id)
        source_paths = list(parent_step["allowed_paths"] or [])
        source_actions = list(parent_step["allowed_actions"] or [])
        source_artifacts = list(parent_step["input_artifacts"] or [])
        preserved_artifacts = list(residual.preserved_artifacts)
        input_artifacts = list(dict.fromkeys([*(str(value) for value in source_artifacts), *preserved_artifacts]))
        step_row = session.execute(
            text(
                """
                INSERT INTO task_plan_steps(
                  task_plan_id, sequence, title, description, step_type, required_capability,
                  preferred_worker_types, dependencies, allowed_paths, allowed_actions,
                  prohibited_actions, context_requirements, input_artifacts, output_contract,
                  validation_policy_id, command_id, success_condition, failure_policy,
                  timeout_seconds, risk_level, approval_requirement, status
                )
                VALUES (
                  CAST(:plan_id AS uuid), :sequence, :title, :description, :step_type,
                  :required_capability, CAST(:preferred_workers AS jsonb), CAST(:dependencies AS jsonb),
                  CAST(:allowed_paths AS jsonb), CAST(:allowed_actions AS jsonb),
                  CAST(:prohibited_actions AS jsonb), CAST(:context_requirements AS jsonb),
                  CAST(:input_artifacts AS jsonb), CAST(:output_contract AS jsonb),
                  :validation_policy_id, :command_id, :success_condition, :failure_policy,
                  :timeout_seconds, :risk_level, :approval_requirement, :status
                )
                RETURNING id::text AS id
                """
            ),
            {
                "plan_id": plan_id,
                "sequence": int(sequence),
                "title": goal.title,
                "description": goal.objective,
                "step_type": str(parent_step["step_type"]),
                "required_capability": requested_capability,
                "preferred_workers": json.dumps(preferred_worker_types),
                "dependencies": json.dumps(runtime_dependencies),
                "allowed_paths": json.dumps(source_paths),
                "allowed_actions": json.dumps(source_actions),
                "prohibited_actions": json.dumps(list(parent_step["prohibited_actions"] or [])),
                "context_requirements": json.dumps(list(parent_step["context_requirements"] or [])),
                "input_artifacts": json.dumps(input_artifacts),
                "output_contract": json.dumps(parent_output_contract, sort_keys=True),
                "validation_policy_id": str(parent_step["validation_policy_id"]),
                "command_id": parent_step["command_id"],
                "success_condition": str(parent_step["success_condition"]),
                "failure_policy": str(parent_step["failure_policy"]),
                "timeout_seconds": int(parent_step["timeout_seconds"]),
                "risk_level": str(parent_step["risk_level"]),
                "approval_requirement": str(parent_step["approval_requirement"]),
                "status": "PENDING" if runtime_dependencies else "READY",
            },
        ).mappings().one()
        step_id = str(step_row["id"])

    record = session.execute(
        text(
            """
            INSERT INTO residual_plan_amendments(
              amendment_id, task_id, residual_id, goal_id, parent_goal_id, plan_id, loop_id,
              parent_step_id, step_id, project_id, project_ref, classification, priority, deadline,
              dependencies, required_capability, network_requirement, resource_constraints,
              authorization_refs, expected_scope_version_ref, provenance, source_result_hash,
              coverage_map_hash, protocol_version, schema_version, idempotency_key, content_hash,
              authority_decision, reason_codes
            )
            VALUES (
              :amendment_id, :task_id, :residual_id, :goal_id, :parent_goal_id,
              CAST(:plan_id AS uuid), CAST(:loop_id AS uuid), CAST(:parent_step_id AS uuid),
              CAST(:step_id AS uuid), CAST(:project_id AS uuid), :project_ref, :classification,
              :priority, CAST(:deadline AS timestamptz), CAST(:dependencies AS jsonb),
              :required_capability, :network_requirement, CAST(:resource_constraints AS jsonb),
              CAST(:authorization_refs AS jsonb), :scope_version, CAST(:provenance AS jsonb),
              :source_hash, :coverage_hash, :protocol_version, :schema_version, :idempotency_key,
              :content_hash, :decision, CAST(:reason_codes AS jsonb)
            )
            RETURNING *
            """
        ),
        {
            "amendment_id": product_amendment.amendment_id,
            "task_id": task_id,
            "residual_id": residual.residual_task_id,
            "goal_id": residual_goal_id,
            "parent_goal_id": parent_goal_id,
            "plan_id": plan_id,
            "loop_id": str(loop["id"]),
            "parent_step_id": str(parent_step["step_id"]),
            "step_id": step_id,
            "project_id": project_ref,
            "project_ref": product_amendment.project_ref,
            "classification": product_amendment.classification.value,
            "priority": product_amendment.priority.value,
            "deadline": None if product_amendment.deadline is None else product_amendment.deadline.isoformat(),
            "dependencies": json.dumps(product_amendment.dependencies),
            "required_capability": product_amendment.required_capability,
            "network_requirement": product_amendment.network_requirement,
            "resource_constraints": json.dumps(dict(product_amendment.resource_constraints), sort_keys=True),
            "authorization_refs": json.dumps(product_amendment.authorization_refs),
            "scope_version": product_amendment.expected_scope_version_ref,
            "provenance": json.dumps(product_amendment.provenance.model_dump(mode="json"), sort_keys=True),
            "source_hash": product_amendment.source_result_hash,
            "coverage_hash": product_amendment.coverage_map_hash,
            "protocol_version": product_amendment.protocol_version,
            "schema_version": product_amendment.schema_version,
            "idempotency_key": key,
            "content_hash": product_amendment.content_hash,
            "decision": decision.value,
            "reason_codes": json.dumps(sorted(set(reason_codes))),
        },
    ).mappings().one()
    if step_id is not None:
        binding_hash = canonical_hash(
            {
                "amendment_hash": product_amendment.content_hash,
                "step_id": step_id,
                "task_envelope_hash": envelope.content_hash,
            }
        )
        insert_goal_runtime_binding(
            session,
            task_id=task_id,
            goal_id=residual_goal_id,
            residual_id=residual.residual_task_id,
            plan_id=plan_id,
            step_id=step_id,
            loop_id=str(loop["id"]),
            amendment_id=str(record["id"]),
            provenance_hash=binding_hash,
        )
    session.execute(
        text(
            """
            INSERT INTO task_loop_events(loop_id, project_id, event_type, payload_json)
            VALUES (
              CAST(:loop_id AS uuid), CAST(:project_id AS uuid), :event_type, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "loop_id": str(loop["id"]),
            "project_id": project_ref,
            "event_type": "RESIDUAL_PLAN_AMENDED" if step_id is not None else "RESIDUAL_AMENDMENT_REAPPROVAL_REQUIRED",
            "payload": json.dumps(
                {
                    "amendment_id": product_amendment.amendment_id,
                    "amendment_hash": product_amendment.content_hash,
                    "authority_decision": decision.value,
                    "reason_codes": sorted(set(reason_codes)),
                    "goal_id": residual_goal_id,
                    "step_id": step_id,
                    "runtime_dependencies": runtime_dependencies,
                },
                sort_keys=True,
            ),
        },
    )
    return dict(record)
