# SPDX-License-Identifier: AGPL-3.0-only
"""User-facing disclosure projection for the first-product readback surface.

READ MODEL != SEMANTIC AUTHORITY, and this projection is one step further
removed again: it is a presentation view derived from the durable read model.

Canonical `ResultReceipt`, `ObjectRef`, run-context, snapshot and report records
are never mutated to hide presentation fields. This module only decides what a
normal authenticated readback discloses by default.

Kept, because it is product information the user needs:
    logical status, phase, attempt numbers, project-relative changed paths,
    bounded validation summaries, product-level failure explanations, the
    accepted result summary, semantic commit state, and contract timestamps.

Withheld by default, because it is physical or implementation detail:
    absolute workspace roots, physical approved-root locations, absolute or
    traversal-bearing allowed paths, object-store locations, raw storage URIs,
    and unrestricted exception text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pados.product_loop.schemas import (
    DurableProductRead,
    ReadAcceptedResult,
    ReadAttemptSummary,
    ReadContractSummary,
    ReadFailureSummary,
    ReadProgressStatus,
    ReadUnderstanding,
    ReadValidationSummary,
)

DISCLOSURE_POLICY = "P6_21M_PUBLIC_PROJECTION_V1"
WORKSPACE_SCOPE_LABEL = "APPROVED_WORKSPACE"
REDACTED_PATH = "[REDACTED_PATH]"
REDACTED_CRITERION = "<redacted-criterion>"

_WINDOWS_DRIVE_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])[^\s<>|\"']*")
_WINDOWS_UNC_PATH = re.compile(r"(?<![:/\w])(?:\\\\|//)[^\s<>|\"']*")
_FILE_URI = re.compile(r"file:/{2,3}[^\s<>|\"']*", re.IGNORECASE)
_POSIX_PATH = re.compile(
    r"(?<![\w./:])/(?!/)(?:[\w.\-+@~%]+(?:/[\w.\-+@~%]+)*)?"
)
_HOME_PATH = re.compile(r"(?<![\w.])~[\\/][^\s<>|\"']*")
_INTERNAL_STORAGE_URI = re.compile(
    r"(?:s3|gs|az|azure|object|storage|postgres(?:ql)?|sqlite|redis)://",
    re.IGNORECASE,
)


class _ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ViewObjectReference(_ViewModel):
    """Logical object identity without any physical storage location."""

    object_id: str
    content_hash: str
    media_type: str | None = None
    size_bytes: int | None = None
    object_kind: str | None = None


class ViewContractSummary(_ViewModel):
    work_contract_id: str
    version: int
    parent_contract_id: str | None = None
    objective: str
    workspace_scope: str = WORKSPACE_SCOPE_LABEL
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    state: str


class ViewAttemptSummary(_ViewModel):
    attempt: int
    contract_id: str
    current_phase: str
    runtime_status: str | None = None
    validation_status: str | None = None
    validation_passed: bool | None = None
    failure_classification: str | None = None
    failure_observation: str | None = None
    reflection_next_action: str | None = None
    revision_created: bool
    receipt_id: str | None = None
    input_scope_version: int | None = None
    input_manifest_hash: str | None = None
    output_content_hash: str | None = None
    changed_paths: tuple[str, ...]
    semantic_commit_status: str


class ViewFailureSummary(_ViewModel):
    attempt: int
    classification: str
    detail: str


class ViewAcceptedResult(_ViewModel):
    receipt_id: str
    input_scope_version: int
    input_manifest_hash: str
    output_content_hash: str | None = None
    changed_paths: tuple[str, ...]
    semantic_commit_status: str


class ProductReadView(_ViewModel):
    """Sanitized product readback. Derived, non-authoritative, presentation only."""

    disclosure_policy: str = DISCLOSURE_POLICY
    read_model_version: int
    run_id: str
    scope_id: str
    status: str
    progress_status: ReadProgressStatus
    current_phase: str
    started_at: str
    finished_at: str | None = None
    goal: str
    understanding: ReadUnderstanding
    authorized_contract: ViewContractSummary
    contracts: tuple[ViewContractSummary, ...]
    attempts: tuple[ViewAttemptSummary, ...]
    attempt_count: int = Field(ge=0)
    failed_attempts: tuple[ViewFailureSummary, ...]
    latest_validation: ReadValidationSummary | None = None
    accepted_result: ViewAcceptedResult | None = None
    semantic_commit_status: str
    verified_progress: str
    understanding_changes: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    human_action_required: bool
    automatic_continuation_allowed: bool
    user_report: str
    memory_authority: str = "CANDIDATE"
    graph_authority: str = "PROPOSAL_ONLY"
    semantic_authority: str = "READ_MODEL_IS_NOT_SEMANTIC_AUTHORITY"
    report_reference: ViewObjectReference | None = None
    withheld_fields: tuple[str, ...]


def is_physical_path(value: str) -> bool:
    """Classify a path as PHYSICAL/ABSOLUTE/STORAGE_INTERNAL rather than logical."""
    candidate = (value or "").strip()
    if not candidate:
        return True
    if "\x00" in candidate:
        return True
    if (
        _WINDOWS_DRIVE_PATH.match(candidate)
        or _WINDOWS_UNC_PATH.match(candidate)
        or _FILE_URI.match(candidate)
    ):
        return True
    if candidate.startswith(("/", "\\", "~")):
        return True
    normalized = candidate.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def sanitize_text(value: str | None) -> str | None:
    """Strip physical locations from free text while keeping the explanation."""
    if value is None:
        return None
    scrubbed = _FILE_URI.sub(REDACTED_PATH, value)
    scrubbed = _WINDOWS_DRIVE_PATH.sub(REDACTED_PATH, scrubbed)
    scrubbed = _WINDOWS_UNC_PATH.sub(REDACTED_PATH, scrubbed)
    scrubbed = _HOME_PATH.sub(REDACTED_PATH, scrubbed)
    return _POSIX_PATH.sub(REDACTED_PATH, scrubbed)


def sanitize_paths(values: Sequence[str]) -> tuple[str, ...]:
    """Keep project-relative paths; redact only physical/absolute ones."""
    return tuple(REDACTED_PATH if is_physical_path(item) else item for item in values)


def sanitize_criterion_identifier(value: str) -> str:
    """Redact physical or internal identifiers only in the public projection."""
    candidate = (value or "").strip()
    if is_physical_path(candidate) or _INTERNAL_STORAGE_URI.match(candidate):
        return REDACTED_CRITERION
    return value


def sanitize_object_reference(ref: Mapping[str, Any] | None) -> ViewObjectReference | None:
    """Keep logical object identity; drop every physical storage locator."""
    if ref is None:
        return None
    object_id = ref.get("object_id")
    content_hash = ref.get("content_hash")
    if object_id is None or content_hash is None:
        return None
    size = ref.get("size_bytes")
    return ViewObjectReference(
        object_id=str(object_id),
        content_hash=str(content_hash),
        media_type=None if ref.get("media_type") is None else str(ref["media_type"]),
        size_bytes=None if size is None else int(size),
        object_kind=None if ref.get("object_kind") is None else str(ref["object_kind"]),
    )


def build_read_view(read: DurableProductRead, *, scope_id: str) -> ProductReadView:
    """Project one durable read into its sanitized user-facing representation."""
    withheld = (
        "contract.workspace_root",
        "contract.physical_allowed_paths",
        "object_reference.storage_uri",
        "internal_exception_detail",
    )
    return ProductReadView(
        read_model_version=read.read_model_version,
        run_id=read.run_id,
        scope_id=scope_id,
        status=read.status,
        progress_status=read.progress_status,
        current_phase=read.current_phase,
        started_at=read.started_at,
        finished_at=read.finished_at,
        goal=_text(read.goal),
        understanding=_understanding(read.understanding),
        authorized_contract=_contract(read.authorized_contract),
        contracts=tuple(_contract(item) for item in read.contracts),
        attempts=tuple(_attempt(item) for item in read.attempts),
        attempt_count=read.attempt_count,
        failed_attempts=tuple(_failure(item) for item in read.failed_attempts),
        latest_validation=_validation(read.latest_validation),
        accepted_result=_accepted(read.accepted_result),
        semantic_commit_status=read.semantic_commit_status,
        verified_progress=_text(read.verified_progress),
        understanding_changes=tuple(_text(item) for item in read.understanding_changes),
        unresolved_issues=tuple(_text(item) for item in read.unresolved_issues),
        human_action_required=read.human_action_required,
        automatic_continuation_allowed=read.automatic_continuation_allowed,
        user_report=_text(read.user_report),
        report_reference=sanitize_object_reference(read.report_object_ref),
        withheld_fields=withheld,
    )


def _text(value: str) -> str:
    sanitized = sanitize_text(value)
    return "" if sanitized is None else sanitized


def _understanding(value: ReadUnderstanding) -> ReadUnderstanding:
    return value.model_copy(
        update={
            "objective": _text(value.objective),
            "approach": _text(value.approach),
            "accepted_direction": _text(value.accepted_direction),
        }
    )


def _contract(value: ReadContractSummary) -> ViewContractSummary:
    return ViewContractSummary(
        work_contract_id=value.work_contract_id,
        version=value.version,
        parent_contract_id=value.parent_contract_id,
        objective=_text(value.objective),
        allowed_paths=sanitize_paths(value.allowed_paths),
        acceptance_criteria=tuple(
            sanitize_criterion_identifier(item) for item in value.acceptance_criteria
        ),
        allowed_actions=tuple(value.allowed_actions),
        prohibited_actions=tuple(value.prohibited_actions),
        state=value.state,
    )


def _attempt(value: ReadAttemptSummary) -> ViewAttemptSummary:
    return ViewAttemptSummary(
        attempt=value.attempt,
        contract_id=value.contract_id,
        current_phase=value.current_phase,
        runtime_status=None if value.runtime_status is None else value.runtime_status.value,
        validation_status=None if value.validation_status is None else value.validation_status.value,
        validation_passed=value.validation_passed,
        failure_classification=(
            None if value.failure_classification is None else value.failure_classification.value
        ),
        failure_observation=sanitize_text(value.failure_observation),
        reflection_next_action=sanitize_text(value.reflection_next_action),
        revision_created=value.revision_created,
        receipt_id=value.receipt_id,
        input_scope_version=value.input_scope_version,
        input_manifest_hash=value.input_manifest_hash,
        output_content_hash=value.output_content_hash,
        changed_paths=sanitize_paths(value.changed_paths),
        semantic_commit_status=value.semantic_commit_status,
    )


def _failure(value: ReadFailureSummary) -> ViewFailureSummary:
    return ViewFailureSummary(
        attempt=value.attempt,
        classification=value.classification,
        detail=_text(value.detail),
    )


def _validation(value: ReadValidationSummary | None) -> ReadValidationSummary | None:
    if value is None:
        return None
    return value.model_copy(
        update={"criterion_statuses": tuple(_text(item) for item in value.criterion_statuses)}
    )


def _accepted(value: ReadAcceptedResult | None) -> ViewAcceptedResult | None:
    if value is None:
        return None
    return ViewAcceptedResult(
        receipt_id=value.receipt_id,
        input_scope_version=value.input_scope_version,
        input_manifest_hash=value.input_manifest_hash,
        output_content_hash=value.output_content_hash,
        changed_paths=sanitize_paths(value.changed_paths),
        semantic_commit_status=value.semantic_commit_status,
    )
