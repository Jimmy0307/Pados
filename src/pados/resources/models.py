# SPDX-License-Identifier: AGPL-3.0-only
"""Model runtime profiles: what one model needs to run (description, never authorization or selection).

``model_id`` uses the canonical compute-node identity ``<backend>/<backend model>@<version|digest>``
(``pados.compute_node.manifest.ModelManifest``). Registering a profile in a ``ModelCatalog`` only makes it
describable to the planner: it does not install, download, authorize or select a model, and PADOS never
guesses memory needs from parameter counts — every runtime path a profile declares must state its own
memory requirement, or the profile is refused.

A catalog may also hold, per registered description, ONE measured ``ModelCapabilityProfile``
(``pados.resources.capability_profile``) whose artifact is exactly that ``model_id`` and whose measured footprint and
capabilities the description does not exceed (``register_capability_profile``). That is the profile's REGISTERED
state: describable to planning, never authorized, never selected; the catalog stays the one model registry.

A catalog may also record, per registered description, ONE ``ModelArtifactLocation``: where the model artifact's
bytes live as an EXTERNAL reference kept by the runtime (a storage backend id and a safe label, ``managed_by =
RUNTIME``). It is provenance only: never a path to copy, never a URL or credential, and it authorizes, installs,
downloads or selects nothing. Weights stay runtime-managed. The location is kept beside the description, so
``profile_digest`` never depends on it, and a catalog without locations has the exact same document and
``catalog_digest`` as before locations existed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.model import Distribution, LicenseInfo, Modality, Profiles
from pados.compute_node.manifest import ModelAvailability, ModelManifest, NodeCapability, NodeDescriptor
from pados.connectivity.requirements import NetworkRequirement, requirement_for_endpoint
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized
from pados.resources.capability_profile import MeasuredCapability, ModelCapabilityProfile, ObservationStatus
from pados.resources.hardware import GPU_RUNTIMES, RUNTIME_ORDER, AcceleratorRuntime

MODEL_PROFILE_SCHEMA = "pados.resources.model-runtime.v1"
MODEL_CATALOG_SCHEMA = "pados.resources.model-catalog.v1"
MAX_CATALOG_MODELS = 256
# Same rule as pados.compute_node.manifest (tests assert the two stay identical).
MODEL_ID_PATTERN = (r"^[a-z0-9][a-z0-9_.-]{0,39}/[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}"
                    r"@[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")
_MODEL_ID = re.compile(MODEL_ID_PATTERN)
_BACKEND = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
_FAMILY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_QUANTIZATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,23}$")
_PATH_LIKE = re.compile(r"\.\.|//|:/|/$")
_STORAGE_BACKEND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Slash-separated plain segments: no scheme, host, port, drive, query, fragment, credential or traversal.
_LOCATION_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}(?:/[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}){0,7}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
# Red-team F-6: a label is an opaque identifier, never an infrastructure identifier or a credential. Local rules (the
# resources package may not import pados.enterprise.secrets); the token shapes mirror its ``looks_like_secret``.
_LABEL_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_LABEL_HOST = re.compile(r"^(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
_LABEL_FQDN = re.compile(r"^(?:[A-Za-z0-9-]{1,63}\.){2,}[A-Za-z]{2,63}$")
_LABEL_HOST_WORDS = frozenset({"localhost", "home", "users", "root"})
_LABEL_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_|xox[abprs]-|(?:AKIA|ASIA)[0-9A-Z]{12,}"
    r"|AIza[0-9A-Za-z_-]{20,}|eyJ[A-Za-z0-9_-]{8,})")
_LABEL_TOKEN_SHAPED = re.compile(r"^(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{24,}$")


def _label_names_infrastructure_or_secret(label: str) -> bool:
    """Host names, IP addresses, home directories and credential-shaped segments are not opaque labels."""
    segments = label.split("/")
    first = segments[0]
    if first.lower() in _LABEL_HOST_WORDS or (len(segments) > 1 and _LABEL_HOST.fullmatch(first)):
        return True  # "<host>/<path>", "localhost/...", a home-directory-like relative path
    return _LABEL_SECRET.search(label) is not None or any(
        _LABEL_IPV4.fullmatch(s) or _LABEL_FQDN.fullmatch(s) or _LABEL_TOKEN_SHAPED.fullmatch(s) for s in segments)


class ModelRole(StrEnum):
    TEXT = "TEXT"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    VISION = "VISION"
    OCR = "OCR"


def canonical_model_id(value: str) -> str:
    if not _MODEL_ID.fullmatch(value) or _PATH_LIKE.search(value):
        raise ValueError("MODEL_ID_NOT_CANONICAL")
    return value


class ModelRuntimeProfile(BaseModel):
    """Requirements per runtime path: ``ram_mb`` for CPU/NPU, ``vram_mb`` for dedicated-GPU runtimes and
    ``unified_memory_mb`` for a unified-memory pool (METAL, or a GPU runtime on a unified machine)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = MODEL_PROFILE_SCHEMA
    model_id: str
    backend: str
    family: str
    roles: tuple[ModelRole, ...] = Field(min_length=1, max_length=len(ModelRole))
    modalities: tuple[Modality, ...] = Field(min_length=1, max_length=8)
    context_length: int = Field(ge=1, le=10_000_000)
    quantization: str | None = None
    runtimes: tuple[AcceleratorRuntime, ...] = Field(min_length=1, max_length=len(AcceleratorRuntime))
    min_cpu_cores: int = Field(default=1, ge=1, le=512)
    ram_mb: int = Field(default=0, ge=0, le=4_194_304)
    vram_mb: int = Field(default=0, ge=0, le=1_048_576)
    unified_memory_mb: int = Field(default=0, ge=0, le=4_194_304)
    profiles: Profiles = Profiles()  # coarse latency/cost/quality expectations; measurement is Outcome Learning
    network_requirement: NetworkRequirement = NetworkRequirement.LOCAL_ONLY
    max_classification: DataClassification = DataClassification.INTERNAL
    license: LicenseInfo
    tool_use: bool = False
    structured_output: bool = False

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != MODEL_PROFILE_SCHEMA:
            raise ValueError("MODEL_PROFILE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("model_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return canonical_model_id(value)

    @field_validator("backend")
    @classmethod
    def backend_name(cls, value: str) -> str:
        if not _BACKEND.fullmatch(value):
            raise ValueError("MODEL_BACKEND_INVALID")
        return value

    @field_validator("family")
    @classmethod
    def family_label(cls, value: str) -> str:
        if not _FAMILY.fullmatch(value) or ".." in value:
            raise ValueError("MODEL_FAMILY_INVALID")
        return value

    @field_validator("quantization")
    @classmethod
    def quantization_label(cls, value: str | None) -> str | None:
        if value is not None and (not _QUANTIZATION.fullmatch(value) or ".." in value):
            raise ValueError("MODEL_QUANTIZATION_INVALID")
        return value

    @model_validator(mode="after")
    def consistent(self) -> ModelRuntimeProfile:
        if self.model_id.split("/", 1)[0] != self.backend:
            raise ValueError("MODEL_ID_BACKEND_MISMATCH")
        for values, code in ((self.roles, "MODEL_ROLE_DUPLICATE"), (self.modalities, "MODEL_MODALITY_DUPLICATE"),
                             (self.runtimes, "MODEL_RUNTIME_DUPLICATE")):
            if len(set(values)) != len(values):
                raise ValueError(code)
        runtimes = set(self.runtimes)
        if runtimes & {AcceleratorRuntime.CPU, AcceleratorRuntime.NPU} and not self.ram_mb:
            raise ValueError("MODEL_RAM_REQUIREMENT_REQUIRED")
        if AcceleratorRuntime.METAL in runtimes and not self.unified_memory_mb:
            raise ValueError("MODEL_UNIFIED_MEMORY_REQUIREMENT_REQUIRED")
        if runtimes & (GPU_RUNTIMES - {AcceleratorRuntime.METAL}) and not self.vram_mb:
            raise ValueError("MODEL_VRAM_REQUIREMENT_REQUIRED")
        if (self.license.distribution is Distribution.EXTERNAL_SERVICE
                and self.network_requirement in {NetworkRequirement.LOCAL_ONLY, NetworkRequirement.NETWORK_OPTIONAL}):
            raise ValueError("EXTERNAL_SERVICE_MUST_DECLARE_NETWORK")
        return self

    @property
    def cpu_supported(self) -> bool:
        return AcceleratorRuntime.CPU in self.runtimes

    @property
    def gpu_supported(self) -> bool:
        return any(runtime in GPU_RUNTIMES for runtime in self.runtimes)

    @property
    def profile_digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class ArtifactManager(StrEnum):
    RUNTIME = "RUNTIME"  # the model runtime owns and manages the bytes; PADOS never copies them


class ModelArtifactLocation(BaseModel):
    """Where one model artifact's bytes live: an external reference (provenance, never authorization)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    backend_id: str  # the storage backend / runtime store that keeps the bytes
    label: str | None = None  # an opaque safe label inside that backend, never a path to copy
    managed_by: ArtifactManager = ArtifactManager.RUNTIME
    content_digest: str | None = None  # SHA-256 hex of the artifact bytes, when the runtime reports one

    @field_validator("model_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return canonical_model_id(value)

    @field_validator("backend_id")
    @classmethod
    def backend_label(cls, value: str) -> str:
        if not _STORAGE_BACKEND.fullmatch(value) or ".." in value:
            raise ValueError("MODEL_ARTIFACT_BACKEND_INVALID")
        return value

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str | None) -> str | None:
        if value is not None and (not _LOCATION_LABEL.fullmatch(value) or ".." in value
                                  or _label_names_infrastructure_or_secret(value)):
            raise ValueError("MODEL_ARTIFACT_LOCATION_NOT_A_SAFE_LABEL")
        return value

    @field_validator("content_digest")
    @classmethod
    def digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_HEX.fullmatch(value):
            raise ValueError("MODEL_ARTIFACT_DIGEST_INVALID")
        return value


class ModelCatalog:
    """Registered model descriptions, sorted by ``model_id``. Registered != authorized != selected."""

    def __init__(self, profiles: Iterable[ModelRuntimeProfile] = (),
                 capability_profiles: Iterable[ModelCapabilityProfile] = (),
                 artifact_locations: Iterable[ModelArtifactLocation] = ()) -> None:
        indexed: dict[str, ModelRuntimeProfile] = {}
        for profile in profiles:
            if not isinstance(profile, ModelRuntimeProfile):
                raise TypeError("MODEL_PROFILE_TYPE_INVALID")
            if profile.model_id in indexed:
                raise ValueError("MODEL_PROFILE_DUPLICATE")
            indexed[profile.model_id] = profile
        if len(indexed) > MAX_CATALOG_MODELS:
            raise ValueError("MODEL_CATALOG_TOO_LARGE")
        self._profiles = dict(sorted(indexed.items()))
        measured: dict[str, ModelCapabilityProfile] = {}
        for evidence in capability_profiles:
            if not isinstance(evidence, ModelCapabilityProfile):
                raise TypeError("CAPABILITY_PROFILE_TYPE_INVALID")
            # Re-validated: a model_construct()-forged profile (wrong id, roles without evidence) is refused.
            evidence = ModelCapabilityProfile.model_validate(evidence.model_dump(mode="json"))
            model_id = evidence.artifact.model_ref
            if model_id not in self._profiles:
                raise ValueError("CAPABILITY_PROFILE_MODEL_NOT_REGISTERED")
            if model_id in measured:
                raise ValueError("CAPABILITY_PROFILE_DUPLICATE")
            unsupported = description_exceeds_evidence(self._profiles[model_id], evidence)
            if unsupported:
                raise ValueError(f"MODEL_DESCRIPTION_EXCEEDS_EVIDENCE:{unsupported[0]}")
            measured[model_id] = evidence
        self._measured = dict(sorted(measured.items()))
        located: dict[str, ModelArtifactLocation] = {}
        for location in artifact_locations:
            if not isinstance(location, ModelArtifactLocation):
                raise TypeError("MODEL_ARTIFACT_LOCATION_TYPE_INVALID")
            location = ModelArtifactLocation.model_validate(location.model_dump(mode="json"))  # no forged values
            if location.model_id not in self._profiles:
                raise ValueError("MODEL_ARTIFACT_LOCATION_MODEL_NOT_REGISTERED")
            if location.model_id in located:
                raise ValueError("MODEL_ARTIFACT_LOCATION_DUPLICATE")
            located[location.model_id] = location
        self._locations = dict(sorted(located.items()))

    def register(self, profile: ModelRuntimeProfile) -> ModelCatalog:
        """A new catalog with one more description (catalogs are immutable values)."""
        return ModelCatalog((*self._profiles.values(), profile), self._measured.values(), self._locations.values())

    def register_capability_profile(self, evidence: ModelCapabilityProfile) -> ModelCatalog:
        """A new catalog in which the description of ``evidence.artifact.model_ref`` carries this measured profile.

        Registration only: nothing is authorized, routed, installed or downloaded. A model_id is bound to its
        manifest digest, so evidence for another artifact under the same tag can never attach here."""
        return ModelCatalog(self._profiles.values(), (*self._measured.values(), evidence), self._locations.values())

    def withdraw_capability_profile(self, model_id: str) -> ModelCatalog:
        """A new catalog without the measured profile of ``model_id`` (it stops being describable as measured)."""
        if model_id not in self._measured:
            raise KeyError("CAPABILITY_PROFILE_NOT_REGISTERED")
        return ModelCatalog(self._profiles.values(), (p for k, p in self._measured.items() if k != model_id),
                            self._locations.values())

    def locate(self, location: ModelArtifactLocation) -> ModelCatalog:
        """A new catalog recording where ``location.model_id``'s bytes live (replacing an earlier location).

        Provenance only: nothing is copied, downloaded, installed, authorized or selected."""
        others = (loc for key, loc in self._locations.items() if key != location.model_id)
        return ModelCatalog(self._profiles.values(), self._measured.values(), (*others, location))

    def artifact_location(self, model_id: str) -> ModelArtifactLocation | None:
        return self._locations.get(model_id)

    def artifact_locations(self) -> tuple[ModelArtifactLocation, ...]:
        return tuple(self._locations.values())

    def capability_profile(self, model_id: str) -> ModelCapabilityProfile | None:
        return self._measured.get(model_id)

    def capability_profiles(self) -> tuple[ModelCapabilityProfile, ...]:
        return tuple(self._measured.values())

    def get(self, model_id: str) -> ModelRuntimeProfile:
        try:
            return self._profiles[model_id]
        except KeyError:
            raise KeyError("MODEL_PROFILE_NOT_REGISTERED") from None

    def __iter__(self) -> Iterator[ModelRuntimeProfile]:
        return iter(tuple(self._profiles.values()))

    def __len__(self) -> int:
        return len(self._profiles)

    def to_document(self) -> dict[str, Any]:
        """The operator catalog document: descriptions plus the REGISTERED measured profiles (no authorization,
        no lifecycle beyond REGISTERED, nothing routable). ``from_document`` restores an identical catalog."""
        document: dict[str, Any] = {"schema_version": MODEL_CATALOG_SCHEMA,
                                    "models": [p.model_dump(mode="json") for p in self._profiles.values()]}
        if self._measured:
            document["capability_profiles"] = [p.model_dump(mode="json") for p in self._measured.values()]
        if self._locations:  # absent when empty: earlier documents and digests stay byte-for-byte identical
            document["artifact_locations"] = [loc.model_dump(mode="json") for loc in self._locations.values()]
        return document

    @property
    def catalog_digest(self) -> str:
        return canonical_hash(self.to_document())

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ModelCatalog:
        """Parse an operator catalog document (``{"schema_version", "models": [...]}`` and, optionally, the
        ``capability_profiles`` registered against those models); fails closed. Every measured profile is fully
        re-validated (``profile_id`` recomputed from content), so a tampered document is refused."""
        assert_minimized(document, allowed_keys=("schema_version", "models", "capability_profiles",
                                                 "artifact_locations"),
                         max_string=256, max_items=MAX_CATALOG_MODELS, max_total_bytes=4 * 1024 * 1024)
        if document.get("schema_version") != MODEL_CATALOG_SCHEMA:
            raise ValueError("MODEL_CATALOG_SCHEMA_UNSUPPORTED")
        models = document.get("models")
        if not isinstance(models, list):
            raise ValueError("MODEL_CATALOG_MODELS_INVALID")
        measured = document.get("capability_profiles", [])
        if not isinstance(measured, list):
            raise ValueError("MODEL_CATALOG_CAPABILITY_PROFILES_INVALID")
        located = document.get("artifact_locations", [])
        if not isinstance(located, list):
            raise ValueError("MODEL_CATALOG_ARTIFACT_LOCATIONS_INVALID")
        return cls((ModelRuntimeProfile.model_validate(item) for item in models),
                   (ModelCapabilityProfile.model_validate(item) for item in measured),
                   (ModelArtifactLocation.model_validate(item) for item in located))


class MemoryRequirement(BaseModel):
    """Operator-declared needs for a manifest model (manifests carry no memory figures)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtimes: tuple[AcceleratorRuntime, ...] = Field(min_length=1, max_length=len(AcceleratorRuntime))
    min_cpu_cores: int = Field(default=1, ge=1, le=512)
    ram_mb: int = Field(default=0, ge=0, le=4_194_304)
    vram_mb: int = Field(default=0, ge=0, le=1_048_576)
    unified_memory_mb: int = Field(default=0, ge=0, le=4_194_304)
    license: LicenseInfo
    tool_use: bool = False
    structured_output: bool = False


_ROLE_FROM_TASK: dict[NodeCapability, ModelRole] = {
    NodeCapability.LOCAL_INFERENCE: ModelRole.TEXT,
    NodeCapability.CLASSIFICATION: ModelRole.TEXT,
    NodeCapability.EVALUATION: ModelRole.TEXT,
    NodeCapability.DOCUMENT_EXTRACTION: ModelRole.TEXT,
    NodeCapability.EMBEDDING: ModelRole.EMBEDDING,
    NodeCapability.RERANK: ModelRole.RERANKER,
    NodeCapability.OCR: ModelRole.OCR,
}
_MODALITIES_FROM_ROLE: dict[ModelRole, tuple[Modality, ...]] = {
    ModelRole.TEXT: (Modality.TEXT,),
    ModelRole.EMBEDDING: (Modality.TEXT, Modality.EMBEDDING),
    ModelRole.RERANKER: (Modality.TEXT,),
    ModelRole.VISION: (Modality.IMAGE, Modality.TEXT),
    ModelRole.OCR: (Modality.IMAGE, Modality.PDF),
}


_T = TypeVar("_T")


def _ordered(items: Iterable[_T], order: Mapping[_T, int]) -> tuple[_T, ...]:
    return tuple(sorted(set(items), key=order.__getitem__))


def profile_from_manifest(manifest: ModelManifest, requirement: MemoryRequirement, *,
                          network_requirement: NetworkRequirement) -> ModelRuntimeProfile:
    """Read-only translation of a compute-node ``ModelManifest`` plus declared memory needs."""
    role_order = {role: index for index, role in enumerate(ModelRole)}
    modality_order = {modality: index for index, modality in enumerate(Modality)}
    roles = _ordered((_ROLE_FROM_TASK[task] for task in manifest.tasks), role_order)
    modalities = _ordered((m for role in roles for m in _MODALITIES_FROM_ROLE[role]), modality_order)
    return ModelRuntimeProfile(
        model_id=manifest.model_id, backend=manifest.backend, family=manifest.family, roles=roles,
        modalities=modalities, context_length=manifest.context_limit_tokens, quantization=manifest.quantization,
        runtimes=_ordered(requirement.runtimes, RUNTIME_ORDER), min_cpu_cores=requirement.min_cpu_cores,
        ram_mb=requirement.ram_mb, vram_mb=requirement.vram_mb, unified_memory_mb=requirement.unified_memory_mb,
        network_requirement=network_requirement, max_classification=manifest.privacy_class,
        license=requirement.license, tool_use=requirement.tool_use, structured_output=requirement.structured_output,
    )


@dataclass(frozen=True)
class NodeModelProfiles:
    profiles: tuple[ModelRuntimeProfile, ...]
    skipped: tuple[tuple[str, str], ...]  # (model_id, reason code), explained, never silently dropped


def profiles_from_node(node: NodeDescriptor,
                       requirements: Mapping[str, MemoryRequirement]) -> NodeModelProfiles:
    """Profiles for the models a node advertises as AVAILABLE and the operator sized explicitly.

    Network requirement derives from the node endpoint's locality (``requirement_for_endpoint``); the
    descriptor itself is not trusted for authority and nothing is contacted.
    """
    network = requirement_for_endpoint(node.endpoint)
    profiles: list[ModelRuntimeProfile] = []
    skipped: list[tuple[str, str]] = []
    for manifest in sorted(node.models, key=lambda item: item.model_id):
        requirement = requirements.get(manifest.model_id)
        if manifest.availability is not ModelAvailability.AVAILABLE:
            skipped.append((manifest.model_id, f"MODEL_NOT_AVAILABLE:{manifest.availability.value}"))
        elif requirement is None:
            skipped.append((manifest.model_id, "MODEL_MEMORY_REQUIREMENT_UNDECLARED"))
        else:
            try:
                profiles.append(profile_from_manifest(manifest, requirement, network_requirement=network))
            except ValueError:
                skipped.append((manifest.model_id, "MODEL_PROFILE_INVALID"))
    return NodeModelProfiles(tuple(profiles), tuple(skipped))


def description_exceeds_evidence(model: ModelRuntimeProfile, evidence: ModelCapabilityProfile) -> tuple[str, ...]:
    """What a description claims beyond the measured profile it would carry (empty = consistent)."""
    reasons: list[str] = []
    if model.backend != evidence.artifact.runtime_family:
        reasons.append("BACKEND")
    if model.quantization is not None and model.quantization != evidence.artifact.quantization:
        reasons.append("QUANTIZATION")
    contexts = [o.context_tokens for o in evidence.observations_of(MeasuredCapability.LONG_CONTEXT_RETRIEVAL)
                if o.all_passed and o.context_tokens]
    if model.context_length > max(contexts, default=evidence.footprint.base_context_tokens):
        reasons.append("CONTEXT_LENGTH")
    if model.tool_use and not evidence.observation(MeasuredCapability.TOOL_CALL_FORMATTING).all_passed:
        reasons.append("TOOL_USE")
    structured = evidence.observation(MeasuredCapability.STRUCTURED_JSON)
    if model.structured_output and not (structured.status is ObservationStatus.MEASURED and structured.all_passed):
        reasons.append("STRUCTURED_OUTPUT")
    if model.vram_mb and model.vram_mb < evidence.footprint.gpu_memory_mib:
        reasons.append("VRAM_BELOW_MEASURED_FOOTPRINT")
    return tuple(reasons)


def profile_from_capability(evidence: ModelCapabilityProfile, *, license: LicenseInfo,
                            max_classification: DataClassification = DataClassification.INTERNAL
                            ) -> ModelRuntimeProfile:
    """The catalog description a measured profile supports: only measured facts, nothing extrapolated.

    Context length is the largest context whose retrieval checks all passed; tool use and structured output are
    claimed only when every measured sample passed; memory is the measured GPU footprint on the measured
    accelerator (no CPU path is claimed because none was measured on its own)."""
    contexts = [o.context_tokens for o in evidence.observations_of(MeasuredCapability.LONG_CONTEXT_RETRIEVAL)
                if o.all_passed and o.context_tokens]
    accelerator = evidence.binding.accelerator
    measured = evidence.footprint.gpu_memory_mib
    cpu_path = accelerator in {AcceleratorRuntime.CPU, AcceleratorRuntime.NPU}
    unified = accelerator is AcceleratorRuntime.METAL
    return ModelRuntimeProfile(
        model_id=evidence.artifact.model_ref, backend=evidence.artifact.runtime_family,
        family=evidence.artifact.model_family, roles=(ModelRole.TEXT,), modalities=(Modality.TEXT,),
        context_length=max(contexts, default=evidence.footprint.base_context_tokens),
        quantization=evidence.artifact.quantization, runtimes=(accelerator,),
        network_requirement=NetworkRequirement.LOCAL_ONLY, max_classification=max_classification, license=license,
        tool_use=evidence.observation(MeasuredCapability.TOOL_CALL_FORMATTING).all_passed,
        structured_output=evidence.observation(MeasuredCapability.STRUCTURED_JSON).all_passed,
        ram_mb=measured if cpu_path else 0, unified_memory_mb=measured if unified else 0,
        vram_mb=0 if cpu_path or unified else measured)
