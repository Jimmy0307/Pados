# SPDX-License-Identifier: AGPL-3.0-only
"""Capability and adapter descriptors (metadata only; never authority, never executable).

Descriptors carry no command lines, URLs, file paths or secrets: execution details belong to the
adapter implementation and its operator configuration, which are validated where they are loaded.
Free text is bounded and identifiers are closed patterns, so metadata cannot smuggle shell or network
instructions into the planner.
"""
from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

CAPABILITY_SCHEMA = "pados.capability.descriptor.v1"
ADAPTER_SCHEMA = "pados.capability.adapter.v1"
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
_CONTRACT_REF = re.compile(r"^[a-z][a-z0-9_.-]{2,79}/v[0-9]{1,3}$")
_SPDX = re.compile(r"^[A-Za-z0-9.+-]{1,64}( (AND|OR|WITH) [A-Za-z0-9.+-]{1,64}){0,4}$")
_OPERATION = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
# Display-only text: no control or shell characters, no URLs, no absolute/parent paths.
_TEXT = re.compile(r"^[^\x00-\x1f\x7f`$<>|;&\\]{0,200}$")
_FORBIDDEN_TEXT = re.compile(r"://|\.\.|(^|\s)/|~/|(^|\s)[A-Za-z]:[\\/]")


class CapabilityFamily(StrEnum):
    PERCEPTION = "PERCEPTION"
    RETRIEVAL = "RETRIEVAL"
    STRUCTURED_DATA = "STRUCTURED_DATA"
    ALGORITHM = "ALGORITHM"
    CODE_INTELLIGENCE = "CODE_INTELLIGENCE"
    DOCUMENT_INTELLIGENCE = "DOCUMENT_INTELLIGENCE"
    MODEL_RUNTIME = "MODEL_RUNTIME"
    KNOWLEDGE = "KNOWLEDGE"
    EXECUTION = "EXECUTION"
    LEARNING = "LEARNING"
    GOVERNANCE = "GOVERNANCE"
    VERIFICATION = "VERIFICATION"


class Determinism(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"  # same inputs + parameters -> same output bytes
    SEEDED = "SEEDED"  # deterministic given a recorded seed
    PROBABILISTIC = "PROBABILISTIC"  # model output: candidate only, needs verification


class Modality(StrEnum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    PDF = "PDF"
    TABLE = "TABLE"
    STRUCTURED = "STRUCTURED"
    CODE = "CODE"
    AUDIO = "AUDIO"
    EMBEDDING = "EMBEDDING"


class LifecycleState(StrEnum):
    EXPERIMENTAL = "EXPERIMENTAL"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"

    @property
    def plannable(self) -> bool:
        return self in {LifecycleState.EXPERIMENTAL, LifecycleState.ACTIVE}


class VerificationMethod(StrEnum):
    SCHEMA = "SCHEMA"  # output validated against the output contract
    DETERMINISTIC_REPLAY = "DETERMINISTIC_REPLAY"  # re-run yields the same output hash
    REFERENCE_CHECK = "REFERENCE_CHECK"  # compared with a trusted reference/fixture
    UNIT_TESTS = "UNIT_TESTS"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    NONE = "NONE"  # output is never more than an unverified candidate


class FailureSemantics(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"  # no output artifact on failure
    PARTIAL_WITH_EVIDENCE = "PARTIAL_WITH_EVIDENCE"  # partial output, explicitly marked


class FallbackPolicy(StrEnum):
    """There is never an implicit fallback (e.g. local -> cloud, OCR engine -> LLM)."""

    NONE = "NONE"
    EXPLICIT_SAME_POLICY = "EXPLICIT_SAME_POLICY"  # another adapter, only if it passes the same policy


class Level(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class Profiles(BaseModel):
    """Coarse, descriptive expectations; measured performance comes from Outcome Learning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latency: Level = Level.UNKNOWN
    cost: Level = Level.UNKNOWN
    quality: Level = Level.UNKNOWN


class ResourceRequirement(BaseModel):
    """accelerators: an any-of set (empty = unconstrained). vram_mb applies to dedicated GPUs and
    unified_memory_mb to unified-memory machines; a requirement sized only in unified memory never fits a
    dedicated-GPU node (resource planning fails closed rather than guessing)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_cores: int = Field(default=1, ge=0, le=512)
    ram_mb: int = Field(default=0, ge=0, le=4_194_304)
    gpu_required: bool = False
    vram_mb: int = Field(default=0, ge=0, le=1_048_576)
    unified_memory_mb: int = Field(default=0, ge=0, le=4_194_304)
    accelerators: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("accelerators")
    @classmethod
    def accelerator_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"^[A-Z][A-Z0-9_]{1,31}$", item) for item in value):
            raise ValueError("ACCELERATOR_NAME_INVALID")
        return value


class CapabilityDescriptor(BaseModel):
    """What a capability does, independent of any vendor or engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CAPABILITY_SCHEMA
    capability_id: str
    version: str
    family: CapabilityFamily
    operation: str  # closed identifier, e.g. RECOGNIZE_TEXT; never an instruction
    input_contract: str
    output_contract: str
    determinism: Determinism
    modalities: tuple[Modality, ...] = Field(min_length=1, max_length=8)
    max_classification: DataClassification = DataClassification.INTERNAL
    network_requirement: NetworkRequirement = NetworkRequirement.LOCAL_ONLY
    profiles: Profiles = Profiles()
    resources: ResourceRequirement = ResourceRequirement()
    verification: tuple[VerificationMethod, ...] = Field(min_length=1, max_length=6)
    failure: FailureSemantics = FailureSemantics.FAIL_CLOSED
    fallback: FallbackPolicy = FallbackPolicy.NONE
    lifecycle: LifecycleState = LifecycleState.ACTIVE
    description: str = Field(default="", max_length=200)  # opaque display text; never interpreted

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != CAPABILITY_SCHEMA:
            raise ValueError("CAPABILITY_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("capability_id")
    @classmethod
    def capability_id_pattern(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("CAPABILITY_VERSION_INVALID")
        return value

    @field_validator("input_contract", "output_contract")
    @classmethod
    def contract_ref(cls, value: str) -> str:
        if not _CONTRACT_REF.fullmatch(value):
            raise ValueError("CONTRACT_REF_INVALID")
        return value

    @field_validator("operation")
    @classmethod
    def operation_identifier(cls, value: str) -> str:
        if not _OPERATION.fullmatch(value):
            raise ValueError("CAPABILITY_OPERATION_INVALID")
        return value

    @field_validator("description")
    @classmethod
    def display_text(cls, value: str) -> str:
        return checked_display_text(value)

    @model_validator(mode="after")
    def probabilistic_needs_verification(self) -> CapabilityDescriptor:
        # A probabilistic output with no verification can only ever be an unverified candidate;
        # declaring it is allowed, but it must say so explicitly with NONE alone.
        if VerificationMethod.NONE in self.verification and len(self.verification) > 1:
            raise ValueError("VERIFICATION_NONE_IS_EXCLUSIVE")
        return self


class Distribution(StrEnum):
    """How a third-party engine reaches the user (license / public-release implications)."""

    NONE = "NONE"  # PADOS-owned implementation
    RUNTIME_OPTIONAL = "RUNTIME_OPTIONAL"  # user-installed separately; not bundled
    RUNTIME_DEPENDENCY = "RUNTIME_DEPENDENCY"  # declared PADOS package dependency installed with it; not vendored
    BUNDLED = "BUNDLED"  # shipped inside a PADOS artifact (needs license review)
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"  # remote API; data leaves the host


class LicenseInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spdx: str = "AGPL-3.0-only"  # PADOS-owned code (OD-1); third-party engines declare their own
    engine_version: str = "n/a"
    distribution: Distribution = Distribution.NONE

    @field_validator("engine_version")
    @classmethod
    def engine_version_pattern(cls, value: str) -> str:
        if value != "n/a" and not _VERSION.fullmatch(value):
            raise ValueError("LICENSE_ENGINE_VERSION_INVALID")
        return value

    @field_validator("spdx")
    @classmethod
    def spdx_expression(cls, value: str) -> str:
        if not _SPDX.fullmatch(value):
            raise ValueError("LICENSE_SPDX_INVALID")
        return value


class AdapterDescriptor(BaseModel):
    """One replaceable implementation of a capability (an engine, algorithm or model runtime)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = ADAPTER_SCHEMA
    adapter_id: str
    capability_id: str
    version: str
    backend: str = Field(min_length=1, max_length=40)
    determinism: Determinism
    max_classification: DataClassification
    network_requirement: NetworkRequirement
    resources: ResourceRequirement = ResourceRequirement()
    license: LicenseInfo = LicenseInfo()
    lifecycle: LifecycleState = LifecycleState.ACTIVE
    delegates_to_worker_router: bool = False  # model inference: selection stays with select_worker

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != ADAPTER_SCHEMA:
            raise ValueError("ADAPTER_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("adapter_id")
    @classmethod
    def adapter_id_pattern(cls, value: str) -> str:
        if not _ADAPTER_ID.fullmatch(value):
            raise ValueError("ADAPTER_ID_INVALID")
        return value

    @field_validator("capability_id")
    @classmethod
    def capability_id_pattern(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("CAPABILITY_ID_INVALID")
        return value

    @field_validator("version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("ADAPTER_VERSION_INVALID")
        return value

    @field_validator("backend")
    @classmethod
    def backend_name(cls, value: str) -> str:
        if not re.fullmatch(r"^[a-z][a-z0-9_.-]{0,39}$", value):
            raise ValueError("ADAPTER_BACKEND_INVALID")
        return value

    @model_validator(mode="after")
    def external_service_is_never_local(self) -> AdapterDescriptor:
        if (self.license.distribution is Distribution.EXTERNAL_SERVICE
                and self.network_requirement in {NetworkRequirement.LOCAL_ONLY, NetworkRequirement.NETWORK_OPTIONAL}):
            raise ValueError("EXTERNAL_SERVICE_MUST_DECLARE_NETWORK")
        return self

    @property
    def grant_capability(self) -> str:
        """The name a project-bound PADOS grant must contain to authorize this capability."""
        return f"CAPABILITY:{self.capability_id}"


def checked_display_text(value: str) -> str:
    """Bounded display text that cannot read as a URL, path or shell fragment."""
    if not _TEXT.fullmatch(value) or _FORBIDDEN_TEXT.search(value):
        raise ValueError("DESCRIPTOR_TEXT_INVALID")
    return value


def descriptor_digest(descriptor: CapabilityDescriptor | AdapterDescriptor) -> str:
    """Canonical digest of a descriptor (bound into policy decisions and receipts)."""
    return canonical_hash(descriptor.model_dump(mode="json"))
