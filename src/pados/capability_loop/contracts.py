# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral contracts of the P7 capability loop.

These are PADOS-owned shapes. Provider-specific response schemas never cross this boundary: an adapter
normalizes them into ``DiscoveryEvidence`` / ``LearningAssessment`` before anything else sees them.

Capability state and learning state are deliberately separate dimensions (``human_mastery``,
``system_availability``, ``validation_status`` and ``evidence_refs`` are never collapsed into one score).
A candidate produced by discovery can only ever be DISCOVERED, EXPERIMENTAL, VALIDATION_REQUIRED or
BLOCKED; VALIDATED is reachable only through a recorded validation outcome (``lifecycle.promote``), never
because discovery succeeded.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.model import checked_display_text
from pados.enterprise.classification import DataClassification

SEMANTIC_AUTHORITY_NONE = "NONE"
CONTRACT_VERSION = "pados.capability_loop.v1"
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,95}$")
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
MAX_LIMITATIONS = 16
MAX_EVIDENCE = 32


class CapabilityState(StrEnum):
    """Executable-capability lifecycle of the loop (separate from the registry ``LifecycleState``)."""

    UNKNOWN = "UNKNOWN"
    DISCOVERED = "DISCOVERED"
    EXPERIMENTAL = "EXPERIMENTAL"
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"
    VALIDATED = "VALIDATED"
    BLOCKED = "BLOCKED"


# The only states a discovery provider's output may land in.
DISCOVERY_STATES: frozenset[CapabilityState] = frozenset({
    CapabilityState.DISCOVERED, CapabilityState.EXPERIMENTAL, CapabilityState.VALIDATION_REQUIRED,
    CapabilityState.BLOCKED,
})


class ProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"  # configured but absent, unhealthy or incompatible
    UNAVAILABLE = "UNAVAILABLE"  # not configured
    UNAUTHORIZED = "UNAUTHORIZED"


class FailureCode(StrEnum):
    """Normalized provider failure vocabulary (never provider free text)."""

    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_ABSENT = "PROVIDER_ABSENT"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    TIMEOUT = "TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"
    CANCELLED = "CANCELLED"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    AUTHORITY_CLAIM_REFUSED = "AUTHORITY_CLAIM_REFUSED"


class Confidence(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _identifier(value: str, code: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(code)
    return value


def _codes(values: tuple[str, ...]) -> tuple[str, ...]:
    if any(not _CODE.fullmatch(item) for item in values):
        raise ValueError("LIMITATION_CODE_INVALID")
    return values


class ProviderHealth(_Frozen):
    provider_id: str
    status: ProviderStatus
    code: str | None = None  # a FailureCode or a provider status code; never free text
    runtime_version: str | None = Field(default=None, max_length=40)

    @field_validator("provider_id")
    @classmethod
    def _provider(cls, value: str) -> str:
        return _identifier(value, "PROVIDER_ID_INVALID")


class DiscoveryRequest(_Frozen):
    """What PADOS asks a discovery provider. Built by PADOS, never by the provider."""

    request_id: str
    project_id: str
    work_contract_id: str
    goal_ref: str
    required_capability: str
    target_ref: str  # an operator-registered target id (never a path supplied by a caller)
    classification: DataClassification

    @field_validator("request_id", "project_id", "work_contract_id", "goal_ref", "target_ref")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _identifier(value, "DISCOVERY_REQUEST_ID_INVALID")

    @field_validator("required_capability")
    @classmethod
    def _capability(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value


class DiscoveryEvidence(_Frozen):
    """One normalized observation from a discovery provider (digest-bound, no raw payload)."""

    evidence_digest: str  # sha256 of the provider's canonical evidence record
    provider_evidence_ref: str | None = Field(default=None, max_length=96)
    operation: str  # closed identifier, e.g. INSPECT_RUNTIME_CATALOG
    confidence: Confidence
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_LIMITATIONS)
    untrusted_content: bool = True  # content derived from an analysis target is never instructions

    @field_validator("evidence_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("EVIDENCE_DIGEST_INVALID")
        return value

    @field_validator("operation")
    @classmethod
    def _operation(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("EVIDENCE_OPERATION_INVALID")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _codes(value)


class CapabilityCandidate(_Frozen):
    """A proposed capability. Inert: it grants, registers and executes nothing."""

    contract_version: Literal["pados.capability_loop.v1"] = "pados.capability_loop.v1"
    candidate_id: str
    required_capability: str
    provider_id: str
    state: CapabilityState
    reason_code: str
    evidence: tuple[DiscoveryEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_LIMITATIONS)
    description: str = Field(default="", max_length=200)
    semantic_authority: Literal["NONE"] = "NONE"
    auto_promote: Literal[False] = False

    @field_validator("candidate_id", "provider_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _identifier(value, "CANDIDATE_ID_INVALID")

    @field_validator("required_capability")
    @classmethod
    def _capability(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("reason_code")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("REASON_CODE_INVALID")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _codes(value)

    @field_validator("description")
    @classmethod
    def _display(cls, value: str) -> str:
        return checked_display_text(value)


class DiscoveryOutcome(_Frozen):
    """A provider's normalized answer: evidence, limitations, or a normalized failure. Never authority."""

    provider_id: str
    status: ProviderStatus
    failure: FailureCode | None = None
    evidence: tuple[DiscoveryEvidence, ...] = Field(default=(), max_length=MAX_EVIDENCE)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_LIMITATIONS)
    capability_supported: bool = False
    runtime_version: str | None = Field(default=None, max_length=40)
    partial: bool = False
    semantic_authority: Literal["NONE"] = "NONE"

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _codes(value)

    @model_validator(mode="after")
    def _status_failure_consistent(self) -> DiscoveryOutcome:
        # An AVAILABLE answer carries no failure; any non-AVAILABLE answer must name why it failed. This
        # makes "status != AVAILABLE with failure=None" impossible to construct, so ``failure is None`` can
        # never be mistaken for success on its own.
        if self.status is ProviderStatus.AVAILABLE and self.failure is not None:
            raise ValueError("OUTCOME_AVAILABLE_WITH_FAILURE")
        if self.status is not ProviderStatus.AVAILABLE and self.failure is None:
            raise ValueError("OUTCOME_NON_AVAILABLE_REQUIRES_FAILURE")
        return self

    @property
    def is_successful(self) -> bool:
        """The whole success invariant: available, no failure, and complete (never partial)."""
        return self.status is ProviderStatus.AVAILABLE and self.failure is None and not self.partial


@runtime_checkable
class CapabilityDiscoveryProvider(Protocol):
    """An external runtime that can say what capability exists for an authorized target.

    Implementations must fail closed: an absent, unhealthy, unauthorized or incompatible runtime returns a
    ``DiscoveryOutcome`` with a ``FailureCode``; it never raises into PADOS and never mutates PADOS state.
    ``discover`` is called only after the PADOS discovery gate allowed the request.
    """

    @property
    def provider_id(self) -> str: ...

    def health(self) -> ProviderHealth: ...

    def discover(self, request: DiscoveryRequest, *, target_path: str) -> DiscoveryOutcome: ...
