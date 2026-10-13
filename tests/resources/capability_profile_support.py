"""Synthetic measured profiles for tests (no evidence archive, no runtime, no model)."""
from __future__ import annotations

from typing import Any

from pados.capability.model import Distribution, Level, LicenseInfo
from pados.resources.capability_profile import (
    CapabilityConstraint,
    CapabilityEvidenceRef,
    CapabilityObservation,
    DeviceSummary,
    ExecutionConfig,
    MeasuredCapability,
    MeasuredExecutionCondition,
    ModelArtifactIdentity,
    ModelCapabilityProfile,
    ModelRuntimeBinding,
    ObservationStatus,
    OutputConstraint,
    ReasoningMode,
    ResourceFootprint,
    derive_roles,
)
from pados.resources.hardware import AcceleratorRuntime

DEVICE_ID = "d" * 64
LICENSE = LicenseInfo(spdx="Apache-2.0", engine_version="1.0", distribution=Distribution.RUNTIME_OPTIONAL)


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def counted(capability: MeasuredCapability, passed: int, total: int, *, lower: float,
            context: int | None = None) -> CapabilityObservation:
    return CapabilityObservation(capability=capability, status=ObservationStatus.MEASURED, unit="checked_tasks",
                                 sample_size=total, success_count=passed, failure_count=total - passed,
                                 success_lower_bound=lower, measured_value=passed / total, context_tokens=context,
                                 confidence=Level.LOW, evidence_refs=("task",))


def timed(capability: MeasuredCapability, median: float, p90: float, *, unit: str = "s",
          context: int | None = 8192) -> CapabilityObservation:
    return CapabilityObservation(capability=capability, status=ObservationStatus.MEASURED, unit=unit, sample_size=5,
                                 success_count=5, measured_value=median,
                                 distribution=(("median", median), ("p90", p90), ("min", median), ("max", p90)),
                                 context_tokens=context, confidence=Level.LOW, evidence_refs=("warm_1",))


def observations(**overrides: CapabilityObservation | None) -> tuple[CapabilityObservation, ...]:
    base = {
        "zh": counted(MeasuredCapability.TEXT_GENERATION_ZH_TW, 2, 2, lower=0.3424),
        "en": counted(MeasuredCapability.TEXT_GENERATION_EN, 2, 2, lower=0.3424),
        "json": counted(MeasuredCapability.STRUCTURED_JSON, 1, 1, lower=0.2065),
        "reason": counted(MeasuredCapability.REASONING, 3, 3, lower=0.4385),
        "summary": counted(MeasuredCapability.SUMMARIZATION, 1, 2, lower=0.0945),
        "tool": counted(MeasuredCapability.TOOL_CALL_FORMATTING, 2, 2, lower=0.3424),
        "ctx8": counted(MeasuredCapability.LONG_CONTEXT_RETRIEVAL, 2, 2, lower=0.3424, context=8192),
        "ctx16": counted(MeasuredCapability.LONG_CONTEXT_RETRIEVAL, 2, 2, lower=0.3424, context=16384),
        "warm": timed(MeasuredCapability.WARM_LATENCY, 0.46, 0.5),
        "tps": timed(MeasuredCapability.GENERATION_THROUGHPUT, 51.0, 51.5, unit="tokens_per_s"),
        "conc": CapabilityObservation(capability=MeasuredCapability.CONCURRENT_REQUESTS,
                                      status=ObservationStatus.MEASURED, unit="requests", sample_size=2,
                                      success_count=2, measured_value=4.0, confidence=Level.LOW),
        "timeout": CapabilityObservation(capability=MeasuredCapability.TIMEOUT_RECOVERY,
                                         status=ObservationStatus.INVALID_MEASUREMENT, unit="none"),
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key)
        else:
            base[key] = value
    return tuple(base.values())


SCHEMA_CONSTRAINED = frozenset({MeasuredCapability.STRUCTURED_JSON, MeasuredCapability.SCHEMA_ADHERENCE,
                                MeasuredCapability.INFORMATION_EXTRACTION, MeasuredCapability.REASONING,
                                MeasuredCapability.REASONING_EXTENDED})


def measured_config(capability: MeasuredCapability, context: int | None = None, **overrides: Any) -> ExecutionConfig:
    """The synthetic measured execution configuration of one capability (same shape as a benchmark's)."""
    values: dict[str, Any] = dict(
        context_window=context or 8192,
        reasoning_mode=ReasoningMode.ENABLED if capability is MeasuredCapability.REASONING_EXTENDED
        else ReasoningMode.DISABLED, temperature=0.0, seed=42, max_output_tokens=768,
        output_constraint=OutputConstraint.JSON_SCHEMA if capability in SCHEMA_CONSTRAINED
        else OutputConstraint.UNCONSTRAINED)
    values.update(overrides)
    return ExecutionConfig(**values)


def conditions_for(found: tuple[CapabilityObservation, ...], *,
                   observed_tokens: int = 70) -> tuple[MeasuredExecutionCondition, ...]:
    """One measured execution condition per MEASURED observation (capability x context)."""
    return tuple(MeasuredExecutionCondition(
        capabilities=(item.capability,), config=measured_config(item.capability, item.context_tokens),
        max_observed_output_tokens=observed_tokens, sample_size=max(1, item.sample_size), evidence_refs=("task",))
        for item in found if item.status is ObservationStatus.MEASURED)


def make_profile(*, artifact_ref: str = "example-model:small", manifest_seed: str = "a", layer_seed: str = "b",
                 runtime_family: str = "runtime-x", runtime_version: str = "1.0.0", model_family: str = "family-x",
                 device_id: str = DEVICE_ID, obs: tuple[CapabilityObservation, ...] | None = None,
                 constraints: tuple[CapabilityConstraint, ...] | None = None, gpu_memory_mib: int = 6000,
                 **extra: Any) -> ModelCapabilityProfile:
    found = obs if obs is not None else observations()
    extra.setdefault("roles", derive_roles(found))
    extra.setdefault("execution_conditions", conditions_for(found))
    return ModelCapabilityProfile.create(
        artifact=ModelArtifactIdentity(runtime_family=runtime_family, artifact_ref=artifact_ref,
                                       manifest_digest=digest(manifest_seed), layer_digests=(digest(layer_seed),),
                                       model_family=model_family, quantization="Q4_K_M"),
        binding=ModelRuntimeBinding(runtime_family=runtime_family, runtime_version=runtime_version, protocol="native",
                                    accelerator=AcceleratorRuntime.CUDA, device_profile_id=device_id),
        device=DeviceSummary(device_profile_id=device_id, os_family="LINUX", machine_class="DESKTOP/DISCRETE_GPU",
                             gpu_model="GPU", gpu_vram_mb=8192, cpu_model="CPU", cpu_logical_cores=16,
                             ram_mb=32768),
        benchmark_config_digest="c" * 64, benchmark_evidence_digest="e" * 64,
        evidence=CapabilityEvidenceRef(archive_sha256="1" * 64, raw_sha256="2" * 64, summary_sha256="3" * 64,
                                       result_sha256="4" * 64, benchmark_version="bench-1",
                                       task_set_digest="5" * 64),
        measured_from="2026-10-07T18:40:13+00:00", measured_until="2026-10-07T18:44:03+00:00",
        observations=found,
        constraints=constraints if constraints is not None else (
            CapabilityConstraint(code="GPU_PARTIAL_OFFLOAD", value=0.78, unit="gpu_fraction",
                                 applies_from_context_tokens=16384),
            CapabilityConstraint(code="CONCURRENCY_SERIALIZED", value=1.0, unit="effective_parallelism")),
        footprint=ResourceFootprint(base_context_tokens=8192, gpu_memory_mib=gpu_memory_mib,
                                    resident_size_mib=gpu_memory_mib - 100),
        evidence_level="MACHINE_CAPTURED_RAW_EVIDENCE_VERIFIED", **extra)
