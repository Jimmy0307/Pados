# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractState(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    AUTHORIZED = "AUTHORIZED"
    DISPATCHED = "DISPATCHED"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VALIDATING = "VALIDATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    CANCELLED = "CANCELLED"


class ProductLoopStatus(str, Enum):
    AUTHORITY_BLOCK = "AUTHORITY_BLOCK"
    REJECTED = "REJECTED"
    COMMITTED = "COMMITTED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    STALE_CANDIDATE = "STALE_CANDIDATE"


class RuntimeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ValidationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class ValidationOperation(str, Enum):
    TEST = "TEST"
    EVIDENCE_VALIDATE = "EVIDENCE_VALIDATE"
    ACCEPTANCE_CHECK = "ACCEPTANCE_CHECK"
    REVIEW = "REVIEW"


class CheckKind(str, Enum):
    FILE_EXISTS = "FILE_EXISTS"
    FILE_CONTAINS = "FILE_CONTAINS"
    PYTHON_COMPILE = "PYTHON_COMPILE"
    CHANGED_PATHS_SCOPED = "CHANGED_PATHS_SCOPED"


class FailureClass(str, Enum):
    IMPLEMENTATION_DEFECT = "IMPLEMENTATION_DEFECT"
    DESIGN_DEFECT = "DESIGN_DEFECT"
    ASSUMPTION_DEFECT = "ASSUMPTION_DEFECT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    RUNTIME_FAILURE = "RUNTIME_FAILURE"
    AUTHORITY_BLOCK = "AUTHORITY_BLOCK"
    STALE_CONTEXT = "STALE_CONTEXT"


class GoalRequest(_FrozenModel):
    goal: str = Field(min_length=1, max_length=2_000)
    workspace_root: str = Field(min_length=1, max_length=1_000)
    scope_id: str = Field(default="p6-21g-product-scope", min_length=1, max_length=160)
    changes: list[FileChange] = Field(min_length=1, max_length=8)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=8)
    revision_changes: list[FileChange] = Field(default_factory=list, max_length=8)
    authorized: bool = False
    human_gate: bool = False
    max_attempts: int = Field(default=1, ge=1, le=3)
    idempotency_key: str = Field(default_factory=lambda: f"p6-21g-{uuid4()}", max_length=200)
    context: dict[str, Any] = Field(default_factory=dict)
    # Optional reference to an existing APPROVED control approval for this
    # scope. When present it is verified before dispatch and bound into the
    # WorkContract and semantic audit; it never replaces authorization or CAS.
    approval_ref: str | None = Field(default=None, min_length=1, max_length=64)


class FileChange(_FrozenModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=100_000)

    @field_validator("path")
    @classmethod
    def relative_path_only(cls, value: str) -> str:
        candidate = value.replace("\\", "/")
        if candidate.startswith("/") or PureWindowsPath(value).is_absolute() or Path(value).is_absolute():
            raise ValueError("FILE_CHANGE_PATH_MUST_BE_RELATIVE")
        if any(part == ".." for part in candidate.split("/")):
            raise ValueError("FILE_CHANGE_PATH_MUST_NOT_ESCAPE_SCOPE")
        if not candidate or candidate == ".":
            raise ValueError("FILE_CHANGE_PATH_REQUIRED")
        return candidate


class AcceptanceCriterion(_FrozenModel):
    criterion_id: str = Field(min_length=1, max_length=120)
    operation: ValidationOperation = ValidationOperation.ACCEPTANCE_CHECK
    check: CheckKind
    target_path: str | None = Field(default=None, max_length=240)
    expected_text: str | None = Field(default=None, max_length=20_000)
    required: bool = True

    @model_validator(mode="after")
    def validate_target(self) -> AcceptanceCriterion:
        if self.check in {CheckKind.FILE_EXISTS, CheckKind.FILE_CONTAINS, CheckKind.PYTHON_COMPILE}:
            if not self.target_path:
                raise ValueError("VALIDATION_TARGET_PATH_REQUIRED")
            FileChange(path=self.target_path, content="")
        if self.check == CheckKind.FILE_CONTAINS and self.expected_text is None:
            raise ValueError("VALIDATION_EXPECTED_TEXT_REQUIRED")
        return self


class ScopeSpec(_FrozenModel):
    workspace_root: str = Field(min_length=1, max_length=1_000)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=8)
    isolated: bool = True


class Goal(_FrozenModel):
    goal_id: str
    objective: str
    scope_id: str


class Intent(_FrozenModel):
    intent_id: str
    original_request: str
    objective: str
    scope_id: str


class CognitiveComposition(_FrozenModel):
    composition_id: str
    version: int = Field(ge=1)
    selected_capabilities: list[str] = Field(min_length=1, max_length=6)
    dependencies: list[str] = Field(min_length=1, max_length=8)
    evidence_requirements: list[str] = Field(min_length=1, max_length=8)
    stop_conditions: list[str] = Field(min_length=1, max_length=8)
    max_rounds: int = Field(ge=1, le=3)
    handoff_conditions: list[str] = Field(min_length=1, max_length=8)
    risk_policy: str


class Design(_FrozenModel):
    design_id: str
    approach: str
    alternatives_rejected: list[str] = Field(default_factory=list, max_length=4)
    acceptance_shape: list[str] = Field(min_length=1, max_length=8)
    constraints: list[str] = Field(min_length=1, max_length=8)


class Decision(_FrozenModel):
    decision_id: str
    accepted_direction: str
    authority: str
    human_gate_required: bool
    base_scope_version: int = Field(ge=0)
    rationale: str


class RetryPolicy(_FrozenModel):
    max_attempts: int = Field(ge=1, le=3)
    allow_bounded_revision: bool = False
    max_revisions: int = Field(default=0, ge=0, le=2)


class WorkContract(_FrozenModel):
    work_contract_id: str
    version: int = Field(ge=1)
    parent_contract_id: str | None = None
    parent_goal: str
    intent: Intent
    decision_ref: str
    objective: str
    scope: ScopeSpec
    constraints: tuple[str, ...] = Field(min_length=1, max_length=12)
    context_ref: str
    context_fingerprint: str
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    semantic_base_scope_version: int = Field(ge=0)
    required_capabilities: tuple[str, ...] = Field(min_length=1, max_length=8)
    skill_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    mcp_capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    secret_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    allowed_actions: tuple[str, ...] = Field(min_length=1, max_length=8)
    prohibited_actions: tuple[str, ...] = Field(min_length=1, max_length=16)
    side_effect_policy: str
    implementation_class: str
    runtime_policy: str
    changes: tuple[FileChange, ...] = Field(min_length=1, max_length=8)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=8)
    validation_policy: str
    retry_policy: RetryPolicy
    idempotency_key: str
    correlation_id: str
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=12)
    human_gate_required: bool = False
    approval_ref: str | None = None
    rollback_ref: str | None = None
    state: ContractState = ContractState.AUTHORIZED


class RuntimeResult(_FrozenModel):
    run_id: str
    attempt: int = Field(ge=1, le=3)
    runtime_id: str
    status: RuntimeStatus
    started_at: str
    completed_at: str
    summary: str
    changed_paths: list[str] = Field(default_factory=list, max_length=8)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    log_refs: list[str] = Field(default_factory=list, max_length=8)
    validation_observations: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    failure_classification: FailureClass | None = None
    retryable: bool = False
    completion_declared: bool = False
    execution_receipt: dict[str, Any] | None = None


class CriterionFinding(_FrozenModel):
    criterion_id: str
    status: ValidationStatus
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(_FrozenModel):
    validation_id: str
    status: ValidationStatus
    criterion_findings: list[CriterionFinding] = Field(default_factory=list, max_length=8)
    technical_test_passed: bool | None = None
    validation_passed: bool
    semantic_accepted: bool = False
    human_approved: bool = False
    failure_classification: FailureClass | None = None


class Reflection(_FrozenModel):
    reflection_id: str
    attempt: int
    failure_classification: FailureClass | None
    progress: str
    next_action: str
    revision_created: bool = False
    stop_reason: str | None = None


class ReceiptRecord(_FrozenModel):
    receipt_id: str
    status: str
    runtime_status: RuntimeStatus
    failure_classification: FailureClass | None = None
    input_scope_version: int
    input_manifest_hash: str
    output_content_hash: str | None = None
    commit: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None


class AttemptRecord(_FrozenModel):
    attempt: int
    contract_id: str
    runtime_result: RuntimeResult
    receipt: ReceiptRecord
    validation: ValidationReport


class ProductLoopReport(_FrozenModel):
    run_id: str
    status: ProductLoopStatus
    goal: Goal
    intent: Intent
    composition: CognitiveComposition
    design: Design
    decision: Decision
    contracts: list[WorkContract] = Field(min_length=1, max_length=3)
    attempts: list[AttemptRecord] = Field(default_factory=list, max_length=3)
    reflections: list[Reflection] = Field(default_factory=list, max_length=3)
    final_result: RuntimeResult | None = None
    final_validation: ValidationReport | None = None
    user_report: str
    memory_authority: str = "CANDIDATE"
    graph_authority: str = "PROPOSAL_ONLY"
    semantic_state: str
    commit: dict[str, Any] | None = None
    human_decision_required: bool = False
    report_object_ref: dict[str, Any] | None = None


class ReadProgressStatus(str, Enum):
    REQUESTED = "REQUESTED"
    WORKING = "WORKING"
    FAILED = "FAILED"
    CORRECTED = "CORRECTED"
    VERIFIED = "VERIFIED"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    ITERATION_LIMIT = "ITERATION_LIMIT"


class ReadUnderstanding(_FrozenModel):
    intent_id: str
    objective: str
    selected_capabilities: tuple[str, ...]
    approach: str
    accepted_direction: str


class ReadContractSummary(_FrozenModel):
    work_contract_id: str
    version: int
    parent_contract_id: str | None = None
    objective: str
    workspace_root: str
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    state: str


class ReadFailureSummary(_FrozenModel):
    attempt: int
    classification: str
    detail: str


class ReadValidationSummary(_FrozenModel):
    attempt: int
    status: str
    validation_passed: bool
    technical_test_passed: bool | None
    criterion_statuses: tuple[str, ...]


class ReadAttemptSummary(_FrozenModel):
    attempt: int
    contract_id: str
    current_phase: str
    runtime_status: RuntimeStatus | None
    validation_status: ValidationStatus | None
    validation_passed: bool | None
    failure_classification: FailureClass | None
    failure_observation: str | None
    reflection_next_action: str | None
    revision_created: bool
    receipt_id: str | None
    input_scope_version: int | None
    input_manifest_hash: str | None
    output_content_hash: str | None
    changed_paths: tuple[str, ...]
    semantic_commit_status: str


class ReadAcceptedResult(_FrozenModel):
    receipt_id: str
    input_scope_version: int
    input_manifest_hash: str
    output_content_hash: str | None
    changed_paths: tuple[str, ...]
    semantic_commit_status: str


class DurableProductRead(_FrozenModel):
    """Small rebuildable user read model; never a semantic authority."""

    read_model_version: int = 1
    run_id: str
    status: str
    progress_status: ReadProgressStatus
    current_phase: str
    started_at: str
    finished_at: str | None = None
    goal: str
    understanding: ReadUnderstanding
    authorized_contract: ReadContractSummary
    contracts: tuple[ReadContractSummary, ...]
    attempts: tuple[ReadAttemptSummary, ...]
    attempt_count: int = Field(ge=0)
    failed_attempts: tuple[ReadFailureSummary, ...]
    latest_validation: ReadValidationSummary | None = None
    accepted_result: ReadAcceptedResult | None = None
    semantic_commit_status: str
    verified_progress: str
    understanding_changes: tuple[str, ...]
    unresolved_issues: tuple[str, ...]
    human_action_required: bool
    automatic_continuation_allowed: bool
    user_report: str
    report_object_ref: dict[str, Any] | None = None
