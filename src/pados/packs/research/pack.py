# SPDX-License-Identifier: AGPL-3.0-only
"""RESEARCH_CAMPAIGN as a Capability Pack V1 consumer over an external AI-Scientist-v3-shaped research runtime.

The research runtime is replaceable and is NEVER authority. PADOS owns the task, policy, resource selection,
authorization, evidence, verification, receipts, ``ResultCandidate``, semantic acceptance, CAS and learning:

* an operator ``ResearchRuntimeProfile`` pins the upstream revision and image digest, the allowlisted agents, the
  public egress hosts and the secret REFERENCES; ``research_pack_manifest`` turns one profile into one
  content-addressed Pack V1 version (``CONTAINER`` runtime -- external code never runs in the PADOS process;
  ``EXPERIMENTAL`` until a recorded live-UAT outcome supports a VALIDATED proposal);
* the task pins the canonical campaign request by digest; the adapter refuses a stale request and any agent outside
  the profile before the runtime is touched;
* the runtime's output is untrusted: ``build_run_manifest`` admits it fail-closed and PADOS writes the manifest and
  the promoted artifacts; SCHEMA and REFERENCE_CHECK checkers re-derive the manifest from those very outputs;
* ``research_projection`` maps a verified manifest onto the bounded work-protocol provider-response fields: only
  EVIDENCED claims are presented, and only a COMPLETED run without review flags can complete a goal.

No runtime is launched by this module: a container runner is supplied by the operator behind ``ResearchRuntime``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    LifecycleState,
    Modality,
    ResourceRequirement,
    VerificationMethod,
)
from pados.capability.pack import (
    CORE_EVIDENCE,
    CapabilityPackManifest,
    CheckerSpec,
    Egress,
    FailureCodeSpec,
    PackProvenance,
    PackProvider,
    PackRuntime,
    PackSecurity,
    PackSource,
    RuntimeType,
    outputs_digest,
)
from pados.capability_loop.contracts import CapabilityState
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.packs.research.contracts import (
    CampaignRequest,
    ClaimStatus,
    PhaseState,
    ResearchError,
    ResearchRunManifest,
    ResearchRuntimeProfile,
    ResearchRuntimeTimeout,
    ResearchRuntimeUnavailable,
    RunStatus,
)
from pados.packs.research.manifest import build_run_manifest, verify_run_manifest

__all__ = ["ResearchRuntimeProfile", "ResearchRuntimeTimeout", "ResearchRuntimeUnavailable", "outputs_digest"]

PACK_ID = "pados.research-campaign"
CAPABILITY_ID = "RESEARCH_CAMPAIGN"
INPUT_CONTRACT = "research.campaign-request/v1"
OUTPUT_CONTRACT = "research.run-manifest/v1"
SCHEMA_CHECKER_ID = "research.manifest.schema"
REFERENCE_CHECKER_ID = "research.manifest.rederive"
KIND_REQUEST = "research.campaign_request"
KIND_MANIFEST = "research.run_manifest"
JSON_MEDIA_TYPE = "application/json"
ARTIFACT_MEDIA_TYPE = "application/octet-stream"
RUNTIME_VERSION = "agsci-v0"
DEVICE = "container"
MAX_REQUEST_BYTES = 64 * 1024
PARAMETER_KEYS = frozenset({"profile_id", "request_digest"})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

FAILURES = (FailureCodeSpec(code="RESEARCH_RUNTIME_UNAVAILABLE", retryable=True),
            *(FailureCodeSpec(code=code) for code in (
                "RESEARCH_REQUEST_INVALID", "RESEARCH_PARAMETERS_INVALID", "RESEARCH_REQUEST_DIGEST_STALE",
                "RESEARCH_AGENT_NOT_ALLOWED", "RESEARCH_RUNTIME_TIMEOUT", "RESEARCH_RUNTIME_FAILED",
                "RESEARCH_MANIFEST_MALFORMED", "RESEARCH_ARTIFACT_PATH_UNAUTHORIZED", "RESEARCH_ARTIFACT_LIMIT",
                "RESEARCH_PHASE_INVALID", "RESEARCH_PHASE_ORDER_INVALID", "RESEARCH_PHASE_OUTPUT_MISSING",
                "RESEARCH_PHASE_OUTPUT_UNEXPECTED", "RESEARCH_TASK_SNAPSHOT_MISSING", "RESEARCH_RESUME_MISMATCH",
                "RESEARCH_EGRESS_VIOLATION", "RESEARCH_FINAL_EVALUATION_MALFORMED")))
EVIDENCE_KEYS = (*CORE_EVIDENCE, "output_contract", "run_status", "phases_completed", "artifacts_promoted",
                 "artifacts_quarantined", "claims_evidenced", "claims_not_evidenced", "review_required",
                 "upstream_run_id", "request_digest")


class ResearchRuntime(Protocol):
    """The operator-supplied runner (e.g. an isolated container). Untrusted: its output is admitted by PADOS."""

    @property
    def provider_id(self) -> str: ...

    def available(self) -> bool: ...

    def run(self, campaign: CampaignRequest, files_budget: Mapping[str, int]) -> object: ...


def _network(profile: ResearchRuntimeProfile) -> NetworkRequirement:
    return NetworkRequirement.WAN_REQUIRED if profile.egress_hosts else NetworkRequirement.LOCAL_ONLY


def capability_descriptor(profile: ResearchRuntimeProfile) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=CAPABILITY_ID, version="1", family=CapabilityFamily.EXECUTION, operation="RUN_RESEARCH_CAMPAIGN",
        input_contract=INPUT_CONTRACT, output_contract=OUTPUT_CONTRACT, determinism=Determinism.PROBABILISTIC,
        modalities=(Modality.TEXT,), max_classification=DataClassification.INTERNAL,
        network_requirement=_network(profile), verification=(VerificationMethod.SCHEMA, VerificationMethod.REFERENCE_CHECK),
        failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
        description="Governed external research campaign (structural evidence, not scientific truth)")


def adapter_descriptor(profile: ResearchRuntimeProfile) -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=profile.adapter_id, capability_id=CAPABILITY_ID, version="1", backend="container",
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.INTERNAL,
        network_requirement=_network(profile), license=LicenseInfo(spdx="AGPL-3.0-only"),
        lifecycle=LifecycleState.EXPERIMENTAL)


def config_digest(profile: ResearchRuntimeProfile) -> str:
    return canonical_hash(profile.model_dump(mode="json"))


def research_pack_manifest(profile: ResearchRuntimeProfile) -> CapabilityPackManifest:
    """One operator profile = one content-addressed Pack V1 version (validated fail-closed by the contract)."""
    egress = Egress.ALLOWLIST if profile.egress_hosts else Egress.NONE
    return CapabilityPackManifest(
        pack_id=PACK_ID, version=f"0.{profile.profile_id}",
        provider=PackProvider(provider_id="pados.research", runtime_id="agsci", runtime_version=RUNTIME_VERSION),
        provenance=PackProvenance(source=PackSource.OPERATOR_SUPPLIED, license=LicenseInfo(spdx="Apache-2.0"),
                                  source_digest=profile.image_digest, publisher="sakanaai"),
        capability_ids=(CAPABILITY_ID,), capabilities=(capability_descriptor(profile),),
        adapters=(adapter_descriptor(profile),),
        runtime=PackRuntime(runtime_type=RuntimeType.CONTAINER, device=(ResourceRequirement(cpu_cores=1),),
                            execution_config_digest=config_digest(profile), timeout_seconds=profile.timeout_seconds,
                            network=_network(profile)),
        security=PackSecurity(secret_refs=profile.secret_refs, allowed_roots=("research.run-root",),
                              classification_ceiling=DataClassification.INTERNAL, egress=egress,
                              egress_hosts=profile.egress_hosts),
        evidence_requirements=EVIDENCE_KEYS,
        verification_hooks=(CheckerSpec(output_contract=OUTPUT_CONTRACT, checker_id=REFERENCE_CHECKER_ID),),
        initial_state=CapabilityState.EXPERIMENTAL, failure_codes=FAILURES)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_request(campaign: CampaignRequest) -> bytes:
    return canonical_json(campaign.model_dump(mode="json"))


def task_parameters(profile: ResearchRuntimeProfile, request_payload: bytes) -> dict[str, str]:
    """The task-owned stage parameters: which profile, and the digest of the exact request the task approved."""
    return {"profile_id": profile.profile_id, "request_digest": hashlib.sha256(request_payload).hexdigest()}


def artifact_kind(root_kind: str) -> str:
    return f"research.artifact.{root_kind}"


class ResearchCampaignAdapter:
    """Registered only wrapped in ``GovernedPackAdapter`` (``register_pack``); reached only via the executor."""

    def __init__(self, profile: ResearchRuntimeProfile, runtime: ResearchRuntime) -> None:
        self._profile = profile
        self._runtime = runtime
        self._descriptor = adapter_descriptor(profile)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._runtime.available() is True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except (ArtifactAccessDenied, CapabilityAdapterError):
            raise
        except ResearchError as exc:
            code, retryable = exc.code, False
        raise CapabilityAdapterError(code, retryable=retryable) from None  # outside the handler: no content

    def _campaign(self, request: StageRequest, context: StageContext) -> tuple[CampaignRequest, str]:
        if request.capability_id != CAPABILITY_ID or len(request.inputs) != 1:
            raise ResearchError("RESEARCH_REQUEST_INVALID")
        parameters = request.parameters
        if (set(parameters) != PARAMETER_KEYS or parameters["profile_id"] != self._profile.profile_id
                or not isinstance(parameters["request_digest"], str)
                or not _SHA256.fullmatch(parameters["request_digest"])):
            raise ResearchError("RESEARCH_PARAMETERS_INVALID")
        source = request.inputs[0]
        if source.kind != KIND_REQUEST or source.media_type != JSON_MEDIA_TYPE or source.size_bytes > MAX_REQUEST_BYTES:
            raise ResearchError("RESEARCH_REQUEST_INVALID")
        payload = context.artifacts.get(source)
        if hashlib.sha256(payload).hexdigest() != parameters["request_digest"]:
            raise ResearchError("RESEARCH_REQUEST_DIGEST_STALE")  # the task approved a different request
        try:
            campaign = CampaignRequest.model_validate(json.loads(payload.decode("ascii")))
        except (ValidationError, ValueError, UnicodeDecodeError, RecursionError):
            campaign = None
        if campaign is None or canonical_request(campaign) != payload:
            raise ResearchError("RESEARCH_REQUEST_INVALID")
        if (campaign.agent not in self._profile.agents
                or (campaign.model is not None and campaign.model not in self._profile.models)
                or (campaign.preset is not None and campaign.preset not in self._profile.presets)):
            raise ResearchError("RESEARCH_AGENT_NOT_ALLOWED")  # agent, model and preset are all profile-allowlisted
        return campaign, parameters["request_digest"]

    def _run(self, campaign: CampaignRequest) -> object:
        budget = {"max_files": 256, "max_file_bytes": 8 * 1024 * 1024, "timeout_seconds": self._profile.timeout_seconds}
        try:
            return self._runtime.run(campaign, budget)
        except ResearchRuntimeUnavailable:
            failure = CapabilityAdapterError("RESEARCH_RUNTIME_UNAVAILABLE", retryable=True)
        except ResearchRuntimeTimeout:
            failure = CapabilityAdapterError("RESEARCH_RUNTIME_TIMEOUT")
        except Exception:  # noqa: BLE001 - untrusted runtime: closed failure, nothing echoed
            failure = CapabilityAdapterError("RESEARCH_RUNTIME_FAILED")
        raise failure from None

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        campaign, request_digest = self._campaign(request, context)
        manifest, promoted = build_run_manifest(self._run(campaign), campaign=campaign, profile=self._profile,
                                                request_digest=request_digest)
        refs = [context.artifacts.put(canonical_json(manifest.model_dump(mode="json")), media_type=JSON_MEDIA_TYPE,
                                      kind=KIND_MANIFEST)]
        refs += [context.artifacts.put(promoted[entry.relative_path], media_type=ARTIFACT_MEDIA_TYPE,
                                       kind=artifact_kind(entry.kind.value)) for entry in manifest.artifacts]
        evidenced = sum(1 for claim in manifest.claims if claim.status is ClaimStatus.EVIDENCED)
        evidence: dict[str, str | int | float | bool | None] = {
            "runtime_version": RUNTIME_VERSION, "device": DEVICE, "config_digest": config_digest(self._profile),
            "provider_id": "pados.research", "artifact_digest": outputs_digest(tuple(refs)),
            "output_contract": OUTPUT_CONTRACT, "run_status": manifest.status.value,
            "phases_completed": sum(1 for p in manifest.phases if p.state is PhaseState.COMPLETED),
            "artifacts_promoted": len(manifest.artifacts), "artifacts_quarantined": manifest.quarantined_count,
            "claims_evidenced": evidenced, "claims_not_evidenced": len(manifest.claims) - evidenced,
            "review_required": manifest.review_required, "upstream_run_id": manifest.upstream_run_id,
            "request_digest": request_digest}
        return StageOutput(outputs=tuple(refs), metrics={"artifacts": float(len(refs))}, evidence=evidence)


def _unpack(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> tuple[ResearchRunManifest, dict[str, bytes]] | None:
    """The manifest and its promoted bytes (by relative path), matched by kind, digest and size; else ``None``."""
    manifests = [(ref, data) for ref, data in outputs if ref.kind == KIND_MANIFEST]
    if len(manifests) != 1:
        return None
    try:
        manifest = ResearchRunManifest.model_validate(json.loads(manifests[0][1].decode("ascii")))
    except (ValidationError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if canonical_json(manifest.model_dump(mode="json")) != manifests[0][1]:
        return None
    remaining = [(ref, data) for ref, data in outputs if ref.kind != KIND_MANIFEST]
    if len(remaining) != len(manifest.artifacts):
        return None
    promoted: dict[str, bytes] = {}
    for entry in manifest.artifacts:
        match = next((item for item in remaining if item[0].kind == artifact_kind(entry.kind.value)
                      and item[0].sha256 == entry.sha256 and len(item[1]) == entry.size_bytes
                      and hashlib.sha256(item[1]).hexdigest() == entry.sha256), None)
        if match is None:
            return None
        remaining.remove(match)
        promoted[entry.relative_path] = match[1]
    return manifest, promoted


def run_manifest_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``research.run-manifest/v1``: one canonical manifest, every artifact exactly as listed."""
    return _unpack(outputs) is not None


def run_manifest_reference_check(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """REFERENCE_CHECK: the manifest re-derives from its own promoted artifacts (metrics, claims, status)."""
    unpacked = _unpack(outputs)
    return unpacked is not None and verify_run_manifest(*unpacked)


def research_projection(outputs: Sequence[tuple[ArtifactRef, bytes]], *, goal_id: str) -> dict[str, Any]:
    """Project VERIFIED stage outputs onto the bounded provider-response fields ``work_protocol`` normalizes.

    The manifest is re-derived from its own promoted artifacts first (the REFERENCE_CHECK); outputs that do not
    verify are ``RESEARCH_MANIFEST_UNVERIFIED`` -- a caller cannot project a manifest it merely holds.

    Content is rendered from PADOS-derived tokens only (phase states, final-evaluation metrics, EVIDENCED claims);
    nothing the runtime wrote as prose is presented. Only a COMPLETED run with no unsupported claim completes the
    goal; PARTIAL is a partial goal; FAILED or review-required runs leave it unresolved. Task, authorization,
    receipt, provenance and acceptance fields are supplied by PADOS when it normalizes, composes and verifies.
    """
    unpacked = _unpack(outputs)
    if unpacked is None or not verify_run_manifest(*unpacked):
        raise ResearchError("RESEARCH_MANIFEST_UNVERIFIED")
    manifest = unpacked[0]
    phases = ", ".join(f"{p.phase.value}={p.state.value}" for p in manifest.phases)
    supported = "; ".join(f"{c.metric} = {c.value}" for c in manifest.claims if c.status is ClaimStatus.EVIDENCED)
    content = f"Research run {manifest.upstream_run_id} ({manifest.status.value}): {phases}."
    if supported:
        content += f" Evidenced by the final evaluation: {supported}."
    complete = manifest.status is RunStatus.COMPLETED and not manifest.review_required
    partial = manifest.status is RunStatus.PARTIAL and not manifest.review_required
    unresolved = not complete and not partial
    failure = None
    if unresolved:
        failure = "QUALITY_INSUFFICIENT" if manifest.review_required else "CAPABILITY_LIMIT"
    tag = canonical_hash({"goal_id": goal_id, "manifest": manifest.model_dump(mode="json")})[:24]
    return {
        "completed_goals": (goal_id,) if complete else (),
        "partial_goals": (goal_id,) if partial else (),
        "unresolved_goals": (goal_id,) if unresolved else (),
        "contributions": ({"contribution_id": f"research.{tag}", "goal_id": goal_id, "content": content,
                           "conflict_key": None},),
        "evidence": tuple({"evidence_id": f"research.{e.kind.value}.{e.sha256[:12]}", "digest": e.sha256,
                           "source_ref": f"research:{manifest.profile_id}"} for e in manifest.artifacts),
        "metrics": {"run_status": manifest.status.value, "artifacts": len(manifest.artifacts),
                    "claims_evidenced": sum(1 for c in manifest.claims if c.status is ClaimStatus.EVIDENCED)},
        "failure_reason": failure,
    }
