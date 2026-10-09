# SPDX-License-Identifier: AGPL-3.0-only
"""Governed Outcome Learning Adapter: external problem-solving evidence as advisory learning input only.

An enterprise may run a structured problem-solving process (cases, root causes, corrective actions,
effectiveness checks) next to PADOS. This module defines how such a process may feed PADOS Outcome Learning
without gaining any authority. Nothing here is specific to any company, method brand or system.

* ``ExternalOutcomeObservation`` is content-free: closed vocabularies, bounded numbers and SHA-256 digests. It
  names the PADOS stage receipt it refers to; case text, people and data never enter PADOS.
* ``outcome_from_external`` turns an observation into an ``OutcomeRecord`` ONLY through the existing
  ``OutcomeAuthenticator`` path (``OutcomeRecord.from_receipts``): the trusted receipt, the trusted plan context
  and the governed decision. An observation whose receipt does not resolve is refused
  (``LEARNING_OUTCOME_UNAUTHENTICATED``).
* The external process's own assessment (``ExternalFeedback``) is never a human decision. ``HumanDecision``
  ACCEPTED / REJECTED comes only from ``OutcomeAuthenticator.resolve_decision``; an observation has no field
  that could carry one, so human confirmation cannot be fabricated.
* ``LearningCandidate`` (memory / rule / retrieval / routing-observation / confidence) and ``TrainingProposal``
  are inert data: authority ``NONE``, evaluation and governed promotion required, and for training offline,
  versioned, gated and reversible, re-entering PADOS only as a NEW artifact through the measured-capability
  path. This module defines no write, apply, promote, deploy or train path, and imports nothing that has one.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.receipts import PlanReceipt
from pados.connectivity.state import ConnectivityState
from pados.execution.context import canonical_hash
from pados.learning.records import (
    CAPABILITY_REF_PATTERN,
    ContextFeatures,
    HardwareClass,
    IdentityKey,
    LearningRecordError,
    OutcomeAuthenticator,
    OutcomeRecord,
    validate_context_features,
)
from pados.lifecycle.minimization import assert_minimized

EXTERNAL_OUTCOME_SCHEMA = "pados.learning.external-outcome.v1"
LEARNING_CANDIDATE_SCHEMA = "pados.learning.candidate.v1"
TRAINING_PROPOSAL_SCHEMA = "pados.learning.training-proposal.v1"
MAX_EVIDENCE_DIGESTS = 16
MAX_SUPPORTING_OUTCOMES = 64
MAX_RECORD_BYTES = 4096

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_OUTCOME_ID = re.compile(r"^oc-[0-9a-f]{40}$")
_EVALUATION_ID = re.compile(r"^ev-[0-9a-f]{40}$")
_CAPABILITY_REF = re.compile(CAPABILITY_REF_PATTERN)


class ExternalFeedback(StrEnum):
    """The external process's own effectiveness assessment. Evidence for learning; never a human decision."""

    RESOLVED_EFFECTIVE = "RESOLVED_EFFECTIVE"
    RESOLVED_INEFFECTIVE = "RESOLVED_INEFFECTIVE"
    RECURRED = "RECURRED"
    UNRESOLVED = "UNRESOLVED"
    UNKNOWN = "UNKNOWN"


def _sha(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("EXTERNAL_DIGEST_INVALID")
    return value


def _digests(value: tuple[str, ...], code: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if any(not pattern.fullmatch(item) for item in value) or len(set(value)) != len(value):
        raise ValueError(code)
    return tuple(sorted(value))


class ExternalOutcomeObservation(BaseModel):
    """One content-free observation an external problem-solving process submits about a PADOS stage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = EXTERNAL_OUTCOME_SCHEMA
    source_label: str  # opaque label the operator assigned to the external process
    stage_receipt_hash: str  # the PADOS stage receipt this observation is about
    external_case_digest: str  # SHA-256 of the external case record (the case itself never enters PADOS)
    evidence_digests: tuple[str, ...] = Field(default=(), max_length=MAX_EVIDENCE_DIGESTS)
    feedback: ExternalFeedback = ExternalFeedback.UNKNOWN
    hardware_class: HardwareClass = HardwareClass.UNKNOWN
    network_state: ConnectivityState
    context_features: ContextFeatures = ()
    quality_metric: float | None = Field(default=None, ge=0.0, le=1.0)
    retry_count: int = Field(default=0, ge=0, le=100)
    observed_at: datetime

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != EXTERNAL_OUTCOME_SCHEMA:
            raise ValueError("EXTERNAL_OUTCOME_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("source_label")
    @classmethod
    def label(cls, value: str) -> str:
        if not _LABEL.fullmatch(value):
            raise ValueError("EXTERNAL_SOURCE_LABEL_INVALID")
        return value

    @field_validator("stage_receipt_hash", "external_case_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha(value)

    @field_validator("evidence_digests")
    @classmethod
    def evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _digests(value, "EXTERNAL_DIGEST_INVALID", _SHA256)

    @field_validator("context_features", mode="before")
    @classmethod
    def closed_features(cls, value: Any) -> ContextFeatures:
        return validate_context_features(value)

    @field_validator("quality_metric")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("LEARNING_NUMBER_NOT_FINITE")
        return value

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("EXTERNAL_TIMESTAMP_NOT_AWARE")
        return value


def _trusted_plan(authenticator: OutcomeAuthenticator, receipt_hash: str) -> PlanReceipt | None:
    try:
        plan = authenticator.resolve_receipt(receipt_hash)
    except Exception:  # noqa: BLE001 - a resolver failure is "not authenticated" (fail closed)
        return None
    return plan if isinstance(plan, PlanReceipt) else None


def outcome_from_external(observation: ExternalOutcomeObservation, *, authenticator: OutcomeAuthenticator,
                          recorded_at: datetime, identity_key: IdentityKey | None = None) -> OutcomeRecord:
    """An ``OutcomeRecord`` through the existing authenticated path, or ``LearningRecordError``.

    Every authenticated fact (task, capability, adapter, success, verification, execution time, human decision)
    comes from the trusted receipt, plan context and governed approval path. The observation contributes only
    the non-authoritative fields ``from_receipts`` accepts; its ``feedback`` never becomes a decision."""
    if not isinstance(observation, ExternalOutcomeObservation):
        raise LearningRecordError("EXTERNAL_OUTCOME_INVALID")
    plan = _trusted_plan(authenticator, observation.stage_receipt_hash)
    stage = None if plan is None else next(
        (item for item in plan.stages if item.receipt_hash == observation.stage_receipt_hash), None)
    if plan is None or stage is None:
        raise LearningRecordError("LEARNING_OUTCOME_UNAUTHENTICATED")
    return OutcomeRecord.from_receipts(
        plan, stage, authenticator=authenticator, hardware_class=observation.hardware_class,
        network_state=observation.network_state, recorded_at=recorded_at,
        context_features=dict(observation.context_features) or None, quality_metric=observation.quality_metric,
        retry_count=observation.retry_count, identity_key=identity_key)


# --- inert improvement candidates (data only; no write path) ----------------------------------------------------

class CandidateKind(StrEnum):
    """What online learning may adjust, after evaluation and governed promotion (never model weights)."""

    MEMORY = "MEMORY"
    RULE = "RULE"
    RETRIEVAL = "RETRIEVAL"
    ROUTING_OBSERVATION = "ROUTING_OBSERVATION"
    CONFIDENCE = "CONFIDENCE"


class TrainingMethod(StrEnum):
    LORA = "LORA"
    ADAPTER = "ADAPTER"
    FINE_TUNE = "FINE_TUNE"


class _Inert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supporting_outcome_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_SUPPORTING_OUTCOMES)
    evaluation_ids: tuple[str, ...] = Field(default=(), max_length=MAX_SUPPORTING_OUTCOMES)
    authority: Literal["NONE"] = "NONE"
    status: Literal["CANDIDATE"] = "CANDIDATE"
    requires_evaluation: Literal[True] = True
    requires_governed_promotion: Literal[True] = True

    @field_validator("supporting_outcome_ids")
    @classmethod
    def outcomes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _digests(value, "SUPPORTING_OUTCOME_ID_INVALID", _OUTCOME_ID)

    @field_validator("evaluation_ids")
    @classmethod
    def evaluations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _digests(value, "EVALUATION_ID_INVALID", _EVALUATION_ID)

    def _derived(self, prefix: str) -> str:
        return f"{prefix}-{canonical_hash(self.model_dump(mode='json'))[:40]}"

    def to_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json")
        assert_minimized(record, allowed_keys=frozenset(type(self).model_fields), max_string=128,
                         max_items=MAX_SUPPORTING_OUTCOMES, max_total_bytes=MAX_RECORD_BYTES)
        return record


class LearningCandidate(_Inert):
    """A memory / rule / retrieval / routing-observation / confidence improvement candidate. A candidate, not a
    change: its content lives outside the learning records and is referenced by ``proposal_digest`` only."""

    schema_version: Literal["pados.learning.candidate.v1"] = "pados.learning.candidate.v1"
    kind: CandidateKind
    capability_ref: str
    proposal_digest: str

    @field_validator("capability_ref")
    @classmethod
    def capability(cls, value: str) -> str:
        if not _CAPABILITY_REF.fullmatch(value):
            raise ValueError("CAPABILITY_REF_NOT_A_DIGEST")
        return value

    @field_validator("proposal_digest")
    @classmethod
    def proposal(cls, value: str) -> str:
        return _sha(value)

    @model_validator(mode="after")
    def minimized(self) -> LearningCandidate:
        self.to_record()
        return self

    @property
    def candidate_id(self) -> str:
        return self._derived("lc")


class TrainingProposal(_Inert):
    """A request for OFFLINE model adaptation. Inert: no job is started, no weights change. A trained result is a
    NEW artifact (new manifest digest) that must be benchmarked into a measured profile and explicitly registered;
    rollback means the previous artifact stays registered."""

    schema_version: Literal["pados.learning.training-proposal.v1"] = "pados.learning.training-proposal.v1"
    method: TrainingMethod
    base_manifest_digest: str
    dataset_snapshot_hash: str
    evaluation_plan_digest: str
    requires_offline_evaluation: Literal[True] = True
    online: Literal[False] = False
    gated: Literal[True] = True
    versioned: Literal[True] = True
    reversible: Literal[True] = True
    result_enters_as: Literal["NEW_MEASURED_ARTIFACT"] = "NEW_MEASURED_ARTIFACT"

    @field_validator("base_manifest_digest")
    @classmethod
    def manifest(cls, value: str) -> str:
        if not _MANIFEST.fullmatch(value):
            raise ValueError("MANIFEST_DIGEST_INVALID")
        return value

    @field_validator("dataset_snapshot_hash", "evaluation_plan_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        return _sha(value)

    @model_validator(mode="after")
    def minimized(self) -> TrainingProposal:
        self.to_record()
        return self

    @property
    def proposal_id(self) -> str:
        return self._derived("tp")
