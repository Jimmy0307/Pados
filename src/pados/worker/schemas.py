# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CapabilityId(StrEnum):
    READ_REPOSITORY = "READ_REPOSITORY"
    SEARCH_CONTEXT = "SEARCH_CONTEXT"
    SUMMARIZE = "SUMMARIZE"
    CLASSIFY_REQUEST = "CLASSIFY_REQUEST"
    GENERATE_DOCUMENT_DRAFT = "GENERATE_DOCUMENT_DRAFT"
    MODIFY_MARKDOWN = "MODIFY_MARKDOWN"
    MODIFY_CODE = "MODIFY_CODE"
    RUN_STATIC_ANALYSIS = "RUN_STATIC_ANALYSIS"
    RUN_TESTS = "RUN_TESTS"
    RUN_MIGRATIONS = "RUN_MIGRATIONS"
    BUILD_RELEASE = "BUILD_RELEASE"
    AUDIT_RELEASE = "AUDIT_RELEASE"
    CREATE_ARTIFACT = "CREATE_ARTIFACT"


DENIED_CAPABILITIES = {
    "DEPLOY",
    "SEND_EMAIL",
    "MODIFY_EXTERNAL_SYSTEM",
    "DELETE_PRODUCTION_DATA",
    "RELEASE_PRODUCTION",
}


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


RISK_ORDER = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}


class WorkerType(StrEnum):
    DETERMINISTIC_TOOL = "DETERMINISTIC_TOOL"
    OLLAMA = "OLLAMA"
    CLAUDE_CODE = "CLAUDE_CODE_RUNTIME"
    CODEX_CLI = "CODEX_CLI"


class WorkerAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    DISABLED = "DISABLED"


class JobStatus(StrEnum):
    READY_FOR_ROUTING = "READY_FOR_ROUTING"
    ROUTING = "ROUTING"
    ROUTED = "ROUTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


class ErrorCode(StrEnum):
    NO_ELIGIBLE_WORKER = "NO_ELIGIBLE_WORKER"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    WORKER_AUTH_REQUIRED = "WORKER_AUTH_REQUIRED"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
    POLICY_DENIED = "POLICY_DENIED"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    OUTPUT_CONTRACT_FAILED = "OUTPUT_CONTRACT_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    TIMED_OUT = "TIMED_OUT"


class IsolationType(StrEnum):
    READ_ONLY = "READ_ONLY"
    TEMP_DIRECTORY = "TEMP_DIRECTORY"
    GIT_WORKTREE = "GIT_WORKTREE"


class WorkerSpec(BaseModel):
    worker_id: str
    worker_type: WorkerType
    display_name: str
    version: str | None = None
    availability: WorkerAvailability
    capabilities: list[CapabilityId]
    risk_ceiling: RiskLevel
    supports_structured_output: bool
    supports_file_changes: bool
    supports_tool_calls: bool
    supports_streaming: bool
    max_context_tokens: int
    default_timeout_seconds: int
    reason: str = ""


class CapabilitySpec(BaseModel):
    capability_id: CapabilityId
    description: str
    minimum_risk_level: RiskLevel
    required_approval_type: str
    requires_isolation: bool
    requires_validation: bool
    allowed_worker_types: list[WorkerType]
    default_timeout: int


class RouteRequest(BaseModel):
    approval_id: str
    capability_id: CapabilityId
    risk_level: RiskLevel = RiskLevel.LOW
    context_query: str = Field(min_length=1, max_length=1000)
    allowed_actions: list[str] = Field(default_factory=lambda: ["READ_REPOSITORY"])
    prohibited_actions: list[str] = Field(default_factory=lambda: ["merge", "push", "release", "deploy"])
    allowed_paths: list[str] = Field(default_factory=list)
    validation_policy_id: str = Field(default="READ_ONLY", max_length=80)
    command_id: str | None = Field(default=None, max_length=80)
    timeout_seconds: int | None = Field(default=None, gt=0, le=600)
    idempotency_key: str = Field(min_length=1, max_length=120)
    # Requesting surface and plan step the approval subject must cover (RC2 D1). The control
    # API forces CONTROL_API / None; only the P6.10 loop sets TASK_LOOP and a root step.
    surface: Literal["CONTROL_API", "TASK_LOOP"] = "CONTROL_API"
    step_sequence: int | None = Field(default=None, gt=0)

    @field_validator("allowed_actions", "prohibited_actions")
    @classmethod
    def _safe_words(cls, value: list[str]) -> list[str]:
        for item in value:
            if any(ch in item for ch in [";", "&", "|", "`", "$("]):
                raise ValueError("unsafe action token")
        return value


class WorkerHandoff(BaseModel):
    job_id: str
    project_id: str
    request: dict[str, Any]
    success_criteria: str
    constraints: list[str]
    allowed_actions: list[str]
    prohibited_actions: list[str]
    relevant_facts: list[dict[str, Any]]
    locked_decisions: list[dict[str, Any]]
    recent_failures: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    allowed_paths: list[str]
    validation_commands: list[str]
    output_contract: dict[str, Any]
    packet_hash: str
    token_estimate: int
    provenance: dict[str, Any]


class WorkerResult(BaseModel):
    job_id: str
    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"]
    started_at: str
    completed_at: str
    exit_code: int | None
    result_summary: str
    structured_result: dict[str, Any]
    stdout_path: str | None
    stderr_path: str | None
    artifact_manifest: list[dict[str, Any]]
    changed_files: list[str]
    validation_result: dict[str, Any]
    usage_metadata: dict[str, Any]
    error_code: str | None = None


class CommandInvocation(BaseModel):
    command_id: str
    arguments: dict[str, str] = Field(default_factory=dict)
    working_directory: Path
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int
