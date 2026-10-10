# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable approval subject for work routing (RC2 D1).

A control approval that may route work names exactly what it authorizes: the
project, the work request, the requesting surface, the executing worker types
and the bounded operations. ``proposal_hash`` is the canonical hash of this
subject, so the approval cannot be reused for anything it does not name.

The requesting surface (``CONTROL_API`` / ``TASK_LOOP``) is a structural
binding, not a user identity: the control API has no authenticated user.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pados.execution.context import canonical_hash
from pados.worker.schemas import RISK_ORDER, CapabilityId, RiskLevel, WorkerType

SUBJECT_KIND_WORK_ROUTE = "WORK_ROUTE"
SUBJECT_SCHEMA_VERSION = "pados.approval-subject.v1"

RouteSurface = Literal["CONTROL_API", "TASK_LOOP"]

APPROVAL_PROJECT_MISMATCH = "APPROVAL_PROJECT_MISMATCH"
APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
APPROVAL_SUBJECT_UNBOUND = "APPROVAL_SUBJECT_UNBOUND"
APPROVAL_SUBJECT_TAMPERED = "APPROVAL_SUBJECT_TAMPERED"
APPROVAL_REQUEST_MISMATCH = "APPROVAL_REQUEST_MISMATCH"
APPROVAL_ACTOR_MISMATCH = "APPROVAL_ACTOR_MISMATCH"
APPROVAL_SCOPE_MISMATCH = "APPROVAL_SCOPE_MISMATCH"
APPROVAL_SCOPE_VERSION_STALE = "APPROVAL_SCOPE_VERSION_STALE"
APPROVAL_SUBJECT_HASH_MISMATCH = "APPROVAL_SUBJECT_HASH_MISMATCH"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ApprovalSubjectActor(_Frozen):
    surfaces: tuple[RouteSurface, ...] = Field(min_length=1)
    worker_types: tuple[WorkerType, ...] = Field(min_length=1)


class ApprovalOperation(_Frozen):
    step_sequence: int | None = Field(default=None, gt=0)
    capability_id: CapabilityId
    command_id: str | None = Field(default=None, max_length=80)
    max_risk_level: RiskLevel


class WorkRouteSubject(_Frozen):
    kind: Literal["WORK_ROUTE"]
    schema_version: Literal["pados.approval-subject.v1"]
    project_id: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=80)
    actor: ApprovalSubjectActor
    operations: tuple[ApprovalOperation, ...] = Field(min_length=1, max_length=50)
    scope_version_ref: str | None = Field(default=None, pattern=r"^scope:[0-9]+$")
    task_envelope_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def subject_hash(subject: WorkRouteSubject) -> str:
    """Canonical SHA-256 of the subject; this is the approval's ``proposal_hash``."""
    return canonical_hash(subject.model_dump(mode="json"))


def load_bound_subject(subject_kind: Any, subject_json: Any, proposal_hash: Any) -> WorkRouteSubject:
    """Validate a stored subject against its hash; raise PermissionError(<code>) on failure."""
    if subject_kind != SUBJECT_KIND_WORK_ROUTE or subject_json is None:
        raise PermissionError(APPROVAL_SUBJECT_UNBOUND)
    if not isinstance(subject_json, dict) or canonical_hash(subject_json) != str(proposal_hash):
        raise PermissionError(APPROVAL_SUBJECT_TAMPERED)
    try:
        subject = WorkRouteSubject.model_validate(subject_json)
    except ValidationError as exc:
        raise PermissionError(APPROVAL_SUBJECT_TAMPERED) from exc
    if subject_hash(subject) != str(proposal_hash):
        raise PermissionError(APPROVAL_SUBJECT_TAMPERED)
    return subject


def find_operation(
    subject: WorkRouteSubject,
    *,
    step_sequence: int | None,
    capability_id: CapabilityId,
    command_id: str | None,
) -> ApprovalOperation | None:
    for operation in subject.operations:
        if (
            operation.step_sequence == step_sequence
            and operation.capability_id == capability_id
            and operation.command_id == command_id
        ):
            return operation
    return None


def match_route(
    subject: WorkRouteSubject,
    *,
    project_id: str,
    request_id: str,
    surface: str,
    step_sequence: int | None,
    capability_id: CapabilityId,
    command_id: str | None,
    risk_level: RiskLevel,
) -> str | None:
    """Return the refusal code for a route the subject does not authorize, else None."""
    if subject.project_id != project_id:
        return APPROVAL_PROJECT_MISMATCH
    if subject.request_id != request_id:
        return APPROVAL_REQUEST_MISMATCH
    if surface not in subject.actor.surfaces:
        return APPROVAL_ACTOR_MISMATCH
    operation = find_operation(
        subject, step_sequence=step_sequence, capability_id=capability_id, command_id=command_id
    )
    if operation is None or RISK_ORDER[risk_level] > RISK_ORDER[operation.max_risk_level]:
        return APPROVAL_SCOPE_MISMATCH
    return None


def scope_version_is_current(subject: WorkRouteSubject, current_version: int | None) -> bool:
    """A subject without a scope reference is unconstrained; otherwise the head must match."""
    if subject.scope_version_ref is None:
        return True
    return current_version is not None and subject.scope_version_ref == f"scope:{current_version}"
