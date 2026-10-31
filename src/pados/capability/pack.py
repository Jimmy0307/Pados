# SPDX-License-Identifier: AGPL-3.0-only
"""Capability Pack V1: a versioned extension contract over the existing Capability Fabric -- never an authority.

A pack is ONE immutable, content-addressed manifest (``pados.capability.pack.v1``) that declares existing
``CapabilityDescriptor``/``AdapterDescriptor``s together with the metadata the fabric descriptors do not carry:
identity, provider/runtime, provenance, runtime requirements, security posture, required execution evidence,
deterministic verification hooks, lifecycle, a closed failure vocabulary, and removal/rollback compatibility.

    installed != registered != authorized != selected != accepted   (unchanged by packs)

* A manifest carries no grant. Using a pack capability still needs the project-bound ``CAPABILITY:<id>`` grant
  through the existing authorization; ``required_grants`` only STATES it.
* ``register_pack`` is an operator action through the EXISTING ``CapabilityRegistry`` (install, register).
  Registration makes adapters plannable; selection stays with the planner / Worker Router.
* Execution happens only through the existing ``CapabilityExecutor``; ``GovernedPackAdapter`` is the adapter
  the registry receives. It enforces the manifest at the adapter boundary (required evidence present and
  consistent with the declared runtime; failures normalized to the declared closed vocabulary) and never widens
  anything. Its output stays ``StageOutput(semantic_authority="NONE")``: verification is evidence, acceptance
  remains the existing ResultCandidate -> governed acceptance -> CAS path.
* Lifecycle reuses the capability-loop vocabulary (``CapabilityState``) and its inert ``propose_transition``: a
  manifest can never declare itself VALIDATED and its descriptors must stay EXPERIMENTAL until an operator
  promotes them after a recorded validation outcome. DISABLED is the registry's REVOKED.
* ``PackIndex`` attributes existing ``StageReceipt`` descriptor digests back to every ``pack@version`` that
  declares them, so receipts stay attributable across rollback.

This module imports no scheduler, router, CAS, approval, state or provider code, and never touches the
executor's stage-binding path (``tests/capability/test_pack_boundaries.py``).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pados.capability.adapter import CapabilityAdapter, CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactRef
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    LicenseInfo,
    LifecycleState,
    ResourceRequirement,
    descriptor_digest,
)
from pados.capability.registry import RegistryError
from pados.capability_loop.contracts import CapabilityState
from pados.capability_loop.lifecycle import PromotionProposal, propose_transition
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import is_secret_ref
from pados.execution.context import canonical_hash

PACK_SCHEMA = "pados.capability.pack.v1"
# Evidence every successful pack output must carry (Measured Capability: MODEL x RUNTIME x DEVICE x CONFIG x
# EVIDENCE). Pack-specific keys may be added; these may not be dropped.
CORE_EVIDENCE = ("runtime_version", "device", "config_digest", "provider_id", "artifact_digest")
PACK_ADAPTER_FAILED = "PACK_ADAPTER_FAILED"
MAX_CAPABILITIES = 32
MAX_ADAPTERS = 64
MAX_FAILURE_CODES = 64
MAX_EVIDENCE_KEYS = 24
MAX_TIMEOUT_SECONDS = 86_400

_PACK_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,79}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT = re.compile(r"^[a-z][a-z0-9_.-]{2,79}/v[0-9]{1,3}$")
_CHECKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


class PackManifestError(ValueError):
    """Closed-code refusal of a pack manifest or registration (fail closed)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PackSource(StrEnum):
    IN_TREE = "IN_TREE"  # reviewed as part of this repository
    OPERATOR_SUPPLIED = "OPERATOR_SUPPLIED"  # supplied by the operator; needs a reviewed source digest
    THIRD_PARTY = "THIRD_PARTY"  # external publisher; needs a reviewed source digest


class RuntimeType(StrEnum):
    IN_PROCESS = "IN_PROCESS"
    LOCAL_SUBPROCESS = "LOCAL_SUBPROCESS"
    LOCAL_HTTP = "LOCAL_HTTP"
    MCP = "MCP"
    CONTAINER = "CONTAINER"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"


class Egress(StrEnum):
    NONE = "NONE"
    LOOPBACK_ONLY = "LOOPBACK_ONLY"
    ALLOWLIST = "ALLOWLIST"


class ProjectBinding(StrEnum):
    PROJECT_SCOPED = "PROJECT_SCOPED"  # V1: every use is bound to one project's grant; nothing is global


class RemovalAction(StrEnum):
    DISABLE = "DISABLE"  # registry lifecycle -> REVOKED; receipts kept
    ROLLBACK = "ROLLBACK"  # register the previous version; PackIndex keeps every version attributable
    ARTIFACT_CLEANUP = "ARTIFACT_CLEANUP"  # pack-owned scratch only, never content-addressed evidence


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _match(pattern: re.Pattern[str], value: object, code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(code)
    return value


class PackProvider(_Frozen):
    """Who supplies the runtime the pack's adapters drive (identity only; never authority)."""

    provider_id: str
    runtime_id: str
    runtime_version: str

    @field_validator("provider_id", "runtime_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        return _match(_ID, value, "PACK_PROVIDER_INVALID")

    @field_validator("runtime_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _match(_VERSION, value, "PACK_PROVIDER_INVALID")


class PackProvenance(_Frozen):
    source: PackSource
    license: LicenseInfo
    source_digest: str | None = None  # SHA-256 of the reviewed source/artifact
    publisher: str

    @field_validator("source_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        return None if value is None else _match(_SHA256, value, "PACK_PROVENANCE_INVALID")

    @field_validator("publisher")
    @classmethod
    def _publisher(cls, value: str) -> str:
        return _match(_ID, value, "PACK_PROVENANCE_INVALID")


class PackRuntime(_Frozen):
    runtime_type: RuntimeType
    device: tuple[ResourceRequirement, ...] = Field(default=(), max_length=4)
    execution_config_digest: str  # the configuration the capability is measured/validated under
    timeout_seconds: int
    network: NetworkRequirement

    @field_validator("execution_config_digest")
    @classmethod
    def _config(cls, value: str) -> str:
        return _match(_SHA256, value, "PACK_RUNTIME_INVALID")

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= MAX_TIMEOUT_SECONDS:
            raise ValueError("PACK_RUNTIME_INVALID")
        return value


class PackSecurity(_Frozen):
    secret_refs: tuple[str, ...] = Field(default=(), max_length=16)  # ``secretref://`` names, never values
    allowed_roots: tuple[str, ...] = Field(default=(), max_length=16)  # logical root names, never paths
    project_binding: ProjectBinding = ProjectBinding.PROJECT_SCOPED
    classification_ceiling: DataClassification
    egress: Egress
    egress_hosts: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("secret_refs")
    @classmethod
    def _refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not is_secret_ref(ref) for ref in value):
            raise ValueError("PACK_SECRET_REF_INVALID")
        return value

    @field_validator("allowed_roots")
    @classmethod
    def _roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_match(_ID, root, "PACK_SECURITY_INVALID") for root in value)

    @field_validator("egress_hosts")
    @classmethod
    def _hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_match(_HOST, host, "PACK_EGRESS_INCONSISTENT") for host in value)


class FailureCodeSpec(_Frozen):
    code: str
    retryable: bool = False

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        _match(_CODE, value, "PACK_FAILURE_CODE_INVALID")
        if value.startswith("PACK_"):
            raise ValueError("PACK_FAILURE_CODE_INVALID")  # the PACK_ namespace belongs to the contract
        return value


class CheckerSpec(_Frozen):
    """A deterministic output validator the pack supplies; the operator registers it (ValidatorRegistry)."""

    output_contract: str
    checker_id: str

    @field_validator("output_contract")
    @classmethod
    def _contract(cls, value: str) -> str:
        return _match(_CONTRACT, value, "PACK_VERIFICATION_HOOK_UNKNOWN")

    @field_validator("checker_id")
    @classmethod
    def _checker(cls, value: str) -> str:
        return _match(_CHECKER, value, "PACK_VERIFICATION_HOOK_INVALID")


class PackRemoval(_Frozen):
    actions: tuple[RemovalAction, ...] = (RemovalAction.DISABLE, RemovalAction.ROLLBACK)
    compatible_output_contracts: tuple[str, ...] = Field(default=(), max_length=MAX_CAPABILITIES)


class CapabilityPackManifest(_Frozen):
    schema_version: str = PACK_SCHEMA
    pack_id: str
    version: str
    provider: PackProvider
    provenance: PackProvenance
    capability_ids: tuple[str, ...]
    capabilities: tuple[CapabilityDescriptor, ...] = Field(max_length=MAX_CAPABILITIES)
    adapters: tuple[AdapterDescriptor, ...] = Field(max_length=MAX_ADAPTERS)
    runtime: PackRuntime
    security: PackSecurity
    evidence_requirements: tuple[str, ...]
    verification_hooks: tuple[CheckerSpec, ...] = Field(default=(), max_length=MAX_CAPABILITIES)
    initial_state: CapabilityState
    failure_codes: tuple[FailureCodeSpec, ...] = Field(default=(), max_length=MAX_FAILURE_CODES)
    removal: PackRemoval = PackRemoval()
    semantic_authority: Literal["NONE"] = "NONE"

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != PACK_SCHEMA:
            raise ValueError("PACK_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("pack_id")
    @classmethod
    def _pack_id(cls, value: str) -> str:
        return _match(_PACK_ID, value, "PACK_ID_INVALID")

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _match(_VERSION, value, "PACK_VERSION_INVALID")

    @model_validator(mode="after")
    def _coherent(self) -> CapabilityPackManifest:
        _check_identity(self)
        _check_lifecycle(self)
        _check_security(self)
        _check_evidence_and_failures(self)
        _check_verification_and_removal(self)
        return self

    @property
    def required_grants(self) -> tuple[str, ...]:
        """The grants USE requires (stated, never granted by the pack)."""
        return tuple(f"CAPABILITY:{capability_id}" for capability_id in self.capability_ids)

    @property
    def manifest_digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def adapter(self, adapter_id: str) -> AdapterDescriptor | None:
        return next((item for item in self.adapters if item.adapter_id == adapter_id), None)

    def failure(self, code: str) -> FailureCodeSpec | None:
        return next((item for item in self.failure_codes if item.code == code), None)


def _check_identity(manifest: CapabilityPackManifest) -> None:
    declared = [item.capability_id for item in manifest.capabilities]
    if not declared or not manifest.adapters:
        raise ValueError("PACK_EMPTY")
    adapter_ids = [item.adapter_id for item in manifest.adapters]
    if len(set(declared)) != len(declared) or len(set(adapter_ids)) != len(adapter_ids):
        raise ValueError("PACK_DUPLICATE_ID")
    if tuple(manifest.capability_ids) != tuple(declared):
        raise ValueError("PACK_CAPABILITY_IDS_MISMATCH")
    if any(item.capability_id not in declared for item in manifest.adapters):
        raise ValueError("PACK_ADAPTER_CAPABILITY_UNDECLARED")
    if any(capability not in {item.capability_id for item in manifest.adapters} for capability in declared):
        raise ValueError("PACK_CAPABILITY_WITHOUT_ADAPTER")
    if manifest.provenance.source is not PackSource.IN_TREE and manifest.provenance.source_digest is None:
        raise ValueError("PACK_PROVENANCE_DIGEST_REQUIRED")


def _descriptors(manifest: CapabilityPackManifest) -> tuple[CapabilityDescriptor | AdapterDescriptor, ...]:
    return (*manifest.capabilities, *manifest.adapters)


def _check_lifecycle(manifest: CapabilityPackManifest) -> None:
    if manifest.initial_state not in (CapabilityState.DISCOVERED, CapabilityState.EXPERIMENTAL):
        raise ValueError("PACK_INITIAL_STATE_INVALID")  # a pack can never declare itself validated
    for item in _descriptors(manifest):
        if item.lifecycle is LifecycleState.ACTIVE:
            raise ValueError("PACK_LIFECYCLE_CLAIMS_ACTIVE")  # promotion is an operator action, never declared
        if item.lifecycle is not LifecycleState.EXPERIMENTAL:
            raise ValueError("PACK_LIFECYCLE_INVALID")


def _check_security(manifest: CapabilityPackManifest) -> None:
    security, network = manifest.security, manifest.runtime.network
    ceiling = min(item.max_classification.rank for item in _descriptors(manifest))
    if security.classification_ceiling.rank > ceiling:
        raise ValueError("PACK_CLASSIFICATION_CEILING_EXCEEDS_DESCRIPTOR")
    if any(item.network_requirement is not network for item in _descriptors(manifest)):
        raise ValueError("PACK_NETWORK_INCONSISTENT")
    local = network is NetworkRequirement.LOCAL_ONLY
    if local != (security.egress in (Egress.NONE, Egress.LOOPBACK_ONLY)):
        raise ValueError("PACK_EGRESS_INCONSISTENT")
    if (security.egress is Egress.ALLOWLIST) != bool(security.egress_hosts):
        raise ValueError("PACK_EGRESS_INCONSISTENT")


def _check_evidence_and_failures(manifest: CapabilityPackManifest) -> None:
    keys = manifest.evidence_requirements
    if len(keys) > MAX_EVIDENCE_KEYS or len(set(keys)) != len(keys) or any(not _KEY.fullmatch(k) for k in keys):
        raise ValueError("PACK_EVIDENCE_REQUIREMENTS_INVALID")
    if not set(CORE_EVIDENCE) <= set(keys):
        raise ValueError("PACK_EVIDENCE_REQUIREMENTS_INCOMPLETE")
    codes = [item.code for item in manifest.failure_codes]
    if len(set(codes)) != len(codes):
        raise ValueError("PACK_FAILURE_CODE_INVALID")


def _check_verification_and_removal(manifest: CapabilityPackManifest) -> None:
    outputs = {item.output_contract for item in manifest.capabilities}
    hooks = [hook.output_contract for hook in manifest.verification_hooks]
    if any(contract not in outputs for contract in hooks):
        raise ValueError("PACK_VERIFICATION_HOOK_UNKNOWN")
    if len(set(hooks)) != len(hooks):
        raise ValueError("PACK_VERIFICATION_HOOK_INVALID")  # one checker per output contract, no override
    if any(contract not in outputs for contract in manifest.removal.compatible_output_contracts):
        raise ValueError("PACK_REMOVAL_INVALID")


def validate_pack(data: Mapping[str, Any]) -> CapabilityPackManifest:
    """Parse and validate a manifest document; any failure is a closed ``PackManifestError`` code."""
    try:
        return CapabilityPackManifest.model_validate(dict(data))
    except ValidationError as exc:
        raise PackManifestError(_first_pack_code(exc)) from None


def _first_pack_code(exc: ValidationError) -> str:
    for error in exc.errors():
        reason = error.get("ctx", {}).get("error")
        if reason is not None and str(reason).startswith("PACK_"):
            return str(reason)
    return "PACK_MANIFEST_INVALID"


def outputs_digest(outputs: Sequence[ArtifactRef]) -> str:
    """The ``artifact_digest`` evidence value: the single output's SHA-256, or a canonical hash of all of them."""
    if len(outputs) == 1:
        return outputs[0].sha256
    return canonical_hash([ref.sha256 for ref in outputs])


class GovernedPackAdapter:
    """The adapter the registry receives for a pack implementation: the manifest enforced at the boundary.

    It is reached only through the existing executor (``run_bound_stage``). It refuses an implementation that is
    not the declared adapter, refuses outputs whose required evidence is missing or contradicts the manifest
    (runtime version, provider, configuration, output digest), and normalizes failures to the declared closed
    vocabulary (anything else is ``PACK_ADAPTER_FAILED`` with no exception chain, so content never escapes).
    """

    def __init__(self, manifest: CapabilityPackManifest, implementation: CapabilityAdapter) -> None:
        declared = manifest.adapter(implementation.descriptor.adapter_id)
        if declared is None or descriptor_digest(declared) != descriptor_digest(implementation.descriptor):
            raise PackManifestError("PACK_IMPLEMENTATION_MISMATCH")
        self._manifest = manifest
        self._inner = implementation
        self._descriptor = declared

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def manifest(self) -> CapabilityPackManifest:
        return self._manifest

    def available(self) -> bool:
        # A missing runtime or an unreachable provider is "unavailable", never an error that escapes the
        # planner: the pack becomes unplannable (fail closed) and the platform keeps working.
        try:
            return self._inner.available() is True
        except Exception:  # noqa: BLE001 - closed boundary: an availability probe cannot crash planning
            return False

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        if request.capability_id != self._descriptor.capability_id:
            raise CapabilityAdapterError("PACK_CAPABILITY_MISMATCH")
        if request.classification.rank > self._manifest.security.classification_ceiling.rank:
            raise CapabilityAdapterError("PACK_CLASSIFICATION_CEILING_EXCEEDED")
        try:
            output = self._inner.execute(request, context=context)
        except ArtifactAccessDenied:
            raise  # a governance refusal from the scoped artifact facade is never re-labelled
        except CapabilityAdapterError as exc:
            spec = self._manifest.failure(exc.code)
            code, retryable = (spec.code, spec.retryable) if spec is not None else (PACK_ADAPTER_FAILED, False)
        except Exception:  # noqa: BLE001 - closed-error boundary: nothing from the implementation escapes
            code, retryable = PACK_ADAPTER_FAILED, False
        else:
            _check_evidence(self._manifest, output)
            return output
        raise CapabilityAdapterError(code, retryable=retryable) from None  # outside the handler: empty chain


def _check_evidence(manifest: CapabilityPackManifest, output: StageOutput) -> None:
    evidence = output.evidence
    if any(evidence.get(key) is None for key in manifest.evidence_requirements):
        raise CapabilityAdapterError("PACK_EVIDENCE_MISSING")
    expected = {"runtime_version": manifest.provider.runtime_version, "provider_id": manifest.provider.provider_id,
                "config_digest": manifest.runtime.execution_config_digest,
                "artifact_digest": outputs_digest(output.outputs)}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise CapabilityAdapterError("PACK_EVIDENCE_MISMATCH")


class PackRegistry(Protocol):
    """The operator surface of the existing ``CapabilityRegistry`` a pack registration uses (nothing more)."""

    def capability(self, capability_id: str) -> CapabilityDescriptor: ...

    def is_installed(self, adapter_id: str) -> bool: ...

    def register_capability(self, descriptor: CapabilityDescriptor, *, operator: str,
                            reason_code: str = ...) -> CapabilityDescriptor: ...

    def install(self, implementation: CapabilityAdapter, *, operator: str) -> str: ...

    def register_adapter(self, adapter_id: str, *, capability_id: str, operator: str,
                         reason_code: str = ...) -> AdapterDescriptor: ...

    def set_adapter_lifecycle(self, adapter_id: str, state: LifecycleState, *, operator: str,
                              reason_code: str) -> AdapterDescriptor: ...


@dataclass(frozen=True)
class PackRegistration:
    pack_id: str
    version: str
    manifest_digest: str
    adapter_ids: tuple[str, ...]
    semantic_authority: str = "NONE"


def register_pack(registry: PackRegistry, manifest: CapabilityPackManifest,
                  implementations: Mapping[str, CapabilityAdapter], *, operator: str,
                  reason_code: str = "PACK_REGISTERED") -> PackRegistration:
    """Operator action: install and register a pack through the EXISTING registry. Grants nothing.

    Every check runs before the registry is touched: a DISCOVERED pack is a known candidate, not an executable
    one; every declared adapter needs exactly its declared implementation; an adapter id that already exists in
    this registry (registered, superseded or disabled) makes the registration stale; a capability that is already
    registered is reused only when its descriptor is identical, never silently redefined. The registry then
    applies its own rules (determinism, lifecycle, revocation) as for any adapter.
    """
    if manifest.initial_state is CapabilityState.DISCOVERED:
        raise PackManifestError("PACK_DISCOVERED_NOT_REGISTRABLE")
    declared = {item.adapter_id for item in manifest.adapters}
    if not declared <= set(implementations):
        raise PackManifestError("PACK_IMPLEMENTATION_MISSING")
    if not set(implementations) <= declared:
        raise PackManifestError("PACK_IMPLEMENTATION_UNDECLARED")
    governed = [GovernedPackAdapter(manifest, implementations[item.adapter_id]) for item in manifest.adapters]
    if any(registry.is_installed(adapter_id) for adapter_id in declared):
        raise PackManifestError("PACK_STALE_REGISTRATION")
    existing = {item.capability_id: _registered(registry, item) for item in manifest.capabilities}
    if any(found is not None and found != descriptor_digest(item)
           for item, found in zip(manifest.capabilities, existing.values(), strict=True)):
        raise PackManifestError("PACK_CAPABILITY_CONFLICT")
    for capability in manifest.capabilities:
        if existing[capability.capability_id] is None:
            registry.register_capability(capability, operator=operator, reason_code=reason_code)
    for adapter in governed:
        registry.install(adapter, operator=operator)
        registry.register_adapter(adapter.descriptor.adapter_id, capability_id=adapter.descriptor.capability_id,
                                  operator=operator, reason_code=reason_code)
    return PackRegistration(manifest.pack_id, manifest.version, manifest.manifest_digest,
                            tuple(item.adapter_id for item in manifest.adapters))


def _registered(registry: PackRegistry, capability: CapabilityDescriptor) -> str | None:
    """Digest of the already registered descriptor (compared at the pack's declared lifecycle, since operators
    move lifecycle), or ``None`` when the capability is not registered yet."""
    try:
        found = registry.capability(capability.capability_id)
    except RegistryError as exc:
        if exc.code != "CAPABILITY_NOT_REGISTERED":
            raise
        return None
    return descriptor_digest(found.model_copy(update={"lifecycle": capability.lifecycle}))


def disable_pack(registry: PackRegistry, manifest: CapabilityPackManifest, *, operator: str,
                 reason_code: str = "PACK_DISABLED") -> None:
    """Operator action: revoke every adapter of the pack (unplannable; receipts and history are kept)."""
    for adapter in manifest.adapters:
        registry.set_adapter_lifecycle(adapter.adapter_id, LifecycleState.REVOKED, operator=operator,
                                       reason_code=reason_code)


def propose_pack_transition(manifest: CapabilityPackManifest, *, current: CapabilityState, target: CapabilityState,
                            reason_code: str, validation_outcome_ref: str | None = None
                            ) -> tuple[PromotionProposal, ...]:
    """Inert per-capability proposals through the existing capability-loop transition rules (VALIDATED needs a
    recorded validation outcome). Nothing is applied: the operator registry decides."""
    return tuple(propose_transition(capability_id, current=current, target=target, reason_code=reason_code,
                                    validation_outcome_ref=validation_outcome_ref)
                 for capability_id in manifest.capability_ids)


@dataclass(frozen=True)
class PackAttribution:
    pack_id: str
    version: str
    manifest_digest: str
    capability_id: str
    adapter_id: str


class PackIndex:
    """Every registered pack version, so existing receipts stay attributable across upgrade and rollback."""

    def __init__(self) -> None:
        self._manifests: dict[tuple[str, str], CapabilityPackManifest] = {}

    def add(self, manifest: CapabilityPackManifest) -> None:
        key = (manifest.pack_id, manifest.version)
        existing = self._manifests.get(key)
        if existing is not None and existing.manifest_digest != manifest.manifest_digest:
            raise PackManifestError("PACK_VERSION_CONFLICT")  # one pack@version names exactly one manifest
        self._manifests[key] = manifest

    def versions(self, pack_id: str) -> tuple[str, ...]:
        return tuple(version for (identifier, version) in self._manifests if identifier == pack_id)

    def attributions(self, capability_digest: str, adapter_digest: str) -> tuple[PackAttribution, ...]:
        """Every pack@version declaring this (capability, adapter) descriptor pair; never a guess between them."""
        found: list[PackAttribution] = []
        for manifest in self._manifests.values():
            for adapter in manifest.adapters:
                capability = next(item for item in manifest.capabilities
                                  if item.capability_id == adapter.capability_id)
                if (descriptor_digest(capability), descriptor_digest(adapter)) == (capability_digest, adapter_digest):
                    found.append(PackAttribution(manifest.pack_id, manifest.version, manifest.manifest_digest,
                                                 capability.capability_id, adapter.adapter_id))
        return tuple(found)
