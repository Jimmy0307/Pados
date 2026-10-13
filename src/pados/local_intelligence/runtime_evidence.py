# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise Local Runtime Adapter: evidence an external runtime plane submits, as generic live observations.

An enterprise may already operate its own local-AI runtime plane (runtimes, device pools, leases, benchmark
runs). This module lets such a plane submit a ``RuntimeEvidenceDocument``, which is validated and projected
into the EXISTING frozen observation contracts (``RuntimeObservation`` / ``DeviceObservation``), so
``bind_live`` and the measured execution contract apply unchanged. Nothing here is specific to any vendor,
runtime brand, model or company.

The document is evidence, never authority:

* it cannot register, authorize or route a model: registration stays explicit and operator-written, an empty
  ``allowed_models`` stays DENY and selection stays with the Worker Router;
* a lease is availability evidence only. An expired lease is a reason; a valid lease authorizes nothing;
* artifacts are identified by manifest digest only. A runtime's own name for an artifact is opaque;
* benchmark evidence references are SHA-256 digests to be re-verified by PADOS, never profiles by themselves.

Fail closed, never raise: ``ingest_runtime_evidence`` returns a ``RuntimeEvidenceResult``. An oversized,
malformed, credential-bearing or unauthenticated document yields no observations (so binding is UNAVAILABLE)
and closed reason codes that never echo submitted values. Only documents carrying a valid HMAC-SHA256
signature over their exact bytes, verified with the operator's ``EvidenceKey`` (resolved through
``pados.enterprise.secrets.SecretBoundary`` from a ``secretref://`` reference; its only constructor), are
accepted. Without a key a document is refused (``EVIDENCE_UNAUTHENTICATED``) unless the caller explicitly passes
``allow_unauthenticated=True`` (DEVELOPMENT ONLY). No network I/O happens here; transport belongs to the caller.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pados.enterprise.secrets import SecretBoundary, looks_like_secret
from pados.resources.capability_profile import (
    BindingPolicy,
    CandidateState,
    DeviceObservation,
    Freshness,
    LiveRuntimeBinding,
    ModelCapabilityProfile,
    RuntimeArtifact,
    RuntimeObservation,
    bind_live,
    freshness_of,
)

RUNTIME_EVIDENCE_SCHEMA = "pados.runtime-evidence.v1"
SIGNATURE_DOMAIN = b"pados.runtime-evidence.signature.v1\x00"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_RUNTIMES = 32
MAX_ARTIFACTS = 256
MAX_LEASES = 64
MAX_BENCHMARK_REFS = 64
MAX_STRING = 200
MIN_EVIDENCE_KEY_BYTES = 32

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_USERINFO = re.compile(r"[A-Za-z0-9._%+-]+:[^@\s/]+@")  # user:secret@host without a scheme


class EvidenceIntegrity(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"  # a configured key verified the signature
    UNAUTHENTICATED = "UNAUTHENTICATED"  # no key, explicit development-only opt-in: unauthenticated evidence
    REFUSED = "REFUSED"


class EndpointScope(StrEnum):
    """Where the external plane serves from, as a scope label only (never a host, address or URL)."""

    LOOPBACK = "LOOPBACK"
    PRIVATE_NETWORK = "PRIVATE_NETWORK"
    UNKNOWN = "UNKNOWN"


_STATES = {state.value: state for state in CandidateState}


_FROM_SECRET_REF = object()  # private factory marker (key material only ever comes through the SecretBoundary)


class EvidenceKey:
    """Operator-configured HMAC key for evidence documents. Memory only: redacted repr, never pickled.

    Constructible only through ``from_secret_ref`` (PC-D03; red-team F-5): direct construction raises."""

    __slots__ = ("_material",)

    def __init__(self, material: bytes, *, _token: object = None) -> None:
        if _token is not _FROM_SECRET_REF:
            raise TypeError("EVIDENCE_KEY_REQUIRES_SECRET_REF")
        if not isinstance(material, bytes) or len(material) < MIN_EVIDENCE_KEY_BYTES:
            raise ValueError("EVIDENCE_KEY_TOO_SHORT")
        self._material = hmac.new(b"pados.runtime-evidence.key.v1", material, hashlib.sha256).digest()

    @classmethod
    def from_secret_ref(cls, boundary: SecretBoundary, ref: str) -> EvidenceKey:
        """Resolve the operator's ``secretref://`` key through the SecretBoundary (that one reference only)."""
        with boundary.resolved({ref}, authorized_refs={ref}) as values:
            return cls(values[ref].reveal().encode("utf-8"), _token=_FROM_SECRET_REF)

    def sign(self, document: bytes) -> str:
        return hmac.new(self._material, SIGNATURE_DOMAIN + document, hashlib.sha256).hexdigest()

    def verify(self, document: bytes, signature: str) -> bool:
        return bool(_SIGNATURE.fullmatch(signature)) and hmac.compare_digest(self.sign(document), signature)

    def __repr__(self) -> str:
        return "EvidenceKey([REDACTED])"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("EVIDENCE_KEY_NOT_SERIALIZABLE")


def _label(value: str) -> str:
    if not _LABEL.fullmatch(value):
        raise ValueError("EVIDENCE_LABEL_INVALID")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("EVIDENCE_TIMESTAMP_NOT_AWARE")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceArtifact(_Strict):
    runtime_ref: str = Field(max_length=160)
    manifest_digest: str | None  # sha256:<hex>; None = unknown (never matches a profile)


class EvidenceRuntime(_Strict):
    runtime_id: str
    runtime_family: str  # opaque family label; compared for equality with the measured binding only
    version: str | None = Field(default=None, max_length=80)
    state: str = Field(max_length=32)  # AVAILABLE / DEGRADED / UNAVAILABLE; anything else is UNAVAILABLE
    artifacts: tuple[EvidenceArtifact, ...] = Field(default=(), max_length=MAX_ARTIFACTS)
    resident_refs: tuple[str, ...] = Field(default=(), max_length=MAX_ARTIFACTS)

    @field_validator("runtime_id", "runtime_family")
    @classmethod
    def labels(cls, value: str) -> str:
        return _label(value)


class EvidenceDevice(_Strict):
    device_profile_id: str | None
    free_gpu_mib: int | None = Field(default=None, ge=0, le=1_048_576)
    available_ram_mib: int | None = Field(default=None, ge=0, le=4_194_304)
    state: str = Field(max_length=32)


class EvidenceLease(_Strict):
    """A lease the external plane granted for a runtime slot: availability evidence, never authorization."""

    lease_id: str
    runtime_ref: str = Field(max_length=160)
    expires_at: datetime

    @field_validator("lease_id")
    @classmethod
    def label(cls, value: str) -> str:
        return _label(value)

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @property
    def authority(self) -> str:
        return "NONE"


class RuntimeEvidenceDocument(_Strict):
    schema_version: str
    plane_id: str  # an opaque label the operator assigned to the external plane
    endpoint_scope: EndpointScope = EndpointScope.UNKNOWN
    observed_at: datetime
    runtimes: tuple[EvidenceRuntime, ...] = Field(default=(), max_length=MAX_RUNTIMES)
    device: EvidenceDevice | None = None
    leases: tuple[EvidenceLease, ...] = Field(default=(), max_length=MAX_LEASES)
    benchmark_evidence_refs: tuple[str, ...] = Field(default=(), max_length=MAX_BENCHMARK_REFS)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != RUNTIME_EVIDENCE_SCHEMA:
            raise ValueError("EVIDENCE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("plane_id")
    @classmethod
    def plane_label(cls, value: str) -> str:
        return _label(value)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("benchmark_evidence_refs")
    @classmethod
    def digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SHA256.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("BENCHMARK_EVIDENCE_REF_INVALID")
        return value


class LeaseEvidence(BaseModel):
    """A lease as PADOS records it: evidence with its own reasons, never an authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: str
    runtime_ref: str
    expires_at: datetime
    reasons: tuple[str, ...]
    authority: str = "NONE"


class RuntimeEvidenceResult(BaseModel):
    """What PADOS derived from one submitted document. Observations are empty when the document was refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    integrity: EvidenceIntegrity
    reasons: tuple[str, ...]
    document_digest: str | None
    plane_id: str | None = None
    endpoint_scope: EndpointScope = EndpointScope.UNKNOWN
    runtimes: tuple[RuntimeObservation, ...] = ()
    device: DeviceObservation | None = None
    leases: tuple[LeaseEvidence, ...] = ()
    benchmark_evidence_refs: tuple[str, ...] = ()
    authority: str = "NONE"

    @property
    def accepted(self) -> bool:
        return self.integrity is not EvidenceIntegrity.REFUSED


def _refused(code: str, digest: str | None = None) -> RuntimeEvidenceResult:
    return RuntimeEvidenceResult(integrity=EvidenceIntegrity.REFUSED, reasons=(code,), document_digest=digest)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def credential_finding(value: str) -> str | None:
    """Closed code when a submitted string carries credential material or a URL with credentials/query."""
    if len(value) > MAX_STRING:
        return "EVIDENCE_STRING_TOO_LONG"
    if looks_like_secret(value) or "secretref://" in value.lower():
        return "EVIDENCE_CREDENTIAL_MATERIAL"
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return "EVIDENCE_URL_INVALID"
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            return "EVIDENCE_URL_HAS_CREDENTIALS"
        if parts.query or parts.fragment or "?" in value:
            return "EVIDENCE_URL_HAS_QUERY"
    elif _USERINFO.search(value):
        return "EVIDENCE_URL_HAS_CREDENTIALS"
    return None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("EVIDENCE_DUPLICATE_KEY")
        result[key] = value
    return result


def _validation_code(error: ValidationError) -> str:
    for item in error.errors():
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        if _CODE.fullmatch(message):
            return message
    return "EVIDENCE_DOCUMENT_INVALID"


def _state(value: str, reasons: list[str], code: str) -> CandidateState:
    state = _STATES.get(value)
    if state is None:
        reasons.append(code)
        return CandidateState.UNAVAILABLE
    return state


def _runtime(item: EvidenceRuntime, observed_at: datetime) -> RuntimeObservation:
    reasons: list[str] = []
    state = _state(item.state, reasons, "EXTERNAL_RUNTIME_STATE_UNKNOWN")
    if state is not CandidateState.AVAILABLE and not reasons:
        reasons.append(f"EXTERNAL_RUNTIME_{state.value}")
    return RuntimeObservation(
        runtime_id=item.runtime_id, runtime_family=item.runtime_family, runtime_version=item.version, state=state,
        reasons=tuple(reasons),
        artifacts=tuple(RuntimeArtifact(runtime_ref=a.runtime_ref, manifest_digest=a.manifest_digest)
                        for a in item.artifacts),
        resident_refs=tuple(dict.fromkeys(item.resident_refs)), observed_at=observed_at)


def _device(item: EvidenceDevice, observed_at: datetime) -> DeviceObservation:
    reasons: list[str] = []
    state = _state(item.state, reasons, "EXTERNAL_DEVICE_STATE_UNKNOWN")
    if item.free_gpu_mib is None or item.available_ram_mib is None:
        reasons.append("EXTERNAL_DEVICE_HEADROOM_UNKNOWN")
    return DeviceObservation(device_profile_id=item.device_profile_id, state=state, reasons=tuple(reasons),
                             free_gpu_memory_mib=item.free_gpu_mib, available_ram_mib=item.available_ram_mib,
                             observed_at=observed_at)


def _leases(document: RuntimeEvidenceDocument, now: datetime) -> tuple[LeaseEvidence, ...]:
    held = {a.runtime_ref for r in document.runtimes for a in r.artifacts}
    leases = []
    for lease in document.leases:
        reasons = ["EXTERNAL_LEASE_IS_EVIDENCE_ONLY"]
        if lease.expires_at <= now:
            reasons.append("EXTERNAL_LEASE_EXPIRED")
        if lease.runtime_ref not in held:
            reasons.append("EXTERNAL_LEASE_RUNTIME_REF_UNKNOWN")
        leases.append(LeaseEvidence(lease_id=lease.lease_id, runtime_ref=lease.runtime_ref,
                                    expires_at=lease.expires_at, reasons=tuple(reasons)))
    return tuple(leases)


def _authenticate(raw: bytes, key: EvidenceKey | None, signature: str | None,
                  allow_unauthenticated: bool) -> EvidenceIntegrity | str:
    if key is None:
        # red-team F-3: fail closed; unauthenticated evidence needs the explicit development-only opt-in
        return EvidenceIntegrity.UNAUTHENTICATED if allow_unauthenticated is True else "EVIDENCE_UNAUTHENTICATED"
    if signature is None:
        return "EVIDENCE_UNAUTHENTICATED"
    return EvidenceIntegrity.AUTHENTICATED if key.verify(raw, signature) else "EVIDENCE_SIGNATURE_INVALID"


def _ingest(raw: bytes, now: datetime, policy: BindingPolicy, key: EvidenceKey | None,
            signature: str | None, allow_unauthenticated: bool) -> RuntimeEvidenceResult:
    if not isinstance(raw, bytes):
        return _refused("EVIDENCE_DOCUMENT_NOT_BYTES")
    if len(raw) > MAX_DOCUMENT_BYTES:
        return _refused("EVIDENCE_DOCUMENT_TOO_LARGE")
    digest = hashlib.sha256(raw).hexdigest()
    integrity = _authenticate(raw, key, signature, allow_unauthenticated)
    if not isinstance(integrity, EvidenceIntegrity):
        return _refused(integrity, digest)
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _refused("EVIDENCE_DOCUMENT_MALFORMED", digest)
    if not isinstance(parsed, dict):
        return _refused("EVIDENCE_DOCUMENT_MALFORMED", digest)
    for text in _strings(parsed):
        finding = credential_finding(text)
        if finding is not None:
            return _refused(finding, digest)
    try:
        document = RuntimeEvidenceDocument.model_validate(parsed)
        runtimes = tuple(_runtime(item, document.observed_at) for item in document.runtimes)
        device = _device(document.device, document.observed_at) if document.device else None
    except ValidationError as exc:
        return _refused(_validation_code(exc), digest)
    reasons: list[str] = [] if integrity is EvidenceIntegrity.AUTHENTICATED else ["EVIDENCE_UNAUTHENTICATED"]
    freshness = freshness_of(document.observed_at, now, policy.max_age_seconds)
    if freshness is not Freshness.FRESH:
        reasons.append(f"EVIDENCE_{freshness.value}")  # bind_live also refuses the stale observations
    if len({r.runtime_id for r in runtimes}) != len(runtimes):
        return _refused("EVIDENCE_RUNTIME_ID_DUPLICATE", digest)
    return RuntimeEvidenceResult(
        integrity=integrity, reasons=tuple(reasons), document_digest=digest, plane_id=document.plane_id,
        endpoint_scope=document.endpoint_scope, runtimes=runtimes, device=device,
        leases=_leases(document, now), benchmark_evidence_refs=document.benchmark_evidence_refs)


def ingest_runtime_evidence(raw: bytes, *, now: datetime, policy: BindingPolicy | None = None,
                            key: EvidenceKey | None = None, signature: str | None = None,
                            allow_unauthenticated: bool = False) -> RuntimeEvidenceResult:
    """Validate one submitted document. Never raises; a refusal carries closed reason codes only.

    Without ``key`` the document is refused (``EVIDENCE_UNAUTHENTICATED``, binding UNAVAILABLE).
    ``allow_unauthenticated=True`` is a DEVELOPMENT-ONLY opt-in that accepts it, marked ``EVIDENCE_UNAUTHENTICATED``;
    a document with a forged device id and digest is then indistinguishable from a real one."""
    try:
        return _ingest(raw, now, policy or BindingPolicy(), key, signature, allow_unauthenticated)
    except Exception:  # noqa: BLE001 - an adapter defect must never reach the planner; fail closed
        return _refused("EVIDENCE_ADAPTER_FAILED")


def bind_external(profile: ModelCapabilityProfile, evidence: RuntimeEvidenceResult, *, now: datetime,
                  policy: BindingPolicy) -> LiveRuntimeBinding:
    """``bind_live`` over the evidence's observations, unchanged, plus the document's and lease reasons.

    A refused document contributes no observations, so the binding is UNAVAILABLE. Leases only add reasons:
    they never turn a state AVAILABLE, never register and never authorize."""
    binding = bind_live(profile, evidence.runtimes, evidence.device, now=now, policy=policy)
    lease_reasons = [reason for lease in evidence.leases if lease.runtime_ref == binding.runtime_ref
                     for reason in lease.reasons if reason != "EXTERNAL_LEASE_IS_EVIDENCE_ONLY"]
    extra = [*evidence.reasons, *lease_reasons]
    if not extra:
        return binding
    return binding.model_copy(update={"reasons": tuple(dict.fromkeys((*binding.reasons, *extra)))})
