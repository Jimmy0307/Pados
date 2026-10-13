# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral contracts for the P7.2 adaptive-learning boundary.

These shapes describe a human learning a capability through an external adaptive-learning runtime. They are
PADOS-owned; a provider's own session/scene/quiz schema never crosses this boundary. OpenMAIC is the first
implementation, not the definition.

Model routing is explicit. An adaptive-learning runtime that chooses its own model server-side is marked
``model_routing = EXTERNAL_PROVIDER_CONTROLLED``; PADOS does not claim to route it. ``PADOS_ROUTED`` is used
only where the operation's model demonstrably goes through the PADOS worker-router bridge.

Learning state and executable-capability state are distinct. ``LearningAssessment`` reports ``human_mastery``
and never asserts ``system_availability`` or a capability promotion: mastering a topic is not the system
gaining a capability, and neither is ever collapsed into the other or into a single score.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.capability_loop.contracts import (
    Confidence,
    FailureCode,
    ProviderStatus,
)

LEARNING_CONTRACT_VERSION = "pados.capability_loop.learning.v1"
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,95}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
MAX_MATERIALS = 32
MAX_LIMITATIONS = 16


class ModelRouting(StrEnum):
    PADOS_ROUTED = "PADOS_ROUTED"  # the operation's model goes through the PADOS worker-router bridge
    EXTERNAL_PROVIDER_CONTROLLED = "EXTERNAL_PROVIDER_CONTROLLED"  # the runtime selects its own model


class LearningSessionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    READY = "READY"  # curriculum/material generated and available
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class MasteryLevel(StrEnum):
    """Coarse, evidence-bound learner mastery (never a bare score, never a capability claim)."""

    UNASSESSED = "UNASSESSED"
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    PASSED = "PASSED"
    FAILED = "FAILED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _identifier(value: str, code: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(code)
    return value


class MaterialRef(_Frozen):
    """A reference to learning material PADOS already holds (never raw bytes, never a path)."""

    material_ref: str  # an operator/project-registered material id
    content_digest: str  # sha256 of the material PADOS holds
    media_kind: str = Field(max_length=32)

    @field_validator("material_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        return _identifier(value, "MATERIAL_REF_INVALID")

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("MATERIAL_DIGEST_INVALID")
        return value

    @field_validator("media_kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if not re.fullmatch(r"^[a-z][a-z0-9_./-]{1,31}$", value):
            raise ValueError("MATERIAL_MEDIA_KIND_INVALID")
        return value


class LearningRequest(_Frozen):
    """What PADOS asks an adaptive-learning provider. Built by PADOS."""

    request_id: str
    project_id: str
    work_contract_id: str
    goal_ref: str
    capability_gap_ref: str  # the capability the human is learning toward
    learning_goal: str = Field(min_length=1, max_length=400)
    materials: tuple[MaterialRef, ...] = Field(default=(), max_length=MAX_MATERIALS)

    @field_validator("request_id", "project_id", "work_contract_id", "goal_ref", "capability_gap_ref")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _identifier(value, "LEARNING_REQUEST_ID_INVALID")

    @field_validator("learning_goal")
    @classmethod
    def _goal(cls, value: str) -> str:
        # Opaque display text: no control characters (it is never interpreted as an instruction by PADOS).
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
            raise ValueError("LEARNING_GOAL_INVALID")
        return value


class LearningSession(_Frozen):
    """Normalized external-session state. PADOS stores only this projection, never the runtime's own state."""

    provider_id: str
    session_ref: str  # the external runtime's session id (opaque to PADOS)
    status: LearningSessionStatus
    model_routing: ModelRouting
    materials_bound: int = Field(ge=0, le=MAX_MATERIALS)
    progress_percent: int = Field(ge=0, le=100)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_LIMITATIONS)
    semantic_authority: Literal["NONE"] = "NONE"

    @field_validator("provider_id")
    @classmethod
    def _provider(cls, value: str) -> str:
        return _identifier(value, "PROVIDER_ID_INVALID")

    @field_validator("session_ref")
    @classmethod
    def _session(cls, value: str) -> str:
        if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", value):
            raise ValueError("SESSION_REF_INVALID")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CODE.fullmatch(item) for item in value):
            raise ValueError("LIMITATION_CODE_INVALID")
        return value


class LearningAssessment(_Frozen):
    """A normalized learner-progress observation. Reports human mastery only; never a capability promotion."""

    provider_id: str
    session_ref: str
    capability_gap_ref: str
    human_mastery: MasteryLevel
    score_available: bool = False
    score_percent: int | None = Field(default=None, ge=0, le=100)
    confidence: Confidence = Confidence.OBSERVED
    evidence_digest: str
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_LIMITATIONS)
    semantic_authority: Literal["NONE"] = "NONE"
    grants_capability: Literal[False] = False  # learning never grants a system capability

    @field_validator("provider_id", "capability_gap_ref")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _identifier(value, "ASSESSMENT_ID_INVALID")

    @field_validator("session_ref")
    @classmethod
    def _session(cls, value: str) -> str:
        if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", value):
            raise ValueError("SESSION_REF_INVALID")
        return value

    @field_validator("evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("ASSESSMENT_DIGEST_INVALID")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CODE.fullmatch(item) for item in value):
            raise ValueError("LIMITATION_CODE_INVALID")
        return value


class LearningOutcome(_Frozen):
    """A provider's normalized answer for one learning operation (session, material, state, assessment)."""

    provider_id: str
    status: ProviderStatus
    failure: FailureCode | None = None
    session: LearningSession | None = None
    assessment: LearningAssessment | None = None
    partial: bool = False
    semantic_authority: Literal["NONE"] = "NONE"


@runtime_checkable
class LearningProvider(Protocol):
    """An external adaptive-learning runtime. Fails closed; never writes PADOS semantic state."""

    @property
    def provider_id(self) -> str: ...

    def health(self) -> "ProviderHealthLike": ...

    def create_session(self, request: LearningRequest) -> LearningOutcome: ...

    def session_state(self, session_ref: str) -> LearningOutcome: ...

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome: ...


class ProviderHealthLike(Protocol):
    @property
    def status(self) -> ProviderStatus: ...
