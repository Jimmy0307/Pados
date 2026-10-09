# SPDX-License-Identifier: AGPL-3.0-only
"""Model candidate audit: typed, basis-tagged facts and the three pre-download gates.

A candidate record keeps every fact with its basis (``UPSTREAM_FACT``, ``MODEL_CARD_CLAIM``, ``COMMUNITY_CLAIM``,
``PADOS_MEASURED``, ``UNKNOWN``) and source; facts of different bases are never merged. The gates are
evidence checks that must all PASS before an operator may download a model:

* ``license_audit``    - an OSI license from an accepted allowlist, read from an authoritative artifact, with the
                         license text shipped in the artifact the runtime will pull; custom licenses are
                         ``REVIEW_REQUIRED`` (never an automatic PASS).
* ``provenance_audit`` - publisher, upstream repository and artifact digests are upstream facts; the weights are the
                         publisher's original or a quantization of them by a trusted distributor; merges and
                         refusal-ablated derivatives fail.
* ``size_fit_audit``   - the estimated GPU-resident footprint (weights + KV cache + runtime reserve) fits the
                         MEASURED device, evaluated by the existing ``StaticResourceInventory`` and against the
                         measured free VRAM.

A PASS here is not authorization, registration or routability; it only allows the model to become AVAILABLE
for an operator-run download. Behaviour labels are descriptive metadata and no gate reads them.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.capability.model import Distribution, LicenseInfo, Modality
from pados.local_intelligence.device import DeviceProfile, GpuStatus, to_hardware_profile
from pados.local_intelligence.probes import Basis
from pados.resources.hardware import AcceleratorRuntime
from pados.resources.inventory import StaticResourceInventory
from pados.resources.models import ModelRole, ModelRuntimeProfile

AUDIT_SCHEMA = "pados.local-intelligence.model-candidate.v1"
ACCEPTED_LICENSES = frozenset({"Apache-2.0", "MIT", "BSD-3-Clause"})
TRUSTED_DISTRIBUTORS = frozenset({"registry.ollama.ai/library"})  # the Ollama library namespace, not user uploads
RUNTIME_RESERVE_MIB = 768  # CUDA context, compute buffers and allocator slack (measured in M2, not assumed later)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{1,79}$")


class CapabilityRole(StrEnum):
    """What a measured (runtime, model) pair may serve. Planner routes to roles, never to brand names."""

    FAST_LOCAL = "FAST_LOCAL"
    PRIMARY_LOCAL_REASONER = "PRIMARY_LOCAL_REASONER"
    CODE_LOCAL = "CODE_LOCAL"
    VISION_LOCAL = "VISION_LOCAL"
    HEAVY_LOCAL = "HEAVY_LOCAL"
    CLOUD_REASONER = "CLOUD_REASONER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    OCR = "OCR"
    ALGORITHM = "ALGORITHM"


class BehaviorLabel(StrEnum):
    """Descriptive only. Security never depends on model refusal behaviour, and no policy reads these."""

    STANDARD_ALIGNED = "STANDARD_ALIGNED"
    LOW_REFUSAL = "LOW_REFUSAL"
    ABLITERATED = "ABLITERATED"
    USER_CONTROLLED_DERIVATIVE = "USER_CONTROLLED_DERIVATIVE"
    RESEARCH_ONLY = "RESEARCH_ONLY"


class ModelLifecycleState(StrEnum):
    """Distinct evidence states of a model on a device; none implies another."""

    DISCOVERED = "DISCOVERED"  # a runtime lists it
    AUDITED = "AUDITED"  # candidate facts collected with bases
    AVAILABLE = "AVAILABLE"  # license, provenance and size gates passed: an operator may download it
    DOWNLOADED = "DOWNLOADED"  # present in the runtime with the audited digest
    BENCHMARKED = "BENCHMARKED"  # measured on this device with a versioned benchmark
    REGISTERED = "REGISTERED"  # described to the existing Capability Registry
    AUTHORIZED = "AUTHORIZED"  # the existing grant authority permits it for a scope
    ROUTABLE = "ROUTABLE"  # healthy + authorized + registered: the existing router may select it
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    REVOKED = "REVOKED"  # terminal


class WeightsKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    QUANTIZED_FROM_ORIGINAL = "QUANTIZED_FROM_ORIGINAL"
    FINETUNE = "FINETUNE"
    MERGED = "MERGED"
    ABLITERATED = "ABLITERATED"
    UNKNOWN = "UNKNOWN"


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any = None
    basis: Basis
    source: str | None = Field(default=None, max_length=500)


def fact(value: Any, basis: Basis, source: str | None = None) -> Fact:
    return Fact(value=value, basis=basis, source=source)


UNKNOWN_FACT = Fact(value=None, basis=Basis.UNKNOWN)


class ArchitectureFacts(BaseModel):
    """What the KV-cache estimate needs (from the upstream ``config.json``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    num_layers: int = Field(ge=1, le=512)
    num_kv_heads: int = Field(ge=1, le=512)
    head_dim: int = Field(ge=8, le=1024)
    source: str = Field(max_length=500)


class ModelCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = AUDIT_SCHEMA
    candidate_id: str
    canonical_name: Fact
    publisher: Fact
    upstream_repository: Fact
    base_model: Fact = UNKNOWN_FACT
    model_family: Fact = UNKNOWN_FACT
    parameter_count: Fact = UNKNOWN_FACT
    architecture: Fact = UNKNOWN_FACT
    context_window: Fact = UNKNOWN_FACT
    license_spdx: Fact
    license_url: Fact = UNKNOWN_FACT
    commercial_use: Fact = UNKNOWN_FACT
    redistribution: Fact = UNKNOWN_FACT
    derivative_lineage: Fact = UNKNOWN_FACT
    weights_kind: WeightsKind = WeightsKind.UNKNOWN
    weights_kind_basis: Basis = Basis.UNKNOWN
    distributor: Fact = UNKNOWN_FACT  # where the runtime artifact comes from, e.g. registry.ollama.ai/library
    gguf_provenance: Fact = UNKNOWN_FACT
    quantization_format: Fact = UNKNOWN_FACT
    runtime_tag: Fact = UNKNOWN_FACT
    model_layer_digest: Fact = UNKNOWN_FACT
    model_layer_size_bytes: Fact = UNKNOWN_FACT
    license_layer_digest: Fact = UNKNOWN_FACT
    manifest_digest: Fact = UNKNOWN_FACT
    kv_architecture: ArchitectureFacts | None = None
    roles_claimed: tuple[CapabilityRole, ...] = ()
    behavior_labels: tuple[BehaviorLabel, ...] = ()
    facts: dict[str, Fact] = Field(default_factory=dict)  # every other audited field, basis-tagged
    state: ModelLifecycleState = ModelLifecycleState.AUDITED
    retrieved_at: datetime

    @field_validator("candidate_id")
    @classmethod
    def candidate_id_pattern(cls, value: str) -> str:
        if not _CANDIDATE_ID.fullmatch(value):
            raise ValueError("CANDIDATE_ID_INVALID")
        return value


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    status: GateStatus
    reasons: tuple[str, ...]
    figures: dict[str, int] = Field(default_factory=dict)


def _upstream(item: Fact) -> bool:
    return item.basis is Basis.UPSTREAM_FACT and item.value not in (None, "", [])


def license_audit(candidate: ModelCandidate, *, accepted: frozenset[str] = ACCEPTED_LICENSES) -> GateResult:
    reasons: list[str] = []
    spdx = candidate.license_spdx
    if not _upstream(spdx):
        return GateResult(gate="MODEL_LICENSE_AUDIT", status=GateStatus.FAIL, reasons=("LICENSE_NOT_UPSTREAM_FACT",))
    if spdx.value not in accepted:
        return GateResult(gate="MODEL_LICENSE_AUDIT", status=GateStatus.REVIEW_REQUIRED,
                          reasons=(f"LICENSE_NOT_IN_ACCEPTED_SET:{spdx.value}",))
    reasons.append(f"LICENSE_ACCEPTED:{spdx.value}")
    for name, item in (("LICENSE_URL", candidate.license_url), ("LICENSE_LAYER_DIGEST", candidate.license_layer_digest)):
        reasons.append(f"{name}_{'PRESENT' if _upstream(item) else 'MISSING'}")
    if candidate.commercial_use.value is False:
        reasons.append("COMMERCIAL_USE_RESTRICTED")
    if _upstream(candidate.license_layer_digest) and not _SHA256.fullmatch(str(candidate.license_layer_digest.value)):
        reasons.append("LICENSE_LAYER_DIGEST_INVALID")
    failed = any(item.endswith("_MISSING") or item in {"COMMERCIAL_USE_RESTRICTED", "LICENSE_LAYER_DIGEST_INVALID"}
                 for item in reasons)
    return GateResult(gate="MODEL_LICENSE_AUDIT", status=GateStatus.FAIL if failed else GateStatus.PASS,
                      reasons=tuple(reasons))


def provenance_audit(candidate: ModelCandidate, *,
                     trusted: frozenset[str] = TRUSTED_DISTRIBUTORS) -> GateResult:
    reasons: list[str] = []
    for name, item in (("PUBLISHER", candidate.publisher), ("UPSTREAM_REPOSITORY", candidate.upstream_repository),
                       ("GGUF_PROVENANCE", candidate.gguf_provenance)):
        reasons.append(f"{name}_{'UPSTREAM_FACT' if _upstream(item) else 'UNVERIFIED'}")
    if candidate.weights_kind not in {WeightsKind.ORIGINAL, WeightsKind.QUANTIZED_FROM_ORIGINAL} or \
            candidate.weights_kind_basis is not Basis.UPSTREAM_FACT:
        reasons.append(f"WEIGHTS_KIND_NOT_ACCEPTED:{candidate.weights_kind.value}:{candidate.weights_kind_basis.value}")
    distributor = candidate.distributor
    reasons.append("DISTRIBUTOR_TRUSTED" if _upstream(distributor) and distributor.value in trusted
                   else f"DISTRIBUTOR_NOT_TRUSTED:{distributor.value}")
    for name, item in (("MODEL_LAYER_DIGEST", candidate.model_layer_digest), ("MANIFEST_DIGEST", candidate.manifest_digest)):
        valid = _upstream(item) and _SHA256.fullmatch(str(item.value)) is not None
        reasons.append(f"{name}_{'PINNED' if valid else 'MISSING'}")
    failed = any(item.endswith(("_UNVERIFIED", "_MISSING")) or item.startswith(("WEIGHTS_KIND_NOT", "DISTRIBUTOR_NOT"))
                 for item in reasons)
    return GateResult(gate="MODEL_PROVENANCE_AUDIT", status=GateStatus.FAIL if failed else GateStatus.PASS,
                      reasons=tuple(reasons))


def kv_cache_mib(architecture: ArchitectureFacts, context_tokens: int, *, bytes_per_value: int = 2) -> int:
    """K and V for every layer at ``context_tokens`` (f16 by default), rounded up to MiB."""
    total = 2 * architecture.num_layers * architecture.num_kv_heads * architecture.head_dim * context_tokens * bytes_per_value
    return -(-total // 2**20)


def size_fit_audit(candidate: ModelCandidate, device: DeviceProfile, *, context_tokens: int = 8192,
                   reserve_mib: int = RUNTIME_RESERVE_MIB, kv_bytes_per_value: int = 2) -> GateResult:
    """GPU-resident fit against the MEASURED device, through the existing ResourceInventory."""
    size = candidate.model_layer_size_bytes
    if not _upstream(size) or not isinstance(size.value, int) or candidate.kv_architecture is None:
        return GateResult(gate="MODEL_SIZE_FITS_DEVICE", status=GateStatus.FAIL,
                          reasons=("SIZE_OR_ARCHITECTURE_NOT_UPSTREAM_FACT",))
    if device.gpu.status is not GpuStatus.GPU_PRESENT or not device.gpu.devices:
        return GateResult(gate="MODEL_SIZE_FITS_DEVICE", status=GateStatus.FAIL, reasons=(f"DEVICE_{device.gpu.status.value}",))
    weights = -(-size.value // 2**20)
    kv = kv_cache_mib(candidate.kv_architecture, context_tokens, bytes_per_value=kv_bytes_per_value)
    required = weights + kv + reserve_mib
    gpu = max(device.gpu.devices, key=lambda item: item.vram_total_mb)
    free = gpu.vram_free_mb if gpu.vram_free_mb is not None else 0
    hardware = to_hardware_profile(device, resource_id="audit-device")
    spdx = candidate.license_spdx.value if isinstance(candidate.license_spdx.value, str) else "NOASSERTION"
    profile = ModelRuntimeProfile(
        model_id=f"ollama/audit-candidate@{candidate.candidate_id.replace(':', '-')}", backend="ollama",
        family="audit-candidate", roles=(ModelRole.TEXT,), modalities=(Modality.TEXT,),
        context_length=context_tokens, runtimes=(AcceleratorRuntime.CUDA,), vram_mb=required,
        license=LicenseInfo(spdx=spdx if spdx in ACCEPTED_LICENSES else "NOASSERTION",
                            distribution=Distribution.RUNTIME_OPTIONAL))
    feasibility = StaticResourceInventory([hardware], as_of=device.measured_at).feasibility(profile, "audit-device")
    reasons = [*feasibility.options[0].reasons, f"MEASURED_FREE_VRAM_{'OK' if required <= free else 'INSUFFICIENT'}"]
    fits = feasibility.fits and required <= free
    return GateResult(gate="MODEL_SIZE_FITS_DEVICE", status=GateStatus.PASS if fits else GateStatus.FAIL,
                      reasons=tuple(reasons), figures={"weights_mib": weights, "kv_cache_mib": kv, "reserve_mib": reserve_mib,
                                                       "required_mib": required, "vram_total_mib": gpu.vram_total_mb,
                                                       "vram_free_mib": free, "context_tokens": context_tokens,
                                                       "kv_bytes_per_value": kv_bytes_per_value})


def pre_download_gates(candidate: ModelCandidate, device: DeviceProfile, *, context_tokens: int = 8192) -> tuple[GateResult, ...]:
    return (license_audit(candidate), provenance_audit(candidate),
            size_fit_audit(candidate, device, context_tokens=context_tokens))


def may_become_available(results: tuple[GateResult, ...]) -> bool:
    """AUDITED -> AVAILABLE only when all three gates PASS (still not authorized, registered or routable)."""
    return len(results) == 3 and all(result.status is GateStatus.PASS for result in results)
