# SPDX-License-Identifier: AGPL-3.0-only
"""Capability lifecycle for the loop: UNKNOWN -> DISCOVERED -> EXPERIMENTAL -> VALIDATION_REQUIRED -> VALIDATED.

Learning state and executable-capability state are kept as separate dimensions and never collapsed into one
score. A ``CapabilityDimensions`` holds ``human_mastery`` (whether a person can perform the task),
``system_availability`` (whether an executable capability exists) and ``validation_status`` (whether that
capability has been validated), each with its own evidence references.

Promotion to ``VALIDATED`` requires a recorded validation outcome; discovery or learning success alone can
never reach it. Promotion here only ever produces a ``PromotionProposal`` -- an inert recommendation. The
registry (operator ``CapabilityRegistry`` calls) remains the sole authority that activates a capability;
this module grants, registers and executes nothing.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.capability_loop.contracts import CapabilityState
from pados.capability_loop.learning_contracts import MasteryLevel

_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")

# The only forward transitions the loop allows. VALIDATED is reachable only from VALIDATION_REQUIRED, and
# only through promote() with a validation outcome (never from DISCOVERED/EXPERIMENTAL directly).
_FORWARD: dict[CapabilityState, frozenset[CapabilityState]] = {
    CapabilityState.UNKNOWN: frozenset({CapabilityState.DISCOVERED, CapabilityState.BLOCKED}),
    CapabilityState.DISCOVERED: frozenset({CapabilityState.EXPERIMENTAL, CapabilityState.VALIDATION_REQUIRED,
                                           CapabilityState.BLOCKED}),
    CapabilityState.EXPERIMENTAL: frozenset({CapabilityState.VALIDATION_REQUIRED, CapabilityState.BLOCKED}),
    CapabilityState.VALIDATION_REQUIRED: frozenset({CapabilityState.VALIDATED, CapabilityState.BLOCKED}),
    CapabilityState.VALIDATED: frozenset({CapabilityState.BLOCKED}),
    CapabilityState.BLOCKED: frozenset({CapabilityState.VALIDATION_REQUIRED}),
}


class SystemAvailability(StrEnum):
    UNKNOWN = "UNKNOWN"
    CANDIDATE = "CANDIDATE"  # a discovered candidate exists, not yet executable
    AVAILABLE = "AVAILABLE"  # a registered, executable capability exists


class ValidationStatus(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"
    VALIDATED = "VALIDATED"


def transition_allowed(current: CapabilityState, target: CapabilityState) -> bool:
    return target in _FORWARD.get(current, frozenset())


class CapabilityDimensions(BaseModel):
    """Three independent dimensions of one capability. Never reduced to a single score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    human_mastery: MasteryLevel = MasteryLevel.UNASSESSED
    system_availability: SystemAvailability = SystemAvailability.UNKNOWN
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("capability_id")
    @classmethod
    def _capability(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _DIGEST.fullmatch(item) for item in value):
            raise ValueError("EVIDENCE_REF_INVALID")
        return value


class PromotionProposal(BaseModel):
    """An inert proposal to move a capability forward. It activates nothing; the registry decides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    from_state: CapabilityState
    to_state: CapabilityState
    validation_outcome_ref: str | None = None  # a recorded validation receipt hash; required for VALIDATED
    reason_code: str
    semantic_authority: Literal["NONE"] = "NONE"
    auto_apply: Literal[False] = False

    @field_validator("capability_id")
    @classmethod
    def _capability(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("validation_outcome_ref")
    @classmethod
    def _ref(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("VALIDATION_OUTCOME_REF_INVALID")
        return value


class LifecycleError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def propose_transition(capability_id: str, *, current: CapabilityState, target: CapabilityState,
                       reason_code: str, validation_outcome_ref: str | None = None) -> PromotionProposal:
    """Build a transition proposal, fail-closed on illegal transitions and unproven validation.

    Promotion to VALIDATED requires ``validation_outcome_ref`` (a recorded validation receipt). No other
    input reaches VALIDATED, so discovery or learning success alone cannot promote a capability.
    """
    if not transition_allowed(current, target):
        raise LifecycleError("TRANSITION_NOT_ALLOWED")
    if target is CapabilityState.VALIDATED and validation_outcome_ref is None:
        raise LifecycleError("VALIDATION_OUTCOME_REQUIRED")
    if target is not CapabilityState.VALIDATED and validation_outcome_ref is not None:
        raise LifecycleError("VALIDATION_OUTCOME_UNEXPECTED")
    return PromotionProposal(capability_id=capability_id, from_state=current, to_state=target,
                             validation_outcome_ref=validation_outcome_ref, reason_code=reason_code)
