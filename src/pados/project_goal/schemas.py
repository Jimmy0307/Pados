# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class GoalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    AT_RISK = "AT_RISK"
    WAITING_HUMAN = "WAITING_HUMAN"
    ACHIEVED = "ACHIEVED"
    PARTIALLY_ACHIEVED = "PARTIALLY_ACHIEVED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    AT_RISK = "AT_RISK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ProjectTaskStatus(StrEnum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ProjectLoopStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    READY = "READY"
    ACTIVE = "ACTIVE"
    EVALUATING = "EVALUATING"
    PLANNING = "PLANNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    AT_RISK = "AT_RISK"
    BLOCKED = "BLOCKED"
    ACHIEVED = "ACHIEVED"
    PARTIALLY_ACHIEVED = "PARTIALLY_ACHIEVED"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class DeltaClassification(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    INCOMPARABLE = "INCOMPARABLE"


class ProjectDecision(StrEnum):
    CONTINUE_ACTIVE_TASKS = "CONTINUE_ACTIVE_TASKS"
    ACTIVATE_NEXT_TASK = "ACTIVATE_NEXT_TASK"
    PROPOSE_NEW_TASKS = "PROPOSE_NEW_TASKS"
    PROPOSE_REPLAN = "PROPOSE_REPLAN"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    PAUSE = "PAUSE"
    MARK_AT_RISK = "MARK_AT_RISK"
    MARK_BLOCKED = "MARK_BLOCKED"
    ACHIEVE = "ACHIEVE"
    PARTIAL_ACHIEVE = "PARTIAL_ACHIEVE"
    FAIL_RECOVERABLE = "FAIL_RECOVERABLE"
    FAIL_TERMINAL = "FAIL_TERMINAL"
    CANCEL = "CANCEL"


class ProjectBudget(BaseModel):
    max_active_tasks: int = Field(default=2, gt=0, le=100)
    max_total_tasks: int = Field(default=20, gt=0, le=10_000)
    max_task_loops: int = Field(default=30, gt=0, le=10_000)
    max_worker_jobs: int = Field(default=100, gt=0, le=100_000)
    max_project_cycles: int = Field(default=50, gt=0, le=100_000)
    max_replans: int = Field(default=4, gt=0, le=100)
    max_execution_seconds: int = Field(default=3600, gt=0, le=31_536_000)
    max_context_tokens: int = Field(default=8000, gt=0, le=1_000_000)
    max_artifact_bytes: int = Field(default=100_000_000, gt=0, le=100_000_000_000)
    max_risk_score: int = Field(default=100, gt=0, le=10_000)


class ProjectProgressSnapshot(BaseModel):
    success_criteria_total: int = Field(ge=0)
    success_criteria_met: int = Field(ge=0)
    success_criteria_partially_met: int = Field(ge=0)
    task_total: int = Field(ge=0)
    task_proposed: int = Field(ge=0)
    task_approved: int = Field(ge=0)
    task_active: int = Field(ge=0)
    task_completed: int = Field(ge=0)
    task_failed: int = Field(ge=0)
    task_blocked: int = Field(ge=0)
    loops_active: int = Field(ge=0)
    loops_completed: int = Field(ge=0)
    loops_failed: int = Field(ge=0)
    validated_artifacts: int = Field(ge=0)
    unresolved_blockers: int = Field(ge=0)
    open_risks: int = Field(ge=0)
    budget_consumed: int = Field(ge=0)
    budget_remaining: int = Field(ge=0)
    time_consumed_seconds: int = Field(ge=0)
    time_remaining_seconds: int = Field(ge=0)
    plan_version: int = Field(gt=0)
    goal_version: int = Field(gt=0)
    scope_hash: str = Field(min_length=1, max_length=128)
    state_hash: str = Field(min_length=1, max_length=128)
    evidence_hash: str = Field(min_length=1, max_length=128)


class ProjectProgressDelta(BaseModel):
    criteria_met_delta: int
    task_completed_delta: int
    task_failed_delta: int
    blocker_delta: int
    risk_delta: int
    budget_delta: int
    artifact_delta: int
    scope_changed: bool
    plan_changed: bool
    goal_changed: bool
    classification: DeltaClassification


class ReplanHistoryEntry(BaseModel):
    cycle: int = Field(ge=0)
    plan_hash: str = Field(min_length=1, max_length=128)
    inverse_plan_hash: str | None = Field(default=None, max_length=128)
    task_set_hash: str = Field(min_length=1, max_length=128)
    dependency_hash: str = Field(min_length=1, max_length=128)
    priority_hash: str = Field(min_length=1, max_length=128)


class ProjectGoalCreate(BaseModel):
    project_id: str
    objective: str = Field(min_length=1, max_length=2000)
    desired_outcomes: list[str] = Field(min_length=1, max_length=50)
    success_criteria: list[str] = Field(min_length=1, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    scope_paths: list[str] = Field(default_factory=list, max_length=100)
    prohibited_actions: list[str] = Field(default_factory=lambda: ["merge", "push", "deploy"], max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    risk_budget: dict[str, int] = Field(default_factory=lambda: {"max_risk_score": 100})
    execution_budget: ProjectBudget = Field(default_factory=ProjectBudget)
    time_horizon_seconds: int = Field(default=86_400, gt=0, le=31_536_000)
    planning_horizon: str = Field(default="BOUNDED", min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("scope_paths", "prohibited_actions")
    @classmethod
    def reject_unsafe_tokens(cls, values: list[str]) -> list[str]:
        if any(any(token in value for token in ("..", ";", "&", "|", "`", "$(")) for value in values):
            raise ValueError("unsafe project goal token")
        return values


class ProjectPlanCreate(BaseModel):
    project_goal_id: str
    planning_horizon: str = Field(min_length=1, max_length=120)
    strategy_summary: str = Field(min_length=1, max_length=4000)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    priorities: list[str] = Field(default_factory=list, max_length=100)
    task_graph: dict[str, Any] = Field(default_factory=dict)
    dependency_graph: dict[str, Any] = Field(default_factory=dict)
    critical_path: list[str] = Field(default_factory=list, max_length=100)
    review_policy: dict[str, Any] = Field(default_factory=dict)
    replan_policy: dict[str, Any] = Field(default_factory=dict)
    stop_policy: dict[str, Any] = Field(default_factory=dict)
    risk_controls: dict[str, Any] = Field(default_factory=dict)
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ProjectTaskCreate(BaseModel):
    project_plan_id: str
    project_goal_id: str
    project_id: str
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=2000)
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=40)
    scope_paths: list[str] = Field(default_factory=list, max_length=40)
    priority: int = Field(gt=0, le=10_000)
    risk_level: str = Field(pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")
    estimated_cost: int = Field(ge=0, le=1_000_000)
    estimated_duration_seconds: int = Field(gt=0, le=604_800)
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    task_goal_template: dict[str, Any]
    task_plan_template: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=120)


class ProjectLoopCreate(BaseModel):
    project_goal_id: str
    active_plan_id: str
    project_id: str
    stop_policy: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=120)
