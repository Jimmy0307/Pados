# SPDX-License-Identifier: AGPL-3.0-only
"""Per-stage execution evidence (operational provenance, never acceptance).

Every stage yields a ``StageReceipt`` binding: capability id/version/digest, adapter id/version/digest, the
resource, an attempt id, input and output hashes, parameters hash, seed, timing, verification method and
result (with an evidence reference), the policy decision hash, optional Worker Router selection hash, partial
and failure state. Receipts are hash-chained into a ``PlanReceipt`` whose genesis is bound to the plan.

``verify_structure`` proves internal consistency only. It does NOT prove that execution happened or that
anything was accepted: receipts are evidence handed to the existing ExecutionReceipt -> validation ->
ResultReceipt -> CAS path, and human acceptance is never a field the fabric sets — the plan records only a
reference to a decision made through the existing governed approval path.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.artifacts import ArtifactRef
from pados.capability.model import CapabilityFamily, Determinism, VerificationMethod
from pados.execution.context import canonical_hash

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_RECEIPT_METRICS = 32


class VerificationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_VERIFIED = "NOT_VERIFIED"


def _hash(value: str, code: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError(code)
    return value


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("RECEIPT_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("RECEIPT_TIMESTAMP_NOT_AWARE")
    return parsed


class StageReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    stage_id: str
    attempt_id: str
    capability_id: str
    capability_family: CapabilityFamily
    capability_version: str
    capability_digest: str
    adapter_id: str
    adapter_version: str
    adapter_digest: str
    resource_id: str
    determinism: Determinism
    inputs: tuple[ArtifactRef, ...]
    outputs: tuple[ArtifactRef, ...]
    parameters_hash: str
    seed: int | None = None
    started_at: str
    finished_at: str
    duration_ms: int = Field(ge=0)
    verification_method: VerificationMethod
    verification: VerificationStatus
    verification_evidence_hash: str | None = None
    verifier_id: str | None = None  # which verifier produced the evidence
    policy_decision_hash: str
    fallback_transition_hash: str | None = None
    router_selection_hash: str | None = None  # Worker Router evidence for model-inference stages
    partial: bool = False
    failure_code: str | None = None
    metrics: Mapping[str, float] = {}
    prev_hash: str
    semantic_authority: str = "NONE"

    @field_validator("plan_id", "stage_id", "attempt_id", "resource_id")
    @classmethod
    def identifiers(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("RECEIPT_IDENTIFIER_INVALID")
        return value

    @field_validator("capability_digest", "adapter_digest", "parameters_hash", "policy_decision_hash", "prev_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        return _hash(value, "RECEIPT_HASH_INVALID")

    @field_validator("verification_evidence_hash", "router_selection_hash", "fallback_transition_hash")
    @classmethod
    def optional_hashes(cls, value: str | None) -> str | None:
        return None if value is None else _hash(value, "RECEIPT_HASH_INVALID")

    @field_validator("metrics")
    @classmethod
    def bounded_metrics(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if len(value) > MAX_RECEIPT_METRICS or any(not math.isfinite(float(v)) for v in value.values()):
            raise ValueError("RECEIPT_METRICS_INVALID")
        return dict(value)

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("STAGE_RECEIPT_CANNOT_CARRY_AUTHORITY")
        return value

    @model_validator(mode="after")
    def consistent(self) -> StageReceipt:
        if _aware(self.finished_at) < _aware(self.started_at):
            raise ValueError("RECEIPT_TIME_ORDER_INVALID")
        if self.determinism is Determinism.SEEDED and self.seed is None:
            raise ValueError("SEEDED_STAGE_REQUIRES_SEED")
        if self.verification is VerificationStatus.PASSED and (
                self.verification_method is VerificationMethod.NONE or self.verification_evidence_hash is None
                or not self.verifier_id or not _ID.fullmatch(self.verifier_id)):
            raise ValueError("PASSED_VERIFICATION_REQUIRES_METHOD_EVIDENCE_AND_VERIFIER")
        if self.capability_family is CapabilityFamily.MODEL_RUNTIME and self.router_selection_hash is None:
            raise ValueError("MODEL_STAGE_REQUIRES_WORKER_ROUTER_EVIDENCE")
        failed = self.failure_code is not None
        if failed and (self.verification is VerificationStatus.PASSED or (self.outputs and not self.partial)):
            raise ValueError("FAILED_STAGE_CANNOT_REPORT_COMPLETE_OUTPUT")
        if self.partial and not failed:
            raise ValueError("PARTIAL_STAGE_REQUIRES_FAILURE_CODE")
        return self

    @property
    def receipt_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def plan_genesis(plan_id: str, plan_hash: str) -> str:
    """The chain starts from the plan itself, so stages cannot be re-hung under another plan."""
    return canonical_hash({"plan_id": plan_id, "plan_hash": plan_hash, "kind": "pados.capability.plan-genesis"})


class PlanReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    plan_hash: str
    expected_stage_ids: tuple[str, ...] = Field(min_length=1)
    stages: tuple[StageReceipt, ...] = Field(min_length=1)
    human_decision_ref: str | None = None  # id of a decision in the existing governed approval path, if any
    semantic_authority: str = "NONE"

    @field_validator("plan_hash")
    @classmethod
    def plan_hash_format(cls, value: str) -> str:
        return _hash(value, "PLAN_HASH_INVALID")

    @field_validator("human_decision_ref")
    @classmethod
    def decision_ref(cls, value: str | None) -> str | None:
        if value is not None and not _ID.fullmatch(value):
            raise ValueError("HUMAN_DECISION_REF_INVALID")
        return value

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("PLAN_RECEIPT_CANNOT_CARRY_AUTHORITY")
        return value

    def verify_structure(self) -> bool:
        """Structural integrity only: stage set/order matches the plan and the chain links from the plan
        genesis. This is not proof of execution or acceptance."""
        if tuple(stage.stage_id for stage in self.stages) != self.expected_stage_ids:
            return False
        prev = plan_genesis(self.plan_id, self.plan_hash)
        for stage in self.stages:
            if stage.prev_hash != prev or stage.plan_id != self.plan_id:
                return False
            prev = stage.receipt_hash
        return True

    @property
    def envelope_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def parameters_hash(parameters: Mapping[str, Any]) -> str:
    return canonical_hash(dict(parameters))


def chain(plan_id: str, plan_hash: str, stages: Sequence[StageReceipt]) -> list[StageReceipt]:
    """Link receipts in order from the plan genesis (executors build receipts stage by stage)."""
    linked: list[StageReceipt] = []
    prev = plan_genesis(plan_id, plan_hash)
    for stage in stages:
        stage = stage.model_copy(update={"prev_hash": prev})
        linked.append(StageReceipt.model_validate(stage.model_dump()))
        prev = linked[-1].receipt_hash
    return linked
