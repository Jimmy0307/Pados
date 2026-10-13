# SPDX-License-Identifier: AGPL-3.0-only
"""Model capability profiles: measured, evidence-bound descriptions of one model artifact on one runtime and device.

A ``ModelCapabilityProfile`` says what was MEASURED for an immutable model artifact (manifest and layer digests,
never a tag or a name) served by one runtime binding on one profiled device, under one benchmark configuration,
with digests of the raw evidence. It is description, never authorization: registering a profile in the existing
``ModelCatalog`` makes it describable to planning, and nothing here grants, routes or selects. Authorization stays
with the project grant (``pados.capability.policy``) and the compute-node registration; selection stays with the
Worker Router.

    benchmarked != registered != authorized != routable

Model independence: the profile's planner-facing vocabulary is ``MeasuredCapability`` plus numbers, units, sample
sizes and constraints. Runtime- or vendor-specific facts (runtime family label, artifact reference, provenance)
are opaque data inside the identity and provenance sections; no planning rule reads them. A capability that was
not measured is ``NOT_MEASURED`` and an invalid measurement is ``INVALID_MEASUREMENT``: neither ever satisfies a
requirement (UNKNOWN stays UNKNOWN; fail closed).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.model import Level
from pados.execution.context import canonical_hash
from pados.resources.hardware import AcceleratorRuntime

PROFILE_SCHEMA = "pados.resources.model-capability-profile.v1"
MAX_OBSERVATIONS = 128
MAX_CONSTRAINTS = 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^mcp-[0-9a-f]{64}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,159}$")
_TEXT = re.compile(r"^[^\x00-\x1f\x7f`$<>|;&]{0,200}$")


class MeasuredCapability(StrEnum):
    """What a benchmark can measure; model-, vendor- and runtime-independent."""

    TEXT_GENERATION_ZH_TW = "TEXT_GENERATION_ZH_TW"
    TEXT_GENERATION_EN = "TEXT_GENERATION_EN"
    STRUCTURED_JSON = "STRUCTURED_JSON"
    SCHEMA_ADHERENCE = "SCHEMA_ADHERENCE"
    INFORMATION_EXTRACTION = "INFORMATION_EXTRACTION"
    REASONING = "REASONING"
    REASONING_EXTENDED = "REASONING_EXTENDED"  # with an explicit reasoning phase enabled
    CODE_GENERATION = "CODE_GENERATION"
    CODE_REPAIR = "CODE_REPAIR"
    SUMMARIZATION = "SUMMARIZATION"
    MULTI_STEP_INSTRUCTION = "MULTI_STEP_INSTRUCTION"
    TOOL_CALL_FORMATTING = "TOOL_CALL_FORMATTING"
    INABILITY_ACKNOWLEDGEMENT = "INABILITY_ACKNOWLEDGEMENT"
    DETERMINISM = "DETERMINISM"
    LONG_CONTEXT_RETRIEVAL = "LONG_CONTEXT_RETRIEVAL"
    CONCURRENT_REQUESTS = "CONCURRENT_REQUESTS"
    COLD_START_LATENCY = "COLD_START_LATENCY"
    WARM_LATENCY = "WARM_LATENCY"
    GENERATION_THROUGHPUT = "GENERATION_THROUGHPUT"
    PROMPT_THROUGHPUT = "PROMPT_THROUGHPUT"
    TIMEOUT_RECOVERY = "TIMEOUT_RECOVERY"
    RELOAD_STABILITY = "RELOAD_STABILITY"


class ObservationStatus(StrEnum):
    MEASURED = "MEASURED"
    NOT_MEASURED = "NOT_MEASURED"  # UNKNOWN: never satisfies a requirement
    INVALID_MEASUREMENT = "INVALID_MEASUREMENT"  # the benchmark did not exercise what it claims to measure


class Dependency(StrEnum):
    DEVICE = "DEVICE"
    RUNTIME = "RUNTIME"
    CONFIGURATION = "CONFIGURATION"
    CONTEXT = "CONTEXT"


class DescriptiveRole(StrEnum):
    """Labels derived from measured evidence. Descriptive only: no policy, planner or router branch reads them."""

    LOCAL_GENERAL_REASONING_CANDIDATE = "LOCAL_GENERAL_REASONING_CANDIDATE"
    LOCAL_ZH_TW_TEXT_CANDIDATE = "LOCAL_ZH_TW_TEXT_CANDIDATE"
    LOCAL_STRUCTURED_OUTPUT_CANDIDATE = "LOCAL_STRUCTURED_OUTPUT_CANDIDATE"
    LOCAL_CODE_ASSIST_CANDIDATE = "LOCAL_CODE_ASSIST_CANDIDATE"
    LOCAL_SUMMARIZATION_CANDIDATE = "LOCAL_SUMMARIZATION_CANDIDATE"
    LOCAL_TOOL_FORMATTING_CANDIDATE = "LOCAL_TOOL_FORMATTING_CANDIDATE"


# A role is derived only when EVERY listed capability was measured and every sample passed.
ROLE_EVIDENCE: Mapping[DescriptiveRole, tuple[MeasuredCapability, ...]] = {
    DescriptiveRole.LOCAL_GENERAL_REASONING_CANDIDATE: (MeasuredCapability.REASONING,
                                                        MeasuredCapability.REASONING_EXTENDED),
    DescriptiveRole.LOCAL_ZH_TW_TEXT_CANDIDATE: (MeasuredCapability.TEXT_GENERATION_ZH_TW,),
    DescriptiveRole.LOCAL_STRUCTURED_OUTPUT_CANDIDATE: (MeasuredCapability.STRUCTURED_JSON,
                                                        MeasuredCapability.SCHEMA_ADHERENCE,
                                                        MeasuredCapability.INFORMATION_EXTRACTION),
    DescriptiveRole.LOCAL_CODE_ASSIST_CANDIDATE: (MeasuredCapability.CODE_GENERATION, MeasuredCapability.CODE_REPAIR),
    DescriptiveRole.LOCAL_SUMMARIZATION_CANDIDATE: (MeasuredCapability.SUMMARIZATION,),
    DescriptiveRole.LOCAL_TOOL_FORMATTING_CANDIDATE: (MeasuredCapability.TOOL_CALL_FORMATTING,),
}


class CandidateState(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


def _label(value: str, code: str) -> str:
    if not _LABEL.fullmatch(value) or ".." in value:
        raise ValueError(code)
    return value


def _is_finite(value: float) -> bool:
    return value - value == 0  # NaN and +/-inf give NaN (the package's import allowlist has no ``math``)


def _finite(value: float | None, code: str) -> float | None:
    if value is not None and not _is_finite(value):
        raise ValueError(code)
    return value


class ModelArtifactIdentity(BaseModel):
    """The immutable artifact. ``artifact_id`` binds the runtime family, manifest digest and layer digests only:
    the same tag with a different manifest is a different artifact, and the tag never identifies one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_family: str  # opaque label of the artifact namespace / runtime kind
    artifact_ref: str  # opaque reference the runtime resolves (e.g. a tag); provenance, never identity
    manifest_digest: str
    layer_digests: tuple[str, ...] = Field(min_length=1, max_length=32)
    model_family: str
    parameter_size: str | None = None
    quantization: str | None = None
    weights_format: str | None = None

    @field_validator("runtime_family", "model_family")
    @classmethod
    def labels(cls, value: str) -> str:
        return _label(value, "ARTIFACT_LABEL_INVALID")

    @field_validator("parameter_size", "quantization", "weights_format")
    @classmethod
    def optional_labels(cls, value: str | None) -> str | None:
        return None if value is None else _label(value, "ARTIFACT_LABEL_INVALID")

    @field_validator("artifact_ref")
    @classmethod
    def reference(cls, value: str) -> str:
        if not _REF.fullmatch(value) or ".." in value or "//" in value:
            raise ValueError("ARTIFACT_REF_INVALID")
        return value

    @field_validator("manifest_digest")
    @classmethod
    def manifest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("MANIFEST_DIGEST_INVALID")
        return value

    @field_validator("layer_digests")
    @classmethod
    def layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _DIGEST.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("LAYER_DIGEST_INVALID")
        return value

    @model_validator(mode="after")
    def manifest_is_not_a_layer(self) -> ModelArtifactIdentity:
        if self.manifest_digest in self.layer_digests:
            raise ValueError("MANIFEST_DIGEST_IS_A_LAYER")
        return self

    @property
    def artifact_id(self) -> str:
        return canonical_hash({"runtime_family": self.runtime_family, "manifest_digest": self.manifest_digest,
                               "layer_digests": sorted(self.layer_digests)})

    @property
    def model_ref(self) -> str:
        """The canonical ``<backend>/<model>@<digest>`` id the existing ``ModelCatalog`` keys descriptions by."""
        return f"{self.runtime_family}/{self.artifact_ref}@{self.manifest_digest}"


class ModelRuntimeBinding(BaseModel):
    """How the artifact was served when measured. Endpoint addresses, paths and API routes are not recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_family: str
    runtime_version: str
    protocol: str  # opaque protocol label of the runtime adapter
    endpoint_scope: str = "LOOPBACK"
    accelerator: AcceleratorRuntime
    device_profile_id: str
    configuration: tuple[tuple[str, str], ...] = Field(default=(), max_length=32)

    @field_validator("runtime_family", "runtime_version", "protocol")
    @classmethod
    def labels(cls, value: str) -> str:
        return _label(value, "BINDING_LABEL_INVALID")

    @field_validator("endpoint_scope")
    @classmethod
    def scope(cls, value: str) -> str:
        if value not in {"LOOPBACK", "PRIVATE_NETWORK"}:
            raise ValueError("BINDING_ENDPOINT_SCOPE_INVALID")
        return value

    @field_validator("device_profile_id")
    @classmethod
    def device(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("DEVICE_PROFILE_ID_INVALID")
        return value

    @field_validator("configuration")
    @classmethod
    def config(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        keys = [key for key, _ in value]
        if len(set(keys)) != len(keys) or any(not _CODE.fullmatch(k) or not _TEXT.fullmatch(v) for k, v in value):
            raise ValueError("BINDING_CONFIGURATION_INVALID")
        return value

    @property
    def binding_id(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class DeviceSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_profile_id: str
    os_family: str
    machine_class: str
    gpu_model: str | None
    gpu_vram_mb: int = Field(ge=0, le=1_048_576)
    cpu_model: str | None
    cpu_logical_cores: int = Field(ge=1, le=4096)
    ram_mb: int = Field(ge=1, le=4_194_304)

    @field_validator("device_profile_id")
    @classmethod
    def device(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("DEVICE_PROFILE_ID_INVALID")
        return value

    @field_validator("os_family", "machine_class", "gpu_model", "cpu_model")
    @classmethod
    def text(cls, value: str | None) -> str | None:
        if value is not None and not _TEXT.fullmatch(value):
            raise ValueError("DEVICE_TEXT_INVALID")
        return value

    @property
    def ram_class(self) -> str:
        """Power-of-two RAM class (e.g. ``RAM_32GB``): coarse and stable across small reporting differences."""
        gib = max(1, round(self.ram_mb / 1024))
        return f"RAM_{1 << max(0, (gib - 1).bit_length())}GB"


class CapabilityEvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_sha256: str
    raw_sha256: str
    summary_sha256: str
    result_sha256: str
    benchmark_version: str
    task_set_digest: str

    @field_validator("archive_sha256", "raw_sha256", "summary_sha256", "result_sha256", "task_set_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("EVIDENCE_DIGEST_INVALID")
        return value

    @field_validator("benchmark_version")
    @classmethod
    def version(cls, value: str) -> str:
        return _label(value, "BENCHMARK_VERSION_INVALID")


class CapabilityObservation(BaseModel):
    """One measurement: counts for checked tasks, or a distribution for latency/throughput, never a bare boolean."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: MeasuredCapability
    status: ObservationStatus
    unit: str
    sample_size: int = Field(default=0, ge=0, le=1_000_000)
    success_count: int = Field(default=0, ge=0, le=1_000_000)
    failure_count: int = Field(default=0, ge=0, le=1_000_000)
    success_lower_bound: float | None = Field(default=None, ge=0.0, le=1.0)  # Wilson 95% lower bound
    measured_value: float | None = None
    distribution: tuple[tuple[str, float], ...] = Field(default=(), max_length=16)
    context_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    confidence: Level = Level.UNKNOWN
    dependencies: tuple[Dependency, ...] = (Dependency.DEVICE, Dependency.RUNTIME, Dependency.CONFIGURATION)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    note: str | None = None
    observed_at: str | None = None

    @field_validator("unit")
    @classmethod
    def unit_label(cls, value: str) -> str:
        return _label(value, "OBSERVATION_UNIT_INVALID")

    @field_validator("measured_value")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        return _finite(value, "OBSERVATION_VALUE_NOT_FINITE")

    @field_validator("distribution")
    @classmethod
    def stats(cls, value: tuple[tuple[str, float], ...]) -> tuple[tuple[str, float], ...]:
        if any(not _CODE.fullmatch(k.upper()) or not _is_finite(v) for k, v in value):
            raise ValueError("OBSERVATION_DISTRIBUTION_INVALID")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REF.fullmatch(item) for item in value):
            raise ValueError("OBSERVATION_EVIDENCE_REF_INVALID")
        return value

    @field_validator("note")
    @classmethod
    def note_text(cls, value: str | None) -> str | None:
        if value is not None and not _TEXT.fullmatch(value):
            raise ValueError("OBSERVATION_NOTE_INVALID")
        return value

    @model_validator(mode="after")
    def consistent(self) -> CapabilityObservation:
        if self.success_count + self.failure_count > self.sample_size:
            raise ValueError("OBSERVATION_COUNTS_INVALID")
        if self.status is not ObservationStatus.MEASURED and (self.success_lower_bound is not None
                                                              or self.measured_value is not None):
            raise ValueError("UNMEASURED_OBSERVATION_CARRIES_A_VALUE")
        return self

    @property
    def all_passed(self) -> bool:
        return (self.status is ObservationStatus.MEASURED and self.sample_size > 0
                and self.success_count == self.sample_size)

    def stat(self, name: str) -> float | None:
        return dict(self.distribution).get(name)


class CapabilityConstraint(BaseModel):
    """A measured limit on where an observation holds (context range, residency, concurrency, evidence gaps)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    value: float | None = None
    unit: str | None = None
    applies_from_context_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    capabilities: tuple[MeasuredCapability, ...] = ()
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)
    note: str | None = None

    @field_validator("code")
    @classmethod
    def code_pattern(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("CONSTRAINT_CODE_INVALID")
        return value

    @field_validator("value")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        return _finite(value, "CONSTRAINT_VALUE_NOT_FINITE")

    @field_validator("unit")
    @classmethod
    def unit_label(cls, value: str | None) -> str | None:
        return None if value is None else _label(value, "CONSTRAINT_UNIT_INVALID")

    @field_validator("note")
    @classmethod
    def note_text(cls, value: str | None) -> str | None:
        if value is not None and not _TEXT.fullmatch(value):
            raise ValueError("CONSTRAINT_NOTE_INVALID")
        return value


class ResourceFootprint(BaseModel):
    """Measured memory need at ``base_context_tokens`` (fully resident), never estimated from parameter counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_context_tokens: int = Field(ge=1, le=10_000_000)
    gpu_memory_mib: int = Field(ge=0, le=1_048_576)
    resident_size_mib: int = Field(ge=0, le=4_194_304)


class ReasoningMode(StrEnum):
    """Whether a separate reasoning phase runs before the visible answer (generic; adapters translate)."""

    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class OutputConstraint(StrEnum):
    UNCONSTRAINED = "UNCONSTRAINED"
    JSON_SCHEMA = "JSON_SCHEMA"  # decoding constrained to a caller-supplied JSON schema


class ExecutionConfig(BaseModel):
    """Capability-critical execution conditions in generic vocabulary.

    Planner-facing: no runtime parameter names. A runtime adapter translates these fields into its own request
    and must refuse a field it cannot apply (``UNVERIFIABLE`` is never treated as compatible)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_window: int = Field(ge=1, le=10_000_000)
    reasoning_mode: ReasoningMode
    temperature: float = Field(ge=0.0, le=2.0)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    output_constraint: OutputConstraint = OutputConstraint.UNCONSTRAINED

    @field_validator("temperature")
    @classmethod
    def finite(cls, value: float) -> float:
        _finite(value, "EXECUTION_TEMPERATURE_NOT_FINITE")
        return value

    def critical(self) -> tuple[int, str, float, int | None, str]:
        """The settings a measured capability depends on (the output budget is compared separately)."""
        return (self.context_window, self.reasoning_mode.value, self.temperature, self.seed,
                self.output_constraint.value)

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class MeasuredExecutionCondition(BaseModel):
    """The execution configuration under which ``capabilities`` were measured, from raw benchmark records.
    ``max_observed_output_tokens`` is the largest completion the measured runs needed (a budget floor)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: tuple[MeasuredCapability, ...] = Field(min_length=1, max_length=64)
    config: ExecutionConfig
    max_observed_output_tokens: int | None = Field(default=None, ge=0, le=1_000_000)
    sample_size: int = Field(default=0, ge=0, le=1_000_000)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("capabilities")
    @classmethod
    def unique(cls, value: tuple[MeasuredCapability, ...]) -> tuple[MeasuredCapability, ...]:
        if len(set(value)) != len(value):
            raise ValueError("EXECUTION_CONDITION_CAPABILITY_DUPLICATE")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REF.fullmatch(item) for item in value):
            raise ValueError("EXECUTION_CONDITION_EVIDENCE_REF_INVALID")
        return value


class ModelCapabilityProfile(BaseModel):
    """Bound to artifact + runtime binding + device + benchmark config + measured execution conditions + evidence
    + time. ``profile_id`` is the canonical hash of everything else, so any change (another manifest, runtime
    version, device, execution condition or evidence) is another profile. Construct with
    ``ModelCapabilityProfile.create``.

        CAPABILITY = MODEL x RUNTIME x DEVICE x EXECUTION CONFIG x EVIDENCE

    A profile without ``execution_conditions`` (built before they were recorded) keeps its identity but supports
    no requirement: the conditions its capabilities hold under are unknown (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = PROFILE_SCHEMA
    profile_id: str
    artifact: ModelArtifactIdentity
    binding: ModelRuntimeBinding
    device: DeviceSummary
    benchmark_config_digest: str
    benchmark_evidence_digest: str
    evidence: CapabilityEvidenceRef
    measured_from: str
    measured_until: str
    observations: tuple[CapabilityObservation, ...] = Field(min_length=1, max_length=MAX_OBSERVATIONS)
    constraints: tuple[CapabilityConstraint, ...] = Field(default=(), max_length=MAX_CONSTRAINTS)
    footprint: ResourceFootprint
    execution_conditions: tuple[MeasuredExecutionCondition, ...] = Field(default=(), max_length=32)
    roles: tuple[DescriptiveRole, ...] = ()
    evidence_level: str
    provenance: tuple[tuple[str, str], ...] = Field(default=(), max_length=32)
    authority: str = "NONE"

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != PROFILE_SCHEMA:
            raise ValueError("CAPABILITY_PROFILE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("benchmark_config_digest", "benchmark_evidence_digest")
    @classmethod
    def digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("PROFILE_DIGEST_INVALID")
        return value

    @field_validator("evidence_level")
    @classmethod
    def level(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("EVIDENCE_LEVEL_INVALID")
        return value

    @field_validator("provenance")
    @classmethod
    def provenance_pairs(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        keys = [key for key, _ in value]
        if len(set(keys)) != len(keys) or any(not _CODE.fullmatch(k) or not _TEXT.fullmatch(v) for k, v in value):
            raise ValueError("PROFILE_PROVENANCE_INVALID")
        return value

    @model_validator(mode="after")
    def bound(self) -> ModelCapabilityProfile:
        if self.authority != "NONE":
            raise ValueError("CAPABILITY_PROFILE_CANNOT_CARRY_AUTHORITY")
        if self.binding.device_profile_id != self.device.device_profile_id:
            raise ValueError("PROFILE_DEVICE_BINDING_MISMATCH")
        if self.binding.runtime_family != self.artifact.runtime_family:
            raise ValueError("PROFILE_RUNTIME_FAMILY_MISMATCH")
        keys = [(item.capability, item.context_tokens) for item in self.observations]
        if len(set(keys)) != len(keys):
            raise ValueError("PROFILE_OBSERVATION_DUPLICATE")
        for role in self.roles:
            if not all(self.observation(cap).all_passed for cap in ROLE_EVIDENCE[role]):
                raise ValueError("PROFILE_ROLE_NOT_SUPPORTED_BY_EVIDENCE")
        measured = {item.capability for item in self.observations if item.status is ObservationStatus.MEASURED}
        if any(cap not in measured for item in self.execution_conditions for cap in item.capabilities):
            raise ValueError("EXECUTION_CONDITION_FOR_UNMEASURED_CAPABILITY")
        critical = [(cap, item.config.critical()) for item in self.execution_conditions for cap in item.capabilities]
        if len(set(critical)) != len(critical):
            raise ValueError("EXECUTION_CONDITION_DUPLICATE")
        if not _PROFILE_ID.fullmatch(self.profile_id) or self.profile_id != profile_id_for(self._body()):
            raise ValueError("PROFILE_ID_MISMATCH")
        return self

    def _body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"profile_id"})

    @classmethod
    def create(cls, **fields: Any) -> ModelCapabilityProfile:
        draft = cls.model_construct(profile_id="", **fields)
        body = {**draft.model_dump(mode="json", exclude={"profile_id"})}
        return cls.model_validate({**body, "profile_id": profile_id_for(body)})

    def observation(self, capability: MeasuredCapability, *,
                    context_tokens: int | None = None) -> CapabilityObservation:
        """The recorded observation, or an explicit NOT_MEASURED one (never a fabricated value)."""
        for item in self.observations:
            if item.capability is capability and (context_tokens is None or item.context_tokens == context_tokens):
                return item
        return CapabilityObservation(capability=capability, status=ObservationStatus.NOT_MEASURED, unit="none",
                                     context_tokens=context_tokens)

    def observations_of(self, capability: MeasuredCapability) -> tuple[CapabilityObservation, ...]:
        return tuple(item for item in self.observations if item.capability is capability)

    def constraint(self, code: str) -> CapabilityConstraint | None:
        return next((item for item in self.constraints if item.code == code), None)


def profile_id_for(body: Mapping[str, Any]) -> str:
    # An empty execution-condition list is omitted, so profiles built before conditions were recorded keep their id.
    hashed = {key: value for key, value in body.items() if not (key == "execution_conditions" and not value)}
    return "mcp-" + canonical_hash({"schema": PROFILE_SCHEMA, **hashed})


def derive_roles(observations: tuple[CapabilityObservation, ...]) -> tuple[DescriptiveRole, ...]:
    by_cap: dict[MeasuredCapability, list[CapabilityObservation]] = {}
    for item in observations:
        by_cap.setdefault(item.capability, []).append(item)
    return tuple(role for role, caps in ROLE_EVIDENCE.items()
                 if all(cap in by_cap and all(o.all_passed for o in by_cap[cap]) for cap in caps))


# --- requirements and assessment (pure; no authorization, no selection) ----------------------------------------

class CapabilityFloor(BaseModel):
    """Every listed sample must come from a MEASURED observation whose Wilson lower bound reaches the floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: MeasuredCapability
    min_success_lower_bound: float = Field(default=0.0, ge=0.0, le=1.0)
    min_samples: int = Field(default=1, ge=1, le=1_000_000)


class CapabilityRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    floors: tuple[CapabilityFloor, ...] = Field(default=(), max_length=32)
    min_context_tokens: int = Field(default=0, ge=0, le=10_000_000)
    max_warm_ttft_p90_s: float | None = Field(default=None, gt=0)
    min_generation_tokens_per_s: float | None = Field(default=None, gt=0)
    concurrent_requests: int = Field(default=1, ge=1, le=1024)
    allow_degraded: bool = False

    @property
    def requirement_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class ExecutionCompatibility(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    MISSING = "MISSING"  # no measured condition, or no effective configuration to compare
    INCOMPATIBLE = "INCOMPATIBLE"  # a capability-critical setting differs from the measured one
    UNVERIFIABLE = "UNVERIFIABLE"  # the runtime cannot apply or confirm a capability-critical setting


MEASURED_EXECUTION_CONFIG_MISSING = "MEASURED_EXECUTION_CONFIG_MISSING"
MEASURED_EXECUTION_CONFIG_INCONSISTENT = "MEASURED_EXECUTION_CONFIG_INCONSISTENT"
MEASURED_EXECUTION_CONFIG_MISMATCH = "MEASURED_EXECUTION_CONFIG_MISMATCH"


def claimed_capabilities(requirement: CapabilityRequirement) -> tuple[MeasuredCapability, ...]:
    """The measured capabilities a requirement relies on (each was measured under some execution condition)."""
    claimed = [floor.capability for floor in requirement.floors]
    if requirement.min_context_tokens:
        claimed.append(MeasuredCapability.LONG_CONTEXT_RETRIEVAL)
    if requirement.max_warm_ttft_p90_s is not None:
        claimed.append(MeasuredCapability.WARM_LATENCY)
    if requirement.min_generation_tokens_per_s is not None:
        claimed.append(MeasuredCapability.GENERATION_THROUGHPUT)
    return tuple(dict.fromkeys(claimed))


def required_execution(profile: ModelCapabilityProfile,
                       requirement: CapabilityRequirement) -> tuple[MeasuredExecutionCondition | None, str | None]:
    """The ONE measured execution condition under which every capability the requirement claims was measured.

    (condition, None) or (None, refusal). No claimed capability, a claimed capability without a recorded condition
    -> MISSING; claimed capabilities measured only under different capability-critical settings -> INCONSISTENT.
    Several shared conditions are resolved only when exactly one is left; otherwise INCONSISTENT (never guessed)."""
    claimed = claimed_capabilities(requirement)
    if not claimed:
        return None, MEASURED_EXECUTION_CONFIG_MISSING
    per_capability: list[dict[tuple[int, str, float, int | None, str], list[MeasuredExecutionCondition]]] = []
    for capability in claimed:
        found: dict[tuple[int, str, float, int | None, str], list[MeasuredExecutionCondition]] = {}
        for item in profile.execution_conditions:
            if capability in item.capabilities:
                found.setdefault(item.config.critical(), []).append(item)
        if not found:
            return None, f"{MEASURED_EXECUTION_CONFIG_MISSING}:{capability.value}"
        per_capability.append(found)
    shared = set(per_capability[0]).intersection(*per_capability[1:])
    if requirement.min_context_tokens:
        shared = {key for key in shared if key[0] >= requirement.min_context_tokens}
    if len({key[1:] for key in shared}) != 1:  # only the context window may differ; the smallest one suffices
        return None, MEASURED_EXECUTION_CONFIG_INCONSISTENT
    key = min(shared)
    conditions = [item for found in per_capability for item in found[key]]
    observed = [item.max_observed_output_tokens for item in conditions]
    return MeasuredExecutionCondition(
        capabilities=claimed, config=conditions[0].config.model_copy(update={
            "max_output_tokens": min(item.config.max_output_tokens for item in conditions)}),
        max_observed_output_tokens=None if None in observed else max(int(v or 0) for v in observed),
        sample_size=sum(item.sample_size for item in conditions),
        evidence_refs=tuple(dict.fromkeys(ref for item in conditions for ref in item.evidence_refs))[:64]), None


def check_execution(measured: MeasuredExecutionCondition | None,
                    effective: ExecutionConfig | None) -> tuple[ExecutionCompatibility, tuple[str, ...]]:
    """Compare the effective configuration with the measured one, field by field (pure; never raises).

    Every capability-critical setting must be equal. The output budget must leave room for the largest completion
    the measured runs needed (or, when that was not recorded, be at least the measured budget)."""
    if measured is None or effective is None:
        return ExecutionCompatibility.MISSING, (MEASURED_EXECUTION_CONFIG_MISSING,)
    want, got = measured.config, effective
    reasons = [f"{MEASURED_EXECUTION_CONFIG_MISMATCH}:{name}" for name, a, b in (
        ("context_window", want.context_window, got.context_window),
        ("reasoning_mode", want.reasoning_mode, got.reasoning_mode),
        ("temperature", want.temperature, got.temperature),
        ("seed", want.seed, got.seed),
        ("output_constraint", want.output_constraint, got.output_constraint)) if a != b]
    floor = measured.max_observed_output_tokens if measured.max_observed_output_tokens is not None \
        else want.max_output_tokens
    if got.max_output_tokens < max(1, floor):
        reasons.append(f"{MEASURED_EXECUTION_CONFIG_MISMATCH}:max_output_tokens")
    return (ExecutionCompatibility.INCOMPATIBLE if reasons else ExecutionCompatibility.COMPATIBLE), tuple(reasons)


class ResourceHeadroom(BaseModel):
    """Current telemetry of the bound device (from the device profiler / runtime health), not of the benchmark."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    free_gpu_memory_mib: int = Field(ge=0, le=1_048_576)
    available_ram_mib: int = Field(ge=0, le=4_194_304)
    model_resident: bool = False


class CandidateAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    state: CandidateState
    reasons: tuple[str, ...]
    requirement_hash: str
    authority: str = "NONE"

    def eligible(self, requirement: CapabilityRequirement) -> bool:
        return self.state is CandidateState.AVAILABLE or (
            self.state is CandidateState.DEGRADED and requirement.allow_degraded)


def evidence_reasons(profile: ModelCapabilityProfile,
                     requirement: CapabilityRequirement) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(rejections, degradations) from measured evidence alone. Missing or invalid evidence always rejects."""
    rejected: list[str] = []
    degraded: list[str] = []
    for floor in requirement.floors:
        found = profile.observations_of(floor.capability) or (profile.observation(floor.capability),)
        for item in found:
            if item.status is ObservationStatus.NOT_MEASURED:
                rejected.append(f"CAPABILITY_UNMEASURED:{floor.capability.value}")
            elif item.status is ObservationStatus.INVALID_MEASUREMENT:
                rejected.append(f"CAPABILITY_MEASUREMENT_INVALID:{floor.capability.value}")
            elif item.sample_size < floor.min_samples:
                rejected.append(f"CAPABILITY_SAMPLE_TOO_SMALL:{floor.capability.value}")
            elif item.success_lower_bound is None or item.success_lower_bound < floor.min_success_lower_bound:
                rejected.append(f"CAPABILITY_BELOW_FLOOR:{floor.capability.value}")
    if requirement.min_context_tokens:
        covering = sorted((o for o in profile.observations_of(MeasuredCapability.LONG_CONTEXT_RETRIEVAL)
                           if o.all_passed and o.context_tokens and o.context_tokens >= requirement.min_context_tokens),
                          key=lambda o: o.context_tokens or 0)
        if not covering:
            rejected.append("CONTEXT_EXCEEDS_MEASURED_EVIDENCE")
        else:
            reach = covering[0].context_tokens or 0
            degraded.extend(f"CONTEXT_CONSTRAINT:{c.code}" for c in profile.constraints
                            if c.applies_from_context_tokens is not None and c.applies_from_context_tokens <= reach)
    if requirement.max_warm_ttft_p90_s is not None:
        p90 = profile.observation(MeasuredCapability.WARM_LATENCY).stat("p90")
        if p90 is None:
            rejected.append("LATENCY_UNMEASURED")
        elif p90 > requirement.max_warm_ttft_p90_s:
            rejected.append("LATENCY_ABOVE_BOUND")
    if requirement.min_generation_tokens_per_s is not None:
        median = profile.observation(MeasuredCapability.GENERATION_THROUGHPUT).stat("median")
        if median is None:
            rejected.append("THROUGHPUT_UNMEASURED")
        elif median < requirement.min_generation_tokens_per_s:
            rejected.append("THROUGHPUT_BELOW_BOUND")
    if requirement.concurrent_requests > 1:
        measured = profile.observation(MeasuredCapability.CONCURRENT_REQUESTS)
        if measured.status is not ObservationStatus.MEASURED or measured.measured_value is None:
            rejected.append("CONCURRENCY_UNMEASURED")
        elif measured.measured_value < requirement.concurrent_requests:
            rejected.append("CONCURRENCY_EXCEEDS_MEASURED_EVIDENCE")
        elif profile.constraint("CONCURRENCY_SERIALIZED") is not None:
            degraded.append("CONCURRENCY_SERIALIZED")
    if not rejected and claimed_capabilities(requirement):
        # evidence that holds only under unknown or mutually inconsistent execution conditions supports nothing
        # (a requirement claiming no measured capability is judged on resources here; the execution gate still
        # refuses to run anything without one bound measured condition)
        _, unbound = required_execution(profile, requirement)
        if unbound is not None:
            rejected.append(unbound)
    return tuple(rejected), tuple(degraded)


def assess(profile: ModelCapabilityProfile, requirement: CapabilityRequirement, *,
           runtime_state: CandidateState, device_state: CandidateState,
           headroom: ResourceHeadroom | None) -> CandidateAssessment:
    """Evidence + current runtime/device state + resource pressure -> AVAILABLE / DEGRADED / UNAVAILABLE.

    Authorization is NOT an input: an assessment never makes a candidate authorized, and an AVAILABLE assessment
    of an unauthorized candidate still cannot be routed (the caller applies the existing authorization authority).
    """
    rejected, degraded = (list(part) for part in evidence_reasons(profile, requirement))
    for name, state in (("RUNTIME", runtime_state), ("DEVICE", device_state)):
        if state is CandidateState.UNAVAILABLE:
            rejected.append(f"{name}_UNAVAILABLE")
        elif state is CandidateState.DEGRADED:
            degraded.append(f"{name}_DEGRADED")
    if headroom is None:
        rejected.append("RESOURCE_TELEMETRY_UNKNOWN")
    elif not headroom.model_resident:
        need = max(profile.footprint.gpu_memory_mib, profile.footprint.resident_size_mib)
        if headroom.free_gpu_memory_mib >= need:
            pass
        elif headroom.free_gpu_memory_mib + headroom.available_ram_mib >= need:
            degraded.append("RESOURCE_PRESSURE_PARTIAL_OFFLOAD")
        else:
            rejected.append("INSUFFICIENT_MEMORY")
    state = (CandidateState.UNAVAILABLE if rejected else CandidateState.DEGRADED if degraded
             else CandidateState.AVAILABLE)
    return CandidateAssessment(profile_id=profile.profile_id, state=state, reasons=tuple((*rejected, *degraded)),
                               requirement_hash=requirement.requirement_hash)


def graph_projection(profile: ModelCapabilityProfile) -> dict[str, Any]:
    """Nodes and edges for an observability projection of existing authority (no graph is created or owned)."""
    profile_node = f"capability-profile:{profile.profile_id}"
    binding_node = f"runtime-binding:{profile.binding.binding_id}"
    device_node = f"device-profile:{profile.device.device_profile_id}"
    evidence_node = f"benchmark-evidence:{profile.benchmark_evidence_digest}"
    capabilities = sorted({item.capability.value for item in profile.observations
                           if item.status is ObservationStatus.MEASURED})
    edges = [[f"capability:{cap}", "SATISFIED_BY", profile_node] for cap in capabilities]
    edges += [[profile_node, "BOUND_TO", binding_node], [binding_node, "EXECUTES_ON", device_node],
              [profile_node, "SUPPORTED_BY", evidence_node]]
    nodes = sorted({node for edge in edges for node in (edge[0], edge[2])})
    return {"schema": "pados.resources.capability-profile-projection.v1", "authority": "NONE",
            "nodes": nodes, "edges": edges}


# --- live runtime binding (pure; generic; no authorization, no selection) ---------------------------------------
#
# A ``LiveRuntimeBinding`` joins one REGISTERED profile with what is CURRENTLY observed about its runtime and device.
# Observations come from runtime adapters and the device profiler (``pados.local_intelligence.live``); this contract
# only compares opaque labels and digests for equality, so it never needs a runtime, vendor or model name, path or
# API. Binding eligibility is description: authorization stays with the existing authority and selection with the
# Worker Router. A runtime's own name for the artifact (``runtime_ref``) is carried for invocation only; the artifact
# is matched by manifest digest, never by tag.

class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"  # observed, but older than the policy allows: never trusted for eligibility
    UNAVAILABLE = "UNAVAILABLE"  # never observed (or the observation failed)


class BindingPolicy(BaseModel):
    """Operator policy for live binding. Defaults are deliberately conservative starting points, not universal
    constants: ``max_age_seconds`` bounds how long one observation may be trusted; the reserves keep memory back for
    the operating system and other workloads before a profile's measured footprint is compared with free memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_age_seconds: float = Field(default=120.0, gt=0, le=86_400)
    ram_reserve_mib: int = Field(default=2048, ge=0, le=4_194_304)
    gpu_reserve_mib: int = Field(default=256, ge=0, le=1_048_576)


class RuntimeArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_ref: str  # the runtime's own name for the artifact (opaque; invocation only)
    manifest_digest: str | None  # as the runtime reports it; None = unknown (never matches)

    @field_validator("runtime_ref")
    @classmethod
    def reference(cls, value: str) -> str:
        if not _REF.fullmatch(value) or ".." in value:
            raise ValueError("RUNTIME_REF_INVALID")
        return value

    @field_validator("manifest_digest")
    @classmethod
    def manifest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("MANIFEST_DIGEST_INVALID")
        return value


class RuntimeObservation(BaseModel):
    """One live runtime as an adapter observed it (health, version, the artifacts it holds, which are resident)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_id: str
    runtime_family: str
    runtime_version: str | None
    state: CandidateState
    reasons: tuple[str, ...] = ()
    artifacts: tuple[RuntimeArtifact, ...] = Field(default=(), max_length=256)
    resident_refs: tuple[str, ...] = Field(default=(), max_length=256)
    observed_at: datetime | None

    @field_validator("runtime_id", "runtime_family")
    @classmethod
    def labels(cls, value: str) -> str:
        return _label(value, "RUNTIME_LABEL_INVALID")

    @field_validator("reasons")
    @classmethod
    def codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CODE.fullmatch(item.split(":", 1)[0]) for item in value):
            raise ValueError("RUNTIME_REASON_INVALID")
        return value


class DeviceObservation(BaseModel):
    """One live device: its stable identity (same derivation as the measured profile's) and current headroom."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_profile_id: str | None  # None = identity could not be established
    state: CandidateState
    reasons: tuple[str, ...] = ()
    free_gpu_memory_mib: int | None = Field(default=None, ge=0, le=1_048_576)
    available_ram_mib: int | None = Field(default=None, ge=0, le=4_194_304)
    observed_at: datetime | None

    @field_validator("device_profile_id")
    @classmethod
    def device(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("DEVICE_PROFILE_ID_INVALID")
        return value


class LiveRuntimeBinding(BaseModel):
    """Profile + live runtime + live device -> the states ``assess`` consumes, with every refusal reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    artifact_id: str
    manifest_digest: str
    measured_binding_id: str
    runtime_id: str | None
    runtime_version: str | None
    runtime_ref: str | None  # what to invoke; None when the pinned artifact is not present
    device_profile_id: str | None
    runtime_freshness: Freshness
    device_freshness: Freshness
    runtime_state: CandidateState
    device_state: CandidateState
    headroom: ResourceHeadroom | None
    reasons: tuple[str, ...]
    observed_at: datetime | None
    policy_hash: str
    authority: str = "NONE"

    @property
    def bindable(self) -> bool:
        """Identity, presence, compatibility and freshness hold (resource pressure is judged by ``assess``)."""
        return (self.runtime_ref is not None and self.runtime_state is not CandidateState.UNAVAILABLE
                and self.device_state is not CandidateState.UNAVAILABLE)

    @property
    def binding_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json", exclude={"observed_at", "headroom"}))


def freshness_of(observed_at: datetime | None, now: datetime, max_age_seconds: float) -> Freshness:
    if observed_at is None:
        return Freshness.UNAVAILABLE
    age = (now - observed_at).total_seconds()
    return Freshness.FRESH if -5.0 <= age <= max_age_seconds else Freshness.STALE  # future stamps are not fresh


def bind_live(profile: ModelCapabilityProfile, runtimes: tuple[RuntimeObservation, ...],
              device: DeviceObservation | None, *, now: datetime, policy: BindingPolicy) -> LiveRuntimeBinding:
    """Bind ``profile`` to the live runtime of its measured family that holds its exact manifest digest, on the
    device whose stable identity equals the measured one. Every unmet condition is a reason; nothing is guessed."""
    reasons: list[str] = []
    family = [r for r in runtimes if r.runtime_family == profile.binding.runtime_family]
    holding = [r for r in family if any(a.manifest_digest == profile.artifact.manifest_digest for a in r.artifacts)]
    runtime = min(holding or family, key=lambda r: (r.state is not CandidateState.AVAILABLE, r.runtime_id),
                  default=None)
    runtime_fresh = freshness_of(runtime.observed_at if runtime else None, now, policy.max_age_seconds)
    runtime_ref: str | None = None
    if runtime is None:
        reasons.append("RUNTIME_NOT_DISCOVERED")
        runtime_state = CandidateState.UNAVAILABLE
    else:
        runtime_state = runtime.state
        reasons.extend(runtime.reasons)
        if runtime_fresh is not Freshness.FRESH:
            reasons.append(f"RUNTIME_STATE_{runtime_fresh.value}")
            runtime_state = CandidateState.UNAVAILABLE
        if runtime.runtime_version != profile.binding.runtime_version:
            reasons.append("RUNTIME_VERSION_NOT_MEASURED")  # evidence holds for the measured version only
            runtime_state = CandidateState.UNAVAILABLE
        matches = sorted(a.runtime_ref for a in runtime.artifacts
                         if a.manifest_digest == profile.artifact.manifest_digest)
        if not matches:
            reasons.append("MODEL_ARTIFACT_NOT_PRESENT")  # a same-named artifact with another digest is not it
            runtime_state = CandidateState.UNAVAILABLE
        else:
            runtime_ref = matches[0]
    device_fresh = freshness_of(device.observed_at if device else None, now, policy.max_age_seconds)
    headroom: ResourceHeadroom | None = None
    if device is None or device_fresh is Freshness.UNAVAILABLE:
        reasons.append("DEVICE_STATE_UNAVAILABLE")
        device_state = CandidateState.UNAVAILABLE
    else:
        device_state = device.state
        reasons.extend(device.reasons)
        if device_fresh is not Freshness.FRESH:
            reasons.append("DEVICE_STATE_STALE")
            device_state = CandidateState.UNAVAILABLE
        if device.device_profile_id != profile.device.device_profile_id:
            reasons.append("DEVICE_NOT_MEASURED")  # another device (or changed driver/OS): evidence does not apply
            device_state = CandidateState.UNAVAILABLE
        if device_fresh is Freshness.FRESH and device.free_gpu_memory_mib is not None \
                and device.available_ram_mib is not None:
            headroom = ResourceHeadroom(
                free_gpu_memory_mib=max(0, device.free_gpu_memory_mib - policy.gpu_reserve_mib),
                available_ram_mib=max(0, device.available_ram_mib - policy.ram_reserve_mib),
                model_resident=runtime_ref is not None and runtime is not None and runtime_ref in runtime.resident_refs)
    return LiveRuntimeBinding(
        profile_id=profile.profile_id, artifact_id=profile.artifact.artifact_id,
        manifest_digest=profile.artifact.manifest_digest, measured_binding_id=profile.binding.binding_id,
        runtime_id=runtime.runtime_id if runtime else None,
        runtime_version=runtime.runtime_version if runtime else None, runtime_ref=runtime_ref,
        device_profile_id=device.device_profile_id if device else None, runtime_freshness=runtime_fresh,
        device_freshness=device_fresh, runtime_state=runtime_state, device_state=device_state, headroom=headroom,
        reasons=tuple(dict.fromkeys(reasons)),
        observed_at=min((t for t in (runtime.observed_at if runtime else None,
                                     device.observed_at if device else None) if t is not None), default=None),
        policy_hash=canonical_hash(policy.model_dump(mode="json")))
