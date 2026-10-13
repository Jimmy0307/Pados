"""Stage B M3: measured, evidence-bound model capability profiles and their registration in the ModelCatalog."""
from __future__ import annotations

import pytest
from capability_profile_support import LICENSE, digest, make_profile, observations
from pydantic import ValidationError

from pados.resources.capability_profile import (
    CandidateState,
    CapabilityFloor,
    CapabilityRequirement,
    DescriptiveRole,
    MeasuredCapability,
    ModelCapabilityProfile,
    ObservationStatus,
    ResourceHeadroom,
    assess,
    evidence_reasons,
    graph_projection,
)
from pados.resources.hardware import AcceleratorRuntime
from pados.resources.models import ModelCatalog, profile_from_capability

OK = CandidateState.AVAILABLE
ROOMY = ResourceHeadroom(free_gpu_memory_mib=8000, available_ram_mib=16000)


def requirement(*floors: CapabilityFloor, **values: object) -> CapabilityRequirement:
    return CapabilityRequirement(floors=floors, **values)  # type: ignore[arg-type]


def floor(capability: MeasuredCapability, lower: float = 0.0, samples: int = 1) -> CapabilityFloor:
    return CapabilityFloor(capability=capability, min_success_lower_bound=lower, min_samples=samples)


# --- identity ---------------------------------------------------------------------------------------------------

def test_model_identity_differs_so_profile_identity_differs() -> None:
    a, b = make_profile(), make_profile(manifest_seed="f")
    assert a.artifact.artifact_id != b.artifact.artifact_id and a.profile_id != b.profile_id
    assert make_profile().profile_id == a.profile_id  # canonical: same evidence -> same id


def test_same_tag_different_manifest_is_a_different_artifact_and_catalog_entry() -> None:
    a, b = make_profile(), make_profile(manifest_seed="9")
    assert a.artifact.artifact_ref == b.artifact.artifact_ref
    assert a.artifact.model_ref != b.artifact.model_ref and a.artifact.artifact_id != b.artifact.artifact_id
    catalog = ModelCatalog([profile_from_capability(a, license=LICENSE)]).register_capability_profile(a)
    with pytest.raises(ValueError, match="CAPABILITY_PROFILE_MODEL_NOT_REGISTERED"):
        catalog.register_capability_profile(b)  # evidence of another manifest never attaches under the same tag


def test_tag_never_identifies_the_artifact() -> None:
    a, b = make_profile(), make_profile(artifact_ref="renamed:tag")
    assert a.artifact.artifact_id == b.artifact.artifact_id  # digests identify; the tag is provenance only
    assert a.profile_id != b.profile_id  # the profile still binds what was asked for


@pytest.mark.parametrize("change", [{"runtime_version": "1.0.1"}, {"device_id": "e" * 64}, {"layer_seed": "c"}])
def test_runtime_device_and_layers_are_bound_into_the_profile_id(change: dict[str, str]) -> None:
    assert make_profile(**change).profile_id != make_profile().profile_id  # type: ignore[arg-type]


def test_tampered_or_forged_profiles_are_refused() -> None:
    profile = make_profile()
    document = profile.model_dump(mode="json")
    warm = next(o for o in document["observations"] if o["capability"] == "WARM_LATENCY")
    warm["measured_value"] = 0.01  # a better latency than was measured
    with pytest.raises(ValidationError, match="PROFILE_ID_MISMATCH"):
        ModelCapabilityProfile.model_validate(document)
    with pytest.raises(ValidationError, match="CANNOT_CARRY_AUTHORITY"):
        ModelCapabilityProfile.model_validate({**profile.model_dump(mode="json"), "authority": "ROUTABLE"})
    forged = ModelCapabilityProfile.model_construct(**{**dict(profile), "profile_id": "mcp-" + "0" * 64})
    with pytest.raises(ValidationError):
        ModelCatalog([profile_from_capability(profile, license=LICENSE)], [forged])


def test_roles_must_be_supported_by_evidence() -> None:
    profile = make_profile()
    assert DescriptiveRole.LOCAL_SUMMARIZATION_CANDIDATE not in profile.roles  # 1/2 summaries passed
    assert DescriptiveRole.LOCAL_TOOL_FORMATTING_CANDIDATE in profile.roles
    with pytest.raises(ValidationError, match="ROLE_NOT_SUPPORTED"):
        make_profile(roles=(DescriptiveRole.LOCAL_SUMMARIZATION_CANDIDATE,))


def test_unknown_remains_unknown_and_no_benchmark_means_no_capability() -> None:
    profile = make_profile(obs=observations(zh=None))
    missing = profile.observation(MeasuredCapability.TEXT_GENERATION_ZH_TW)
    assert missing.status is ObservationStatus.NOT_MEASURED and missing.success_lower_bound is None
    assert DescriptiveRole.LOCAL_ZH_TW_TEXT_CANDIDATE not in profile.roles
    rejected, _ = evidence_reasons(profile, requirement(floor(MeasuredCapability.TEXT_GENERATION_ZH_TW)))
    assert rejected == ("CAPABILITY_UNMEASURED:TEXT_GENERATION_ZH_TW",)
    measured = profile.observation(MeasuredCapability.REASONING).model_dump()
    with pytest.raises(ValidationError, match="UNMEASURED_OBSERVATION_CARRIES_A_VALUE"):
        type(missing).model_validate({**measured, "status": "NOT_MEASURED"})  # no value without a measurement


# --- assessment ---------------------------------------------------------------------------------------------------

def test_required_capability_absent_rejects() -> None:
    result = assess(make_profile(), requirement(floor(MeasuredCapability.CODE_REPAIR)), runtime_state=OK,
                    device_state=OK, headroom=ROOMY)
    assert result.state is CandidateState.UNAVAILABLE and "CAPABILITY_UNMEASURED:CODE_REPAIR" in result.reasons


def test_quality_floor_uses_the_wilson_lower_bound() -> None:
    profile = make_profile()
    met = assess(profile, requirement(floor(MeasuredCapability.REASONING, 0.4)), runtime_state=OK, device_state=OK,
                 headroom=ROOMY)
    unmet = assess(profile, requirement(floor(MeasuredCapability.REASONING, 0.5)), runtime_state=OK, device_state=OK,
                   headroom=ROOMY)
    small = assess(profile, requirement(floor(MeasuredCapability.REASONING, 0.0, samples=10)), runtime_state=OK,
                   device_state=OK, headroom=ROOMY)
    assert met.state is OK and unmet.reasons == ("CAPABILITY_BELOW_FLOOR:REASONING",)
    assert small.reasons == ("CAPABILITY_SAMPLE_TOO_SMALL:REASONING",)


def test_invalid_measurement_never_satisfies() -> None:
    result = assess(make_profile(), requirement(floor(MeasuredCapability.TIMEOUT_RECOVERY)), runtime_state=OK,
                    device_state=OK, headroom=ROOMY)
    assert result.reasons == ("CAPABILITY_MEASUREMENT_INVALID:TIMEOUT_RECOVERY",)


def test_context_requirement_beyond_measured_evidence_rejects_and_offload_degrades() -> None:
    profile = make_profile()
    fine = assess(profile, requirement(min_context_tokens=8000), runtime_state=OK, device_state=OK, headroom=ROOMY)
    offload = assess(profile, requirement(min_context_tokens=12000), runtime_state=OK, device_state=OK, headroom=ROOMY)
    beyond = assess(profile, requirement(min_context_tokens=32768), runtime_state=OK, device_state=OK, headroom=ROOMY)
    assert fine.state is OK
    assert offload.state is CandidateState.DEGRADED and offload.reasons == ("CONTEXT_CONSTRAINT:GPU_PARTIAL_OFFLOAD",)
    assert not offload.eligible(requirement(min_context_tokens=12000))
    assert offload.eligible(requirement(min_context_tokens=12000, allow_degraded=True))
    assert beyond.reasons == ("CONTEXT_EXCEEDS_MEASURED_EVIDENCE",)


def test_latency_throughput_and_concurrency_bounds() -> None:
    profile = make_profile()

    def reasons(**values: object) -> tuple[str, ...]:
        return assess(profile, requirement(**values), runtime_state=OK, device_state=OK, headroom=ROOMY).reasons

    assert reasons(max_warm_ttft_p90_s=0.4) == ("LATENCY_ABOVE_BOUND",)
    assert reasons(max_warm_ttft_p90_s=1.0) == ()
    assert reasons(min_generation_tokens_per_s=80.0) == ("THROUGHPUT_BELOW_BOUND",)
    assert reasons(concurrent_requests=8) == ("CONCURRENCY_EXCEEDS_MEASURED_EVIDENCE",)
    assert reasons(concurrent_requests=4) == ("CONCURRENCY_SERIALIZED",)
    bare = make_profile(obs=observations(warm=None))
    assert assess(bare, requirement(max_warm_ttft_p90_s=9.0), runtime_state=OK, device_state=OK,
                  headroom=ROOMY).reasons == ("LATENCY_UNMEASURED",)


def test_runtime_device_and_resource_pressure_change_state() -> None:
    profile, plain = make_profile(), requirement()

    def state(runtime: CandidateState = OK, device: CandidateState = OK,
              headroom: ResourceHeadroom | None = ROOMY) -> tuple[CandidateState, tuple[str, ...]]:
        result = assess(profile, plain, runtime_state=runtime, device_state=device, headroom=headroom)
        return result.state, result.reasons

    assert state() == (OK, ())
    assert state(runtime=CandidateState.UNAVAILABLE) == (CandidateState.UNAVAILABLE, ("RUNTIME_UNAVAILABLE",))
    assert state(device=CandidateState.UNAVAILABLE) == (CandidateState.UNAVAILABLE, ("DEVICE_UNAVAILABLE",))
    assert state(runtime=CandidateState.DEGRADED) == (CandidateState.DEGRADED, ("RUNTIME_DEGRADED",))
    assert state(headroom=None) == (CandidateState.UNAVAILABLE, ("RESOURCE_TELEMETRY_UNKNOWN",))
    tight = ResourceHeadroom(free_gpu_memory_mib=2000, available_ram_mib=9000)
    assert state(headroom=tight) == (CandidateState.DEGRADED, ("RESOURCE_PRESSURE_PARTIAL_OFFLOAD",))
    starved = ResourceHeadroom(free_gpu_memory_mib=1000, available_ram_mib=1000)
    assert state(headroom=starved) == (CandidateState.UNAVAILABLE, ("INSUFFICIENT_MEMORY",))
    loaded = ResourceHeadroom(free_gpu_memory_mib=0, available_ram_mib=0, model_resident=True)
    assert state(headroom=loaded) == (OK, ())


def test_assessment_carries_no_authority() -> None:
    result = assess(make_profile(), requirement(), runtime_state=OK, device_state=OK, headroom=ROOMY)
    assert result.authority == "NONE"
    assert not {name for name in type(result).model_fields if "authoriz" in name or "grant" in name}


# --- catalog registration and planning --------------------------------------------------------------------------

def test_registration_is_description_only_and_consistent_with_evidence() -> None:
    profile = make_profile()
    description = profile_from_capability(profile, license=LICENSE)
    assert description.context_length == 16384 and description.vram_mb == 6000
    assert description.tool_use and description.structured_output
    assert description.runtimes == (AcceleratorRuntime.CUDA,)
    catalog = ModelCatalog([description]).register_capability_profile(profile)
    assert catalog.capability_profile(description.model_id) == profile
    assert catalog.register(profile_from_capability(make_profile(manifest_seed="7"), license=LICENSE)) \
        .capability_profile(description.model_id) == profile  # registering another model keeps the evidence
    overclaim = description.model_copy(update={"context_length": 40960})
    with pytest.raises(ValueError, match="MODEL_DESCRIPTION_EXCEEDS_EVIDENCE:CONTEXT_LENGTH"):
        ModelCatalog([overclaim], [profile])
    assert catalog.withdraw_capability_profile(description.model_id).capability_profile(description.model_id) is None
    with pytest.raises(ValueError, match="CAPABILITY_PROFILE_DUPLICATE"):
        catalog.register_capability_profile(profile)


def test_graph_projection_is_a_projection_without_authority() -> None:
    projection = graph_projection(make_profile())
    assert projection["authority"] == "NONE"
    relations = {edge[1] for edge in projection["edges"]}
    assert relations == {"SATISFIED_BY", "BOUND_TO", "EXECUTES_ON", "SUPPORTED_BY"}
    assert all(not node.startswith("capability:TIMEOUT") for node in projection["nodes"])  # invalid != satisfied


def test_digest_helper_is_well_formed() -> None:
    assert digest("a").startswith("sha256:") and len(digest("a")) == 71
