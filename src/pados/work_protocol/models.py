# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical V1 work-protocol schemas.

Provider response shapes are deliberately absent. Every executable result is
bound to PADOS authorization, project, classification, and ScopeVersion
metadata supplied by the trusted adapter boundary.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.enterprise.classification import DataClassification, parse_classification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_no_credentials

PROTOCOL_MAJOR = 1
SUPPORTED_PROTOCOL_MINOR = 1
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_CAPABILITY = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PROTOCOL_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ProtocolModel(BaseModel):
    """Immutable protocol document with additive-field round-trip support."""

    model_config = ConfigDict(frozen=True, extra="allow", str_max_length=16_000)

    SCHEMA: ClassVar[str]

    protocol_version: str
    schema_version: str

    @field_validator("protocol_version")
    @classmethod
    def protocol_version_supported(cls, value: str) -> str:
        match = _PROTOCOL_VERSION.fullmatch(value)
        if match is None or int(match.group(1)) != PROTOCOL_MAJOR:
            raise ValueError("PROTOCOL_MAJOR_UNSUPPORTED")
        if int(match.group(2)) > SUPPORTED_PROTOCOL_MINOR:
            raise ValueError("PROTOCOL_MINOR_UNSUPPORTED")
        return value

    @field_validator("schema_version")
    @classmethod
    def schema_version_matches(cls, value: str) -> str:
        expected = getattr(cls, "SCHEMA", None)
        if expected is not None and value != expected:
            raise ValueError("PROTOCOL_SCHEMA_UNSUPPORTED")
        return value

    @model_validator(mode="after")
    def no_credential_material(self) -> ProtocolModel:
        assert_no_credentials(self.model_dump(mode="json"))
        return self

    @property
    def content_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class GoalState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"


class GoalRelationType(StrEnum):
    PARENT_OF = "PARENT_OF"
    DEPENDS_ON = "DEPENDS_ON"
    BLOCKED_BY = "BLOCKED_BY"
    DERIVED_FROM = "DERIVED_FROM"
    RESIDUAL_OF = "RESIDUAL_OF"


class TaskPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class DecompositionStatus(StrEnum):
    PENDING = "PENDING"
    PROPOSED = "PROPOSED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ACCEPTED = "ACCEPTED"


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class UnresolvedReason(StrEnum):
    CAPABILITY_LIMIT = "CAPABILITY_LIMIT"
    MISSING_TOOL = "MISSING_TOOL"
    MISSING_DATA = "MISSING_DATA"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    QUALITY_INSUFFICIENT = "QUALITY_INSUFFICIENT"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    POLICY_RESTRICTION = "POLICY_RESTRICTION"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"


class ResourceKind(StrEnum):
    LOCAL_MODEL = "LOCAL_MODEL"
    CLOUD_MODEL = "CLOUD_MODEL"
    DETERMINISTIC_ALGORITHM = "DETERMINISTIC_ALGORITHM"
    OCR_ENGINE = "OCR_ENGINE"
    VISION_ENGINE = "VISION_ENGINE"
    TOOL = "TOOL"
    MCP = "MCP"
    OFFICE = "OFFICE"
    HUMAN = "HUMAN"
    COMPUTE_NODE = "COMPUTE_NODE"


class VerificationStatus(StrEnum):
    NOT_VERIFIED = "NOT_VERIFIED"
    PASSED = "PASSED"
    FAILED = "FAILED"


class Goal(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.goal.v1"

    goal_id: str
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2_000)
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=16)
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    state: GoalState = GoalState.PENDING

    @field_validator("goal_id", "dependencies")
    @classmethod
    def ids_are_bounded(cls, value: Any) -> Any:
        values = (value,) if isinstance(value, str) else value
        if any(not isinstance(item, str) or not _ID.fullmatch(item) for item in values):
            raise ValueError("GOAL_ID_INVALID")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def capabilities_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CAPABILITY.fullmatch(item) for item in value):
            raise ValueError("CAPABILITY_ID_INVALID")
        if len(set(value)) != len(value):
            raise ValueError("GOAL_CAPABILITY_DUPLICATE")
        return value

    @model_validator(mode="after")
    def no_self_dependency(self) -> Goal:
        if self.goal_id in self.dependencies:
            raise ValueError("GOAL_SELF_DEPENDENCY")
        return self


class GoalRelation(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.goal-relation.v1"

    source_goal_id: str
    target_goal_id: str
    relation: GoalRelationType

    @field_validator("source_goal_id", "target_goal_id")
    @classmethod
    def relation_ids_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("GOAL_RELATION_ID_INVALID")
        return value

    @model_validator(mode="after")
    def relation_is_not_reflexive(self) -> GoalRelation:
        if self.source_goal_id == self.target_goal_id:
            raise ValueError("GOAL_RELATION_SELF_REFERENCE")
        return self


def _validate_goal_graph(goals: tuple[Goal, ...], relations: tuple[GoalRelation, ...]) -> None:
    ids = [goal.goal_id for goal in goals]
    if len(ids) != len(set(ids)):
        raise ValueError("GOAL_ID_DUPLICATE")
    known = set(ids)
    for goal in goals:
        unknown = set(goal.dependencies) - known
        if unknown:
            raise ValueError("GOAL_DEPENDENCY_UNKNOWN")
    for relation in relations:
        if relation.source_goal_id not in known:
            raise ValueError("GOAL_RELATION_SOURCE_UNKNOWN")
        if relation.target_goal_id not in known:
            raise ValueError("GOAL_RELATION_TARGET_UNKNOWN")

    graph: dict[str, set[str]] = {goal_id: set() for goal_id in ids}
    for goal in goals:
        graph[goal.goal_id].update(goal.dependencies)
    for relation in relations:
        if relation.relation in {GoalRelationType.DEPENDS_ON, GoalRelationType.BLOCKED_BY}:
            graph[relation.source_goal_id].add(relation.target_goal_id)
        elif relation.relation is GoalRelationType.PARENT_OF:
            graph[relation.target_goal_id].add(relation.source_goal_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(goal_id: str) -> None:
        if goal_id in visiting:
            raise ValueError("GOAL_GRAPH_CYCLE")
        if goal_id in visited:
            return
        visiting.add(goal_id)
        for dependency in graph[goal_id]:
            visit(dependency)
        visiting.remove(goal_id)
        visited.add(goal_id)

    for goal_id in ids:
        visit(goal_id)


class TaskEnvelope(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.task-envelope.v1"

    task_id: str
    original_intent: str = Field(min_length=1, max_length=8_000)
    decomposition_status: DecompositionStatus = DecompositionStatus.PENDING
    normalized_goals: tuple[Goal, ...] = Field(min_length=1, max_length=128)
    goal_relations: tuple[GoalRelation, ...] = Field(default=(), max_length=512)
    constraints: tuple[str, ...] = Field(default=(), max_length=64)
    project_ref: str
    classification: DataClassification
    priority: TaskPriority
    deadline: datetime | None
    scope_version_ref: str
    created_by: str
    created_at: datetime

    @field_validator("task_id", "project_ref", "scope_version_ref", "created_by")
    @classmethod
    def task_ids_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("TASK_IDENTIFIER_INVALID")
        return value

    @field_validator("classification", mode="before")
    @classmethod
    def classification_is_known(cls, value: object) -> DataClassification:
        return parse_classification(value)

    @field_validator("created_at", "deadline")
    @classmethod
    def datetime_is_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("TASK_DATETIME_MUST_BE_TIMEZONE_AWARE")
        return value

    @field_validator("constraints")
    @classmethod
    def constraint_text_is_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 1_000 for item in value):
            raise ValueError("TASK_CONSTRAINT_INVALID")
        return value

    @model_validator(mode="after")
    def graph_is_valid(self) -> TaskEnvelope:
        _validate_goal_graph(self.normalized_goals, self.goal_relations)
        return self


class AuthorizationMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_ref: str
    classification: DataClassification
    scope_version_ref: str
    grant_ref: str
    policy_decision_ref: str
    capability: str

    @field_validator("classification", mode="before")
    @classmethod
    def classification_is_known(cls, value: object) -> DataClassification:
        return parse_classification(value)

    @field_validator("project_ref", "scope_version_ref", "grant_ref", "policy_decision_ref")
    @classmethod
    def authorization_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("AUTHORIZATION_REF_INVALID")
        return value

    @field_validator("capability")
    @classmethod
    def authorization_capability_is_bounded(cls, value: str) -> str:
        if not _CAPABILITY.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value


class ArtifactReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    digest: str
    project_ref: str
    classification: DataClassification

    @field_validator("artifact_id", "project_ref")
    @classmethod
    def artifact_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("ARTIFACT_REF_INVALID")
        return value

    @field_validator("digest")
    @classmethod
    def artifact_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("ARTIFACT_DIGEST_INVALID")
        return value

    @field_validator("classification", mode="before")
    @classmethod
    def classification_is_known(cls, value: object) -> DataClassification:
        return parse_classification(value)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    digest: str
    source_ref: str

    @field_validator("evidence_id", "source_ref")
    @classmethod
    def evidence_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("EVIDENCE_REF_INVALID")
        return value

    @field_validator("digest")
    @classmethod
    def evidence_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("EVIDENCE_DIGEST_INVALID")
        return value


class ExecutionReceiptRef(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.execution-receipt-ref.v1"

    receipt_id: str
    digest: str

    @field_validator("receipt_id")
    @classmethod
    def receipt_id_is_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("RECEIPT_REF_INVALID")
        return value

    @field_validator("digest")
    @classmethod
    def receipt_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("RECEIPT_DIGEST_INVALID")
        return value


class ResultReceiptRef(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.result-receipt-ref.v1"

    receipt_id: str
    candidate_id: str
    task_id: str
    project_ref: str
    classification: DataClassification
    digest: str
    scope_version_ref: str

    @field_validator("receipt_id", "candidate_id", "task_id", "project_ref", "scope_version_ref")
    @classmethod
    def result_receipt_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("RESULT_RECEIPT_REF_INVALID")
        return value

    @field_validator("classification", mode="before")
    @classmethod
    def result_receipt_classification_is_known(cls, value: object) -> DataClassification:
        return parse_classification(value)

    @field_validator("digest")
    @classmethod
    def result_receipt_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("RESULT_RECEIPT_REF_INVALID")
        return value


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_id: str
    resource_id: str
    resource_kind: ResourceKind
    adapter_id: str
    adapter_version: str
    normalized_from: str
    provider_response_digest: str
    input_digests: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("worker_id", "resource_id", "adapter_id", "adapter_version", "normalized_from")
    @classmethod
    def provenance_text_is_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("PROVENANCE_REF_INVALID")
        return value

    @field_validator("provider_response_digest", "input_digests")
    @classmethod
    def provenance_digest_is_sha256(cls, value: Any) -> Any:
        values = (value,) if isinstance(value, str) else value
        if any(not _DIGEST.fullmatch(item) for item in values):
            raise ValueError("PROVENANCE_DIGEST_INVALID")
        return value


class PolicyRefusal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str
    code: str
    automatic_bypass: Literal[False] = False

    @field_validator("policy", "code")
    @classmethod
    def refusal_codes_are_bounded(cls, value: str) -> str:
        if not re.fullmatch(r"^[A-Z][A-Z0-9_]{1,63}$", value):
            raise ValueError("POLICY_REFUSAL_CODE_INVALID")
        return value


class CapabilityContribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contribution_id: str
    goal_id: str
    content: str = Field(min_length=1, max_length=16_000)
    conflict_key: str | None = Field(default=None, max_length=120)

    @field_validator("contribution_id", "goal_id")
    @classmethod
    def contribution_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("CONTRIBUTION_REF_INVALID")
        return value


class CapabilityRequest(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.capability-request.v1"

    request_id: str
    task_id: str
    goal_id: str
    capability: str
    constraints: tuple[str, ...] = Field(default=(), max_length=64)
    authorization: AuthorizationMetadata

    @field_validator("request_id", "task_id", "goal_id")
    @classmethod
    def request_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("CAPABILITY_REQUEST_REF_INVALID")
        return value

    @field_validator("capability")
    @classmethod
    def requested_capability_is_bounded(cls, value: str) -> str:
        if not _CAPABILITY.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @model_validator(mode="after")
    def authority_matches_request(self) -> CapabilityRequest:
        if self.authorization.capability != self.capability:
            raise ValueError("CAPABILITY_AUTHORIZATION_MISMATCH")
        return self


class CapabilityResult(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.capability-result.v1"

    result_id: str
    task_id: str
    goal_id: str
    capability: str
    resource_id: str
    resource_kind: ResourceKind
    provider: str | None
    completed_goals: tuple[str, ...] = Field(default=(), max_length=128)
    partial_goals: tuple[str, ...] = Field(default=(), max_length=128)
    unresolved_goals: tuple[str, ...] = Field(default=(), max_length=128)
    blocked_goals: tuple[str, ...] = Field(default=(), max_length=128)
    rejected_goals: tuple[str, ...] = Field(default=(), max_length=128)
    contributions: tuple[CapabilityContribution, ...] = Field(default=(), max_length=256)
    artifacts: tuple[ArtifactReference, ...] = Field(default=(), max_length=128)
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=256)
    metrics: Mapping[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1)
    failure_reason: UnresolvedReason | None = None
    policy_refusal: PolicyRefusal | None
    execution_receipt_ref: ExecutionReceiptRef
    authorization: AuthorizationMetadata
    provenance: Provenance
    human_decision_ref: str | None = None

    @field_validator("result_id", "task_id", "goal_id", "resource_id")
    @classmethod
    def result_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("CAPABILITY_RESULT_REF_INVALID")
        return value

    @field_validator("capability")
    @classmethod
    def result_capability_is_bounded(cls, value: str) -> str:
        if not _CAPABILITY.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("provider", "human_decision_ref")
    @classmethod
    def optional_identity_is_bounded(cls, value: str | None) -> str | None:
        if value is not None and not _ID.fullmatch(value):
            raise ValueError("CAPABILITY_RESULT_REF_INVALID")
        return value

    @field_validator(
        "completed_goals", "partial_goals", "unresolved_goals", "blocked_goals", "rejected_goals"
    )
    @classmethod
    def outcome_goal_ids_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _ID.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("CAPABILITY_RESULT_GOAL_ID_INVALID")
        return value

    @field_validator("metrics")
    @classmethod
    def metrics_are_finite_and_bounded(
        cls, value: Mapping[str, str | int | float | bool | None]
    ) -> Mapping[str, str | int | float | bool | None]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_]{0,47}$", key) for key in value):
            raise ValueError("CAPABILITY_METRIC_NAME_INVALID")
        if any(isinstance(item, float) and not math.isfinite(item) for item in value.values()):
            raise ValueError("CAPABILITY_METRIC_VALUE_INVALID")
        return value

    @model_validator(mode="after")
    def result_preserves_authority_and_resource_kind(self) -> CapabilityResult:
        outcome_sets = (
            set(self.completed_goals),
            set(self.partial_goals),
            set(self.unresolved_goals),
            set(self.blocked_goals),
            set(self.rejected_goals),
        )
        combined: set[str] = set()
        for values in outcome_sets:
            if combined & values:
                raise ValueError("CAPABILITY_RESULT_OUTCOME_OVERLAP")
            combined.update(values)
        if self.goal_id not in combined:
            raise ValueError("CAPABILITY_RESULT_PRIMARY_GOAL_MISSING")
        if self.authorization.capability != self.capability:
            raise ValueError("CAPABILITY_AUTHORIZATION_MISMATCH")
        if self.resource_id != self.provenance.resource_id or self.resource_kind is not self.provenance.resource_kind:
            raise ValueError("CAPABILITY_RESULT_PROVENANCE_MISMATCH")
        model_resource = self.resource_kind in {ResourceKind.LOCAL_MODEL, ResourceKind.CLOUD_MODEL}
        if model_resource != (self.provider is not None):
            raise ValueError("CAPABILITY_RESULT_PROVIDER_KIND_MISMATCH")
        if self.resource_kind is ResourceKind.DETERMINISTIC_ALGORITHM and self.provider is not None:
            raise ValueError("DETERMINISTIC_ALGORITHM_CANNOT_CLAIM_MODEL_PROVIDER")
        if self.resource_kind is ResourceKind.DETERMINISTIC_ALGORITHM and self.confidence is not None:
            raise ValueError("DETERMINISTIC_ALGORITHM_CONFIDENCE_NOT_MEANINGFUL")
        if self.rejected_goals and self.human_decision_ref is None:
            raise ValueError("REJECTED_GOAL_REQUIRES_GOVERNED_DECISION")
        if self.policy_refusal is not None and self.policy_refusal.automatic_bypass:
            raise ValueError("POLICY_REFUSAL_BYPASS_FORBIDDEN")
        for item in self.contributions:
            if item.goal_id not in combined:
                raise ValueError("CONTRIBUTION_GOAL_NOT_IN_RESULT")
        return self

class CoverageEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: str
    status: CoverageStatus
    result_refs: tuple[str, ...] = Field(default=(), max_length=256)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=256)
    unresolved_reasons: tuple[UnresolvedReason, ...] = Field(default=(), max_length=16)
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    next_candidate_action: str | None = Field(default=None, max_length=120)


class CoverageMap(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.coverage-map.v1"

    task_id: str
    task_envelope_hash: str
    entries: tuple[CoverageEntry, ...] = Field(max_length=128)
    source_result_hashes: tuple[str, ...] = Field(default=(), max_length=512)

    def by_goal(self, goal_id: str) -> CoverageEntry:
        for entry in self.entries:
            if entry.goal_id == goal_id:
                return entry
        raise KeyError("GOAL_COVERAGE_NOT_FOUND")


class ResidualGoal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: str
    status: CoverageStatus
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=16)
    unresolved_reasons: tuple[UnresolvedReason, ...] = Field(default=(), max_length=16)
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)


class ResidualTask(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.residual-task.v1"

    residual_task_id: str
    task_id: str
    goals: tuple[ResidualGoal, ...] = Field(min_length=1, max_length=128)
    preserved_result_refs: tuple[str, ...] = Field(default=(), max_length=512)
    preserved_artifacts: tuple[str, ...] = Field(default=(), max_length=256)
    preserved_evidence: tuple[str, ...] = Field(default=(), max_length=512)
    only_remaining_work: Literal[True] = True
    automatic_routing_allowed: bool
    authorization_required: Literal[True] = True
    source_coverage_hash: str

    @property
    def goal_ids(self) -> tuple[str, ...]:
        return tuple(item.goal_id for item in self.goals)


class AmendmentAuthorityDecision(StrEnum):
    AUTO_AMENDABLE = "AUTO_AMENDABLE"
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"


class ResidualPlanAmendment(ProtocolModel):
    """Product planning record compiled by the governed P6.10 bridge."""

    SCHEMA: ClassVar[str] = "pados.residual-plan-amendment.v1"

    amendment_id: str
    task_id: str
    goal_id: str
    residual_id: str
    parent_goal_id: str
    parent_plan_ref: str
    parent_step_ref: str
    project_ref: str
    classification: DataClassification
    priority: TaskPriority
    deadline: datetime | None
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    required_capability: str
    network_requirement: Literal["NONE", "REQUIRED"]
    resource_constraints: Mapping[str, str | int | bool] = Field(default_factory=dict, max_length=16)
    authorization_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    expected_scope_version_ref: str
    provenance: Provenance
    source_result_hash: str
    coverage_map_hash: str
    idempotency_key: str
    authority_decision: AmendmentAuthorityDecision

    @field_validator(
        "amendment_id", "task_id", "goal_id", "residual_id", "parent_goal_id",
        "parent_plan_ref", "parent_step_ref", "project_ref", "expected_scope_version_ref",
        "idempotency_key",
    )
    @classmethod
    def amendment_refs_are_bounded(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("RESIDUAL_AMENDMENT_REF_INVALID")
        return value

    @field_validator("classification", mode="before")
    @classmethod
    def amendment_classification_is_known(cls, value: object) -> DataClassification:
        return parse_classification(value)

    @field_validator("required_capability")
    @classmethod
    def amendment_capability_is_bounded(cls, value: str) -> str:
        if not _CAPABILITY.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("source_result_hash", "coverage_map_hash")
    @classmethod
    def amendment_hashes_are_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("RESIDUAL_AMENDMENT_HASH_INVALID")
        return value

    @field_validator("dependencies", "authorization_refs")
    @classmethod
    def amendment_reference_lists_are_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _ID.fullmatch(value) for value in values) or len(set(values)) != len(values):
            raise ValueError("RESIDUAL_AMENDMENT_REF_INVALID")
        return values

    @field_validator("deadline")
    @classmethod
    def amendment_deadline_is_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("TASK_DATETIME_MUST_BE_TIMEZONE_AWARE")
        return value


class WorkAssignment(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.work-assignment.v1"

    assignment_id: str
    task_id: str
    goal_id: str
    worker_id: str
    capability: str
    reason: str = Field(min_length=1, max_length=500)
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    priority: TaskPriority
    deadline: datetime | None
    resource_id: str
    status: GoalState


class CapabilityConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_key: str
    section_refs: tuple[str, ...] = Field(min_length=2, max_length=128)
    goal_id: str


class CompositionSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    goal_id: str
    result_ref: str
    worker_id: str
    capability: str
    resource_id: str
    content: str = Field(min_length=1, max_length=16_000)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=128)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=256)
    verification_status: VerificationStatus = VerificationStatus.NOT_VERIFIED
    conflict_status: Literal["CLEAR", "CONFLICT"] = "CLEAR"


class ResultCandidate(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.result-candidate.v1"

    candidate_id: str
    task_id: str
    project_ref: str
    classification: DataClassification
    scope_version_ref: str
    sections: tuple[CompositionSection, ...] = Field(default=(), max_length=512)
    conflicts: tuple[CapabilityConflict, ...] = Field(default=(), max_length=256)
    verification_status: VerificationStatus = VerificationStatus.NOT_VERIFIED
    semantic_acceptance: Literal["PENDING_GOVERNED_ACCEPTANCE"] = "PENDING_GOVERNED_ACCEPTANCE"
    provenance_hash: str
    result_receipt_ref: ResultReceiptRef | None = None

    @model_validator(mode="after")
    def receipt_matches_candidate(self) -> ResultCandidate:
        receipt = self.result_receipt_ref
        if receipt is not None:
            if self.verification_status is not VerificationStatus.PASSED:
                raise ValueError("RESULT_RECEIPT_REQUIRES_VERIFIED_CANDIDATE")
            if (
                receipt.candidate_id != self.candidate_id
                or receipt.task_id != self.task_id
                or receipt.project_ref != self.project_ref
                or receipt.classification != self.classification
                or receipt.scope_version_ref != self.scope_version_ref
            ):
                raise ValueError("RESULT_RECEIPT_BINDING_MISMATCH")
        return self


class WorkNodeKind(StrEnum):
    PROJECT = "PROJECT"
    TASK = "TASK"
    GOAL = "GOAL"
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    MODEL = "MODEL"
    CAPABILITY = "CAPABILITY"
    TOOL = "TOOL"
    ARTIFACT = "ARTIFACT"
    DECISION = "DECISION"
    RECEIPT = "RECEIPT"
    PLAN_STEP = "PLAN_STEP"
    AMENDMENT = "AMENDMENT"


class WorkEdgeKind(StrEnum):
    CONTAINS = "CONTAINS"
    DEPENDS_ON = "DEPENDS_ON"
    ASSIGNED_TO = "ASSIGNED_TO"
    EXECUTED_BY = "EXECUTED_BY"
    PRODUCED = "PRODUCED"
    VERIFIED_BY = "VERIFIED_BY"
    BLOCKED_BY = "BLOCKED_BY"
    RESIDUAL_OF = "RESIDUAL_OF"
    DERIVED_FROM = "DERIVED_FROM"


class WorkGraphNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    kind: WorkNodeKind
    label: str = Field(min_length=1, max_length=160)
    status: str = Field(max_length=40)
    metadata: Mapping[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=24)


class WorkGraphEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    target_id: str
    kind: WorkEdgeKind
    reason: str | None = Field(default=None, max_length=240)


class ResourceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_id: str
    worker_id: str
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=32)
    resource_kind: ResourceKind


class ResidualAssignmentCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    goal_id: str
    capability: str
    resource_id: str
    worker_id: str


class WorkGraph(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.work-graph.v1"

    projection_id: str
    authority: Literal["PROJECTION"] = "PROJECTION"
    source_hashes: tuple[str, ...] = Field(min_length=1, max_length=512)
    nodes: tuple[WorkGraphNode, ...] = Field(max_length=2_048)
    edges: tuple[WorkGraphEdge, ...] = Field(max_length=8_192)

    @model_validator(mode="after")
    def graph_is_closed_and_minimized(self) -> WorkGraph:
        node_ids = {item.node_id for item in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("WORK_GRAPH_NODE_DUPLICATE")
        if any(edge.source_id not in node_ids or edge.target_id not in node_ids for edge in self.edges):
            raise ValueError("WORK_GRAPH_EDGE_DANGLING")
        assert_no_credentials(self.model_dump(mode="json"))
        return self

    def node(self, node_id: str) -> WorkGraphNode:
        for item in self.nodes:
            if item.node_id == node_id:
                return item
        raise KeyError("WORK_GRAPH_NODE_NOT_FOUND")


class ScheduleReason(StrEnum):
    DEPENDENCY_READY = "DEPENDENCY_READY"
    DEADLINE_PRESSURE = "DEADLINE_PRESSURE"
    LOCAL_ONLY_CLASSIFICATION = "LOCAL_ONLY_CLASSIFICATION"
    REQUIRED_RESOURCE_ONLINE = "REQUIRED_RESOURCE_ONLINE"
    ACCEPTABLE_COST = "ACCEPTABLE_COST"
    WAITING_FOR_ARTIFACT = "WAITING_FOR_ARTIFACT"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


class SchedulingCandidate(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.scheduling-candidate.v1"

    candidate_id: str
    task_id: str
    goal_id: str
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    priority: TaskPriority
    deadline: datetime | None
    required_capabilities: tuple[str, ...] = Field(default=(), max_length=16)
    resource_requirements: tuple[str, ...] = Field(default=(), max_length=32)
    network_requirement: str
    classification: DataClassification
    estimated_latency_ms: int | None = Field(default=None, ge=0)
    estimated_cost_microunits: int | None = Field(default=None, ge=0)
    human_approval_required: bool
    reason: ScheduleReason
    explanation: str = Field(min_length=1, max_length=400)


class InstallationStatus(StrEnum):
    NOT_INSTALLED = "NOT_INSTALLED"
    INSTALLED = "INSTALLED"


class RegistrationStatus(StrEnum):
    NOT_REGISTERED = "NOT_REGISTERED"
    REGISTERED = "REGISTERED"


class AuthorizationStatus(StrEnum):
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    AUTHORIZED = "AUTHORIZED"


class SelectionStatus(StrEnum):
    NOT_SELECTED = "NOT_SELECTED"
    SELECTED = "SELECTED"


class RecommendationStatus(StrEnum):
    RECOMMENDED = "RECOMMENDED"
    DISMISSED = "DISMISSED"
    ACCEPTED_FOR_REVIEW = "ACCEPTED_FOR_REVIEW"


class CapabilityRecommendation(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.capability-recommendation.v1"

    recommendation_id: str
    task_id: str
    goal_id: str
    required_capability: str
    why_unavailable: str = Field(min_length=1, max_length=500)
    candidate_integration: str
    relevance: str = Field(min_length=1, max_length=48)
    match_rationale: str = Field(min_length=1, max_length=240)
    license_status: str
    provenance_status: str
    installation_status: InstallationStatus
    registration_status: RegistrationStatus
    authorization_status: AuthorizationStatus
    selection_status: SelectionStatus
    status: RecommendationStatus = RecommendationStatus.RECOMMENDED
    auto_install: Literal[False] = False


class RemoteIntentType(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    ASSIGN_GOAL = "ASSIGN_GOAL"
    CHANGE_PRIORITY = "CHANGE_PRIORITY"
    REQUEST_EXECUTION = "REQUEST_EXECUTION"
    REQUEST_REPLAN = "REQUEST_REPLAN"
    APPROVE_RESULT = "APPROVE_RESULT"
    REJECT_RESULT = "REJECT_RESULT"
    REQUEST_CANCEL = "REQUEST_CANCEL"


class RemoteIntent(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.remote-intent.v1"

    intent_id: str
    project_ref: str
    intent_type: RemoteIntentType
    task_id: str | None
    goal_id: str | None
    priority: TaskPriority | None
    human_message: str = Field(default="", max_length=2_000)
    approval_ref: str | None
    target_scope_version_ref: str


class OutcomeRecord(ProtocolModel):
    SCHEMA: ClassVar[str] = "pados.outcome-record.v1"

    outcome_id: str
    task_id: str
    goal_id: str
    result_ref: str
    outcome: str
    recorded_by: str
    recorded_at: datetime
    execution_receipt_ref: ExecutionReceiptRef

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("OUTCOME_DATETIME_MUST_BE_TIMEZONE_AWARE")
        return value


def now_utc() -> datetime:
    """Clock helper for services; pure protocols generally receive time from their caller."""
    return datetime.now(UTC)


def json_size(document: Mapping[str, Any]) -> int:
    return len(json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
