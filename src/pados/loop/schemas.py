# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from pados.worker.schemas import CapabilityId, RiskLevel, WorkerType


class GoalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class StepType(StrEnum):
    INSPECT = "INSPECT"
    ANALYZE = "ANALYZE"
    DRAFT = "DRAFT"
    MODIFY_DOCUMENT = "MODIFY_DOCUMENT"
    MODIFY_CODE = "MODIFY_CODE"
    RUN_VALIDATION = "RUN_VALIDATION"
    BUILD_ARTIFACT = "BUILD_ARTIFACT"
    AUDIT = "AUDIT"
    CHECKPOINT = "CHECKPOINT"
    HUMAN_DECISION = "HUMAN_DECISION"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    ROUTING = "ROUTING"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class LoopStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    READY = "READY"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class IterationStatus(StrEnum):
    CREATED = "CREATED"
    ROUTING = "ROUTING"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    DECIDED = "DECIDED"
    FAILED = "FAILED"


class DeltaClassification(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    INCOMPARABLE = "INCOMPARABLE"


class LoopDecision(StrEnum):
    CONTINUE = "CONTINUE"
    RETRY_SAME_STEP = "RETRY_SAME_STEP"
    ADVANCE_STEP = "ADVANCE_STEP"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    PAUSE = "PAUSE"
    BLOCK = "BLOCK"
    COMPLETE = "COMPLETE"
    PARTIAL_COMPLETE = "PARTIAL_COMPLETE"
    FAIL_RECOVERABLE = "FAIL_RECOVERABLE"
    FAIL_TERMINAL = "FAIL_TERMINAL"
    CANCEL = "CANCEL"


class TaskGoalCreate(BaseModel):
    project_id: str
    request_id: str
    objective: str = Field(min_length=1, max_length=1000)
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    scope_paths: list[str] = Field(default_factory=list, max_length=40)
    prohibited_actions: list[str] = Field(default_factory=lambda: ["merge", "push", "deploy"], max_length=40)
    risk_level: RiskLevel = RiskLevel.LOW
    approval_id: str
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("scope_paths", "prohibited_actions")
    @classmethod
    def _reject_shell_tokens(cls, values: list[str]) -> list[str]:
        for value in values:
            if any(token in value for token in ["..", ";", "&", "|", "`", "$("]):
                raise ValueError("unsafe scope or action token")
        return values


class PlanStepCreate(BaseModel):
    sequence: int = Field(gt=0)
    product_goal_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    step_type: StepType
    required_capability: CapabilityId
    preferred_worker_types: list[WorkerType] = Field(default_factory=lambda: [WorkerType.DETERMINISTIC_TOOL])
    dependencies: list[int] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["READ_REPOSITORY"])
    prohibited_actions: list[str] = Field(default_factory=lambda: ["merge", "push", "deploy"])
    context_requirements: list[str] = Field(default_factory=list)
    input_artifacts: list[str] = Field(default_factory=list)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    validation_policy_id: str = Field(default="READ_ONLY", max_length=80)
    command_id: str | None = Field(default=None, max_length=80)
    success_condition: str = Field(default="validation_passed", max_length=200)
    failure_policy: str = Field(default="fail_recoverable", max_length=80)
    timeout_seconds: int = Field(default=120, gt=0, le=600)
    risk_level: RiskLevel = RiskLevel.LOW
    approval_requirement: str = Field(default="PLAN_APPROVAL", max_length=80)

    @field_validator("allowed_paths", "allowed_actions", "prohibited_actions")
    @classmethod
    def _reject_injection_tokens(cls, values: list[str]) -> list[str]:
        for value in values:
            if any(token in value for token in ["..", ";", "&", "|", "`", "$("]):
                raise ValueError("unsafe plan step token")
        return values


class StopPolicy(BaseModel):
    max_iterations: int = Field(default=8, gt=0, le=100)
    max_attempts_per_step: int = Field(default=2, gt=0, le=20)
    max_worker_failures: int = Field(default=2, gt=0, le=20)
    max_execution_seconds: int = Field(default=1800, gt=0, le=86_400)
    max_context_tokens: int = Field(default=2400, gt=0, le=32_000)
    no_progress_limit: int = Field(default=2, gt=0, le=20)
    negative_progress_limit: int = Field(default=1, gt=0, le=20)
    require_progress_delta: bool = True
    stop_on_security_violation: bool = True
    stop_on_scope_change: bool = True
    stop_on_approval_expiry: bool = True


class TaskPlanCreate(BaseModel):
    task_goal_id: str
    approval_id: str
    assumptions: list[str] = Field(default_factory=list, max_length=40)
    steps: list[PlanStepCreate] = Field(min_length=1, max_length=50)
    stop_policy: StopPolicy = Field(default_factory=StopPolicy)
    validation_policy: dict[str, Any] = Field(default_factory=dict)
    rollback_policy: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=120)


class LoopCreate(BaseModel):
    task_goal_id: str
    active_plan_id: str
    approval_id: str
    max_iterations: int = Field(default=8, gt=0, le=100)
    max_replans: int = Field(default=0, ge=0, le=3)
    max_worker_failures: int = Field(default=2, gt=0, le=20)
    max_execution_seconds: int = Field(default=1800, gt=0, le=86_400)
    no_progress_limit: int = Field(default=2, gt=0, le=20)
    context_token_budget: int = Field(default=2400, gt=0, le=32_000)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ProgressSnapshot(BaseModel):
    acceptance_criteria_total: int = Field(ge=0)
    acceptance_criteria_met: int = Field(ge=0)
    tests_total: int = Field(ge=0)
    tests_passed: int = Field(ge=0)
    tests_failed: int = Field(ge=0)
    blocker_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    validation_status: str
    scope_hash: str
    state_hash: str


class ProgressDelta(BaseModel):
    criteria_met_delta: int
    tests_passed_delta: int
    tests_failed_delta: int
    blocker_delta: int
    artifact_delta: int
    scope_changed: bool
    state_changed: bool
    progress_score: int
    classification: DeltaClassification
