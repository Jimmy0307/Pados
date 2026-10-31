# SPDX-License-Identifier: AGPL-3.0-only
"""STRUCTURED_PROBLEM_SOLVING as a Capability Pack V1 (first profile: 8D).

Input: one evidence-pack artifact (``problem_solving.evidence-pack/v1``). Parameters: ``{"profile": "8d"}``.
An injected, UNTRUSTED ``ClaimProvider`` drafts claims (for example a model reached through the existing Worker
Router delegate -- this pack never calls a model or network itself); an optional ``Critic`` reviews the report.
The adapter grounds the claims deterministically, assembles the abstaining report and writes three canonical
artifacts -- the evidence pack, the claim set and the report -- so the REFERENCE_CHECK hook can re-derive the
report from its own outputs. Provider unavailability is a retryable closed failure; malformed provider or critic
output fails closed. The report is a candidate (``semantic_authority = NONE``): acceptance remains the existing
ResultCandidate -> governed acceptance -> CAS path.
"""
from __future__ import annotations

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
from pados.packs.problem_solving.contracts import (
    PROFILES,
    ClaimSet,
    EvidencePack,
    GroundingStatus,
    ProblemSolvingError,
    ProblemSolvingProfile,
    ProblemSolvingReport,
    SectionStatus,
)
from pados.packs.problem_solving.pipeline import (
    CriticVerdict,
    admit_evidence,
    build_report,
    parse_claims,
    verify_report,
)

PACK_ID = "pados.structured-problem-solving"
CAPABILITY_ID = "STRUCTURED_PROBLEM_SOLVING"
ADAPTER_ID = "problem-solving.grounded"
INPUT_CONTRACT = "problem_solving.evidence-pack/v1"
OUTPUT_CONTRACT = "problem_solving.report/v1"
CHECKER_ID = "problem-solving.rederive"
KIND_EVIDENCE = "problem_solving.evidence_pack"
KIND_CLAIMS = "problem_solving.claims"
KIND_REPORT = "problem_solving.report"
JSON_MEDIA_TYPE = "application/json"
RUNTIME_VERSION = "1.0.0"
DEVICE = "cpu"
RULES_VERSION = "pados.problem_solving.grounding.v1"
CONFIG_DIGEST = canonical_hash({"rules": RULES_VERSION, "profiles": sorted(
    f"{profile.profile_id}@{profile.version}" for profile in PROFILES.values())})
MAX_ARTIFACT_BYTES = 1024 * 1024


class ClaimProviderUnavailable(RuntimeError):
    """The claim provider cannot be reached right now (retryable; carries no content)."""


class ClaimProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def draft_claims(self, pack: EvidencePack, profile: ProblemSolvingProfile) -> object:
        """Untrusted raw output; parsed strictly by the pack."""


class Critic(Protocol):
    def review(self, pack: EvidencePack, claims: ClaimSet, report: ProblemSolvingReport) -> object:
        """Untrusted raw verdict ``{"agrees": bool, "reason_code": str}``; advisory only."""


CAPABILITY = CapabilityDescriptor(
    capability_id=CAPABILITY_ID, version="1", family=CapabilityFamily.DOCUMENT_INTELLIGENCE,
    operation="SOLVE_STRUCTURED_PROBLEM", input_contract=INPUT_CONTRACT, output_contract=OUTPUT_CONTRACT,
    determinism=Determinism.PROBABILISTIC, modalities=(Modality.TEXT,),
    max_classification=DataClassification.CONFIDENTIAL, network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.REFERENCE_CHECK),
    failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
    description="Evidence-grounded structured problem solving with abstention (8D profile)")

ADAPTER = AdapterDescriptor(
    adapter_id=ADAPTER_ID, capability_id=CAPABILITY_ID, version="1", backend="python",
    determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.CONFIDENTIAL,
    network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo(spdx="AGPL-3.0-only"),
    lifecycle=LifecycleState.EXPERIMENTAL)

EVIDENCE_KEYS = (*CORE_EVIDENCE, "output_contract", "report_status", "claims_grounded", "claims_rejected",
                 "sections_abstained", "evidence_pack_digest", "claim_provider_id")

MANIFEST = CapabilityPackManifest(
    pack_id=PACK_ID, version="1",
    provider=PackProvider(provider_id="pados.problem-solving", runtime_id="python",
                          runtime_version=RUNTIME_VERSION),
    provenance=PackProvenance(source=PackSource.IN_TREE, license=LicenseInfo(spdx="AGPL-3.0-only"),
                              publisher="pados"),
    capability_ids=(CAPABILITY_ID,), capabilities=(CAPABILITY,), adapters=(ADAPTER,),
    runtime=PackRuntime(runtime_type=RuntimeType.IN_PROCESS, device=(ResourceRequirement(cpu_cores=1),),
                        execution_config_digest=CONFIG_DIGEST, timeout_seconds=120,
                        network=NetworkRequirement.LOCAL_ONLY),
    security=PackSecurity(classification_ceiling=DataClassification.CONFIDENTIAL, egress=Egress.NONE),
    evidence_requirements=EVIDENCE_KEYS,
    verification_hooks=(CheckerSpec(output_contract=OUTPUT_CONTRACT, checker_id=CHECKER_ID),),
    initial_state=CapabilityState.EXPERIMENTAL,
    failure_codes=(FailureCodeSpec(code="PS_INPUT_INVALID"), FailureCodeSpec(code="PS_PARAMETERS_INVALID"),
                   FailureCodeSpec(code="PS_EVIDENCE_PACK_INVALID"),
                   FailureCodeSpec(code="PS_EVIDENCE_DIGEST_STALE"),
                   FailureCodeSpec(code="PS_EVIDENCE_SOURCE_UNAUTHORIZED"),
                   FailureCodeSpec(code="PS_PROVIDER_UNAVAILABLE", retryable=True),
                   FailureCodeSpec(code="PS_PROVIDER_FAILED"),
                   FailureCodeSpec(code="PS_PROVIDER_OUTPUT_MALFORMED"),
                   FailureCodeSpec(code="PS_CRITIC_OUTPUT_MALFORMED"),
                   FailureCodeSpec(code="PS_CLAIM_SECTION_UNKNOWN")))


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def load_evidence_pack(payload: bytes) -> EvidencePack:
    """Strict, canonical load of an evidence-pack artifact (closed code)."""
    try:
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError("too large")
        pack = EvidencePack.model_validate(json.loads(payload.decode("ascii")))
    except (ValidationError, ValueError, UnicodeDecodeError, RecursionError):
        pack = None
    if pack is None or canonical_json(pack.model_dump(mode="json")) != payload:
        raise ProblemSolvingError("PS_EVIDENCE_PACK_INVALID")
    return pack


PARAMETER_KEYS = frozenset({"profile", "evidence_pack_digest", "authorized_sources"})
MAX_AUTHORIZED_SOURCES = 16
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,79}$")


def _parameters(parameters: Mapping[str, Any]) -> tuple[ProblemSolvingProfile, str, frozenset[str]]:
    """Task-owned parameters: the profile, the pinned evidence-pack digest and the comma-separated authorized
    sources (stage parameters are flat scalars). Anything missing, extra or malformed fails closed."""
    if set(parameters) != PARAMETER_KEYS:
        raise ProblemSolvingError("PS_PARAMETERS_INVALID")
    profile, pinned, sources = (parameters["profile"], parameters["evidence_pack_digest"],
                                parameters["authorized_sources"])
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ProblemSolvingError("PS_PARAMETERS_INVALID")
    if not isinstance(pinned, str) or not _SHA256.fullmatch(pinned) or not isinstance(sources, str):
        raise ProblemSolvingError("PS_PARAMETERS_INVALID")
    names = sources.split(",")
    if (not 1 <= len(names) <= MAX_AUTHORIZED_SOURCES or len(set(names)) != len(names)
            or any(not _SOURCE.fullmatch(name) for name in names)):
        raise ProblemSolvingError("PS_PARAMETERS_INVALID")
    return PROFILES[profile], pinned, frozenset(names)


class ProblemSolvingAdapter:
    """Registered only wrapped in ``GovernedPackAdapter`` (``register_pack``); reached only via the executor."""

    def __init__(self, provider: ClaimProvider, *, critic: Critic | None = None) -> None:
        self._descriptor = ADAPTER
        self._provider = provider
        self._critic = critic

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except ArtifactAccessDenied:
            raise
        except CapabilityAdapterError:
            raise
        except ProblemSolvingError as exc:
            code, retryable = exc.code, False
        except ClaimProviderUnavailable:
            code, retryable = "PS_PROVIDER_UNAVAILABLE", True
        raise CapabilityAdapterError(code, retryable=retryable) from None  # outside the handler: no content

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.capability_id != CAPABILITY_ID or len(request.inputs) != 1:
            raise ProblemSolvingError("PS_INPUT_INVALID")
        profile, pinned, authorized = _parameters(request.parameters)
        source = request.inputs[0]
        if source.kind != KIND_EVIDENCE or source.media_type != JSON_MEDIA_TYPE:
            raise ProblemSolvingError("PS_INPUT_INVALID")
        pack = load_evidence_pack(context.artifacts.get(source))
        admit_evidence(pack, pinned_digest=pinned, authorized_sources=authorized)
        claims = parse_claims(self._draft(pack, profile))
        report = build_report(pack, claims, profile)
        if self._critic is not None:
            report = build_report(pack, claims, profile, critic=self._review(pack, claims, report))
        refs = (context.artifacts.put(canonical_json(pack.model_dump(mode="json")), media_type=JSON_MEDIA_TYPE,
                                      kind=KIND_EVIDENCE),
                context.artifacts.put(canonical_json(claims.model_dump(mode="json")), media_type=JSON_MEDIA_TYPE,
                                      kind=KIND_CLAIMS),
                context.artifacts.put(canonical_json(report.model_dump(mode="json")), media_type=JSON_MEDIA_TYPE,
                                      kind=KIND_REPORT))
        rejected = sum(1 for verdict in report.verdicts if verdict.status is not GroundingStatus.GROUNDED)
        evidence: dict[str, str | int | float | bool | None] = {
            "runtime_version": RUNTIME_VERSION, "device": DEVICE, "config_digest": CONFIG_DIGEST,
            "provider_id": MANIFEST.provider.provider_id, "artifact_digest": outputs_digest(refs),
            "output_contract": OUTPUT_CONTRACT, "report_status": report.status.value,
            "claims_grounded": len(report.verdicts) - rejected, "claims_rejected": rejected,
            "sections_abstained": sum(1 for s in report.sections if s.status is SectionStatus.ABSTAINED),
            "evidence_pack_digest": pack.digest, "claim_provider_id": self._provider_id()}
        return StageOutput(outputs=refs, metrics={"claims": float(len(claims.claims))}, evidence=evidence)

    def _draft(self, pack: EvidencePack, profile: ProblemSolvingProfile) -> object:
        try:
            return self._provider.draft_claims(pack, profile)
        except ClaimProviderUnavailable:
            raise
        except Exception:  # noqa: BLE001 - untrusted provider: closed failure, nothing echoed
            pass
        raise ProblemSolvingError("PS_PROVIDER_FAILED")

    def _review(self, pack: EvidencePack, claims: ClaimSet, report: ProblemSolvingReport) -> CriticVerdict:
        critic = self._critic
        assert critic is not None
        try:
            raw = critic.review(pack, claims, report)
            if isinstance(raw, dict) and set(raw) == {"agrees", "reason_code"}:
                return CriticVerdict(agrees=raw["agrees"], reason_code=raw["reason_code"])
        except Exception:  # noqa: BLE001 - untrusted critic: closed failure, nothing echoed
            pass
        raise ProblemSolvingError("PS_CRITIC_OUTPUT_MALFORMED")

    def _provider_id(self) -> str:
        provider_id = getattr(self._provider, "provider_id", "")
        return provider_id if isinstance(provider_id, str) and _PROVIDER_ID.fullmatch(provider_id) else "unknown"


def _by_kind(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> dict[str, bytes] | None:
    found = {ref.kind: data for ref, data in outputs}
    if len(outputs) != 3 or set(found) != {KIND_EVIDENCE, KIND_CLAIMS, KIND_REPORT}:
        return None
    return found


def report_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``problem_solving.report/v1``: the three canonical artifacts, each strictly valid."""
    found = _by_kind(outputs)
    if found is None:
        return False
    try:
        load_evidence_pack(found[KIND_EVIDENCE])
        claims = ClaimSet.model_validate(json.loads(found[KIND_CLAIMS].decode("ascii")))
        report = ProblemSolvingReport.model_validate(json.loads(found[KIND_REPORT].decode("ascii")))
    except (ProblemSolvingError, ValidationError, ValueError, UnicodeDecodeError, RecursionError):
        return False
    return (canonical_json(claims.model_dump(mode="json")) == found[KIND_CLAIMS]
            and canonical_json(report.model_dump(mode="json")) == found[KIND_REPORT])


def report_reference_check(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """REFERENCE_CHECK: the report is exactly what its own evidence pack and claim set deterministically yield."""
    if not report_output_valid(outputs):
        return False
    found = _by_kind(outputs)
    assert found is not None
    pack = load_evidence_pack(found[KIND_EVIDENCE])
    claims = ClaimSet.model_validate(json.loads(found[KIND_CLAIMS].decode("ascii")))
    report = ProblemSolvingReport.model_validate(json.loads(found[KIND_REPORT].decode("ascii")))
    return verify_report(pack, claims, report)
