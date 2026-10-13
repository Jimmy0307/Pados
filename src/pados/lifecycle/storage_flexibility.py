# SPDX-License-Identifier: AGPL-3.0-only
"""Storage Flexibility V1: small authoritative state locally, large assets by reference (policy; no new authority).

PADOS keeps SMALL AUTHORITATIVE STATE (metadata, identities, hashes, manifests, receipts, indexes, policy,
capability profiles, small evidence) locally and avoids LARGE DUPLICATED ASSETS (model weights, large binary
sources, duplicated artifacts, rebuildable caches, large intermediates). This module decides WHERE bytes should
live and WHAT may be dropped under pressure. It never decides what is TRUE:

* identity is the same SHA-256 content address ``ObjectStore`` already uses, computed by PADOS from the bytes and
  re-verified on every read, whatever backend holds them (no second CAS; semantic acceptance stays with
  ``StateProofService.commit_result``);
* backends are replaceable placements behind ``StorageBackend``: the existing local ``ObjectStore``, an external
  disk / NAS / object store, or a reference to bytes another system keeps (a model runtime's weights);
* the placement index maps many logical references to one physical artifact (deduplication) and is an immutable
  value (every change returns a new index);
* storage pressure is an observable condition (``NORMAL`` / ``PRESSURED`` / ``CRITICAL``) with advisory signals
  for planning. It never changes authorization or semantic truth;
* eviction is a RECOMMENDATION (``EvictionPlan``). Only rebuildable data, or data with a verified replica on
  another backend, is ever a candidate. Authoritative state, receipts, audit evidence and unique unreplicated
  artifacts are never candidates. Physical deletion stays with the operator-invoked ``pados.lifecycle.purge``.

Limits come from configuration (``StorageBudget``); nothing here knows a disk size.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,159}$")


class StorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StorageTier(StrEnum):
    HOT = "HOT"  # active authoritative state, receipts, current work artifacts, small indexes
    WARM = "WARM"  # recent artifacts and evidence
    COLD = "COLD"  # historical artifacts, archives, datasets, old exports
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"  # bytes held elsewhere; PADOS keeps identity and provenance only
    EPHEMERAL = "EPHEMERAL"  # rebuildable cache, temporary extraction / embeddings / benchmark files


class ArtifactClass(StrEnum):
    AUTHORITATIVE_STATE = "AUTHORITATIVE_STATE"  # metadata, identities, manifests, policy, profiles, task state
    RECEIPT_OR_EVIDENCE = "RECEIPT_OR_EVIDENCE"  # receipts and audit evidence
    PROJECT_ARTIFACT = "PROJECT_ARTIFACT"  # work products and project sources
    LARGE_SOURCE = "LARGE_SOURCE"  # large binary inputs, datasets, archives
    MODEL_WEIGHTS = "MODEL_WEIGHTS"  # managed by a model runtime; never copied into PADOS storage
    REBUILDABLE_CACHE = "REBUILDABLE_CACHE"  # derivable again from other bytes
    INTERMEDIATE = "INTERMEDIATE"  # temporary outputs of a pipeline step

    @property
    def protected(self) -> bool:
        """Never evicted, never externalized away from a verified local copy by policy alone."""
        return self in {ArtifactClass.AUTHORITATIVE_STATE, ArtifactClass.RECEIPT_OR_EVIDENCE}

    @property
    def rebuildable(self) -> bool:
        return self in {ArtifactClass.REBUILDABLE_CACHE, ArtifactClass.INTERMEDIATE}


class StoragePressure(StrEnum):
    NORMAL = "NORMAL"
    PRESSURED = "PRESSURED"
    CRITICAL = "CRITICAL"


class PressureAdvice(StrEnum):
    """Advisory signals for planning and execution (no authority; the planner may ignore them)."""

    AVOID_LARGE_LOCAL_DOWNLOADS = "AVOID_LARGE_LOCAL_DOWNLOADS"
    PREFER_RESIDENT_RESOURCES = "PREFER_RESIDENT_RESOURCES"
    EXTERNALIZE_LARGE_ARTIFACTS = "EXTERNALIZE_LARGE_ARTIFACTS"
    PREFER_REMOTE_CAPABILITY_WHERE_PERMITTED = "PREFER_REMOTE_CAPABILITY_WHERE_PERMITTED"
    REDUCE_CACHE_RETENTION = "REDUCE_CACHE_RETENTION"


def content_id(payload: bytes) -> str:
    """The one identity scheme: the SHA-256 hex digest of the bytes (as ``ObjectStore`` addresses them)."""
    return hashlib.sha256(payload).hexdigest()


def _digest(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("CONTENT_ID_INVALID")
    return value


def _label(value: str) -> str:
    if not _LABEL.fullmatch(value) or ".." in value:
        raise ValueError("STORAGE_LABEL_INVALID")
    return value


# --- budget and pressure ----------------------------------------------------------------------------------------

class StorageBudget(BaseModel):
    """Operator configuration. ``None`` means "no limit configured"; nothing is assumed about the disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Numbers are strict: a string or a boolean where a size belongs is refused, never coerced (fail closed).
    min_free_bytes: StrictInt = Field(default=0, ge=0)  # reserve kept free for the OS and other work
    pressured_free_fraction: float = Field(default=0.15, gt=0.0, lt=1.0, strict=True)
    critical_free_fraction: float = Field(default=0.05, gt=0.0, lt=1.0, strict=True)
    project_budget_bytes: Mapping[str, StrictInt] = Field(default_factory=dict)  # per project, local bytes
    cache_budget_bytes: StrictInt | None = Field(default=None, ge=0)
    max_local_residency_bytes: StrictInt | None = Field(default=None, ge=1)  # larger artifacts go by reference
    local_budget_bytes: StrictInt | None = Field(default=None, ge=1)  # total PADOS-held local bytes (V1+)

    @model_validator(mode="after")
    def ordered(self) -> StorageBudget:
        if self.critical_free_fraction >= self.pressured_free_fraction:
            raise ValueError("STORAGE_PRESSURE_THRESHOLDS_INVALID")
        if any(v < 0 for v in self.project_budget_bytes.values()):
            raise ValueError("STORAGE_PROJECT_BUDGET_INVALID")
        if any(not _LABEL.fullmatch(k) or ".." in k for k in self.project_budget_bytes):
            raise ValueError("STORAGE_PROJECT_ID_INVALID")
        return self


class StorageObservation(BaseModel):
    """Free / total bytes of the volume holding PADOS local storage (from the device profiler or the OS)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_bytes: int = Field(ge=1)
    free_bytes: int = Field(ge=0)


_PRESSURE_ORDER = (StoragePressure.NORMAL, StoragePressure.PRESSURED, StoragePressure.CRITICAL)


def _headroom_pressure(headroom: int, total: int, budget: StorageBudget) -> StoragePressure:
    if headroom <= total * budget.critical_free_fraction:
        return StoragePressure.CRITICAL
    if headroom <= total * budget.pressured_free_fraction:
        return StoragePressure.PRESSURED
    return StoragePressure.NORMAL


def pressure_of(observation: StorageObservation | None, budget: StorageBudget,
                local_bytes: int | None = None) -> StoragePressure:
    """Unknown free space is treated as PRESSURED (fail safe: avoid growth, never delete on that basis).

    With a configured ``local_budget_bytes`` and the bytes PADOS holds locally (``local_bytes``), the same
    fractions apply to the budget's headroom; the result is the worse of the disk and the budget condition."""
    if observation is None:
        disk = StoragePressure.PRESSURED
    else:
        disk = _headroom_pressure(observation.free_bytes - budget.min_free_bytes, observation.total_bytes, budget)
    if budget.local_budget_bytes is None or local_bytes is None:
        return disk
    own = _headroom_pressure(budget.local_budget_bytes - local_bytes, budget.local_budget_bytes, budget)
    return max(disk, own, key=_PRESSURE_ORDER.index)


def pressure_advice(pressure: StoragePressure) -> tuple[PressureAdvice, ...]:
    if pressure is StoragePressure.NORMAL:
        return ()
    advice = [PressureAdvice.AVOID_LARGE_LOCAL_DOWNLOADS, PressureAdvice.PREFER_RESIDENT_RESOURCES,
              PressureAdvice.EXTERNALIZE_LARGE_ARTIFACTS, PressureAdvice.REDUCE_CACHE_RETENTION]
    if pressure is StoragePressure.CRITICAL:
        advice.append(PressureAdvice.PREFER_REMOTE_CAPABILITY_WHERE_PERMITTED)
    return tuple(advice)


# --- backends -----------------------------------------------------------------------------------------------------

class StorageBackend(Protocol):
    """A replaceable place for bytes. Identity is computed and verified by PADOS, never trusted from a backend."""

    backend_id: str
    holds_bytes: bool  # False: the backend only references bytes another system keeps

    def has(self, digest: str) -> bool: ...

    def put(self, payload: bytes) -> str: ...

    def get(self, digest: str) -> bytes: ...


def _verified(digest: str, payload: bytes) -> bytes:
    if content_id(payload) != digest:
        raise StorageError("STORAGE_CONTENT_HASH_MISMATCH")
    return payload


class ObjectStoreBackend:
    """The EXISTING local content-addressed ``ObjectStore`` (same layout, same identity) as a placement."""

    holds_bytes = True

    def __init__(self, store: object, backend_id: str = "local-object-store") -> None:
        root = getattr(store, "root", None)
        if not isinstance(root, Path) or not callable(getattr(store, "put_bytes", None)):
            raise StorageError("OBJECT_STORE_REQUIRED")
        self._store = store
        self._root = root
        self.backend_id = _label(backend_id)

    def _path(self, digest: str) -> Path:
        digest = _digest(digest)
        return self._root / digest[:2] / digest[2:4] / digest

    def has(self, digest: str) -> bool:
        path = self._path(digest)
        return path.is_file() and not path.is_symlink()

    def put(self, payload: bytes) -> str:
        ref = self._store.put_bytes(payload, media_type="application/octet-stream",  # type: ignore[attr-defined]
                                    object_kind="storage-placement")
        digest = str(ref.content_hash)
        if digest != content_id(payload):
            raise StorageError("STORAGE_CONTENT_HASH_MISMATCH")
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        if path.is_symlink() or not path.is_file():
            raise StorageError("STORAGE_ARTIFACT_MISSING")
        return _verified(digest, path.read_bytes())


class MemoryBackend:
    """A byte-holding backend kept in memory (tests, and the shape of any remote object store adapter)."""

    holds_bytes = True

    def __init__(self, backend_id: str) -> None:
        self.backend_id = _label(backend_id)
        self._objects: dict[str, bytes] = {}

    def has(self, digest: str) -> bool:
        return _digest(digest) in self._objects

    def put(self, payload: bytes) -> str:
        digest = content_id(payload)
        self._objects.setdefault(digest, bytes(payload))
        return digest

    def get(self, digest: str) -> bytes:
        if not self.has(digest):
            raise StorageError("STORAGE_ARTIFACT_MISSING")
        return _verified(digest, self._objects[digest])


class ReferenceBackend:
    """Bytes another system keeps (a model runtime's store, an archive, a NAS PADOS does not manage).

    PADOS never copies them: ``put`` is refused. ``get`` goes through the caller-supplied ``fetch`` and is
    verified against the recorded identity; ``has`` reports only what ``probe`` (also caller-supplied) says."""

    holds_bytes = False

    def __init__(self, backend_id: str, *, probe: Callable[[str], bool],
                 fetch: Callable[[str], bytes] | None = None) -> None:
        self.backend_id = _label(backend_id)
        self._probe = probe
        self._fetch = fetch

    def has(self, digest: str) -> bool:
        return bool(self._probe(_digest(digest)))

    def put(self, payload: bytes) -> str:
        raise StorageError("REFERENCE_BACKEND_HOLDS_NO_PADOS_BYTES")

    def get(self, digest: str) -> bytes:
        if self._fetch is None:
            raise StorageError("REFERENCE_NOT_FETCHABLE")
        return _verified(_digest(digest), self._fetch(digest))


# --- placement index (immutable) ---------------------------------------------------------------------------------

class Placement(BaseModel):
    """Where one artifact's bytes are (or are referenced). ``location`` is an opaque label, never a secret URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    tier: StorageTier
    location: str | None = None
    verified: bool = False  # the bytes at this placement hashed to the artifact identity when last checked

    @field_validator("backend_id")
    @classmethod
    def backend(cls, value: str) -> str:
        return _label(value)

    @field_validator("location")
    @classmethod
    def location_label(cls, value: str | None) -> str | None:
        if value is not None and (not _LABEL.fullmatch(value) or "?" in value or "@" in value.split("/")[0]):
            raise ValueError("STORAGE_LOCATION_NOT_A_SAFE_LABEL")  # no query strings, credentials or signatures
        return value


class ArtifactEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    size_bytes: int = Field(ge=0)
    artifact_class: ArtifactClass
    project_id: str | None = None
    placements: tuple[Placement, ...] = ()
    references: tuple[str, ...] = ()  # logical references (many -> one physical artifact)

    @field_validator("digest")
    @classmethod
    def identity(cls, value: str) -> str:
        return _digest(value)

    def local_bytes(self, local_backends: frozenset[str]) -> int:
        return self.size_bytes if any(p.backend_id in local_backends for p in self.placements) else 0

    def replicated_outside(self, backend_id: str) -> bool:
        return any(p.backend_id != backend_id and p.verified for p in self.placements)


class PlacementIndex(BaseModel):
    """digest -> entry. Every operation returns a NEW index (immutable value)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: Mapping[str, ArtifactEntry] = Field(default_factory=dict)

    def entry(self, digest: str) -> ArtifactEntry | None:
        return self.entries.get(digest)

    def with_entry(self, entry: ArtifactEntry) -> PlacementIndex:
        return PlacementIndex(entries={**self.entries, entry.digest: entry})

    def resolve(self, reference: str) -> ArtifactEntry | None:
        return next((e for e in self.entries.values() if reference in e.references), None)

    def local_bytes(self, local_backends: frozenset[str]) -> int:
        """Physical bytes held on the given local backends (each deduplicated artifact counted once)."""
        return sum(e.local_bytes(local_backends) for e in self.entries.values())


# --- placement decisions ---------------------------------------------------------------------------------------

def choose_tier(artifact_class: ArtifactClass, size_bytes: int, budget: StorageBudget,
                pressure: StoragePressure) -> StorageTier:
    """Where new bytes of this class and size should live. Authoritative state always stays local (HOT)."""
    if artifact_class.protected:
        return StorageTier.HOT
    if artifact_class is ArtifactClass.MODEL_WEIGHTS:
        return StorageTier.EXTERNAL_REFERENCE  # the runtime manages weights; PADOS keeps identity and provenance
    if artifact_class.rebuildable:
        return StorageTier.EPHEMERAL
    large = budget.max_local_residency_bytes is not None and size_bytes > budget.max_local_residency_bytes
    if large or artifact_class is ArtifactClass.LARGE_SOURCE and pressure is not StoragePressure.NORMAL:
        return StorageTier.EXTERNAL_REFERENCE
    return StorageTier.WARM if pressure is StoragePressure.NORMAL else StorageTier.COLD


class StorageManager:
    """Places bytes on replaceable backends and keeps the (immutable) placement index. Holds no authority."""

    def __init__(self, *, local: StorageBackend, external: StorageBackend | None = None,
                 budget: StorageBudget | None = None) -> None:
        if not local.holds_bytes:
            raise StorageError("LOCAL_BACKEND_MUST_HOLD_BYTES")
        self.local = local
        self.external = external
        self.budget = budget or StorageBudget()

    @property
    def local_backends(self) -> frozenset[str]:
        return frozenset({self.local.backend_id})

    def store(self, index: PlacementIndex, payload: bytes, *, reference: str, artifact_class: ArtifactClass,
              observation: StorageObservation | None, project_id: str | None = None) -> PlacementIndex:
        """Place bytes (or reuse an identical artifact) and record the logical reference. Returns a new index."""
        if artifact_class is ArtifactClass.MODEL_WEIGHTS:
            raise StorageError("MODEL_WEIGHTS_ARE_REFERENCED_NOT_STORED")  # use ``reference_external``
        digest = content_id(payload)
        existing = index.entry(digest)
        if existing is not None:  # deduplicated: one physical artifact, another logical reference
            refs = existing.references if reference in existing.references else (*existing.references, reference)
            return index.with_entry(existing.model_copy(update={"references": refs}))
        used = index.local_bytes(self.local_backends)
        pressure = pressure_of(observation, self.budget, used)
        tier = choose_tier(artifact_class, len(payload), self.budget, pressure)
        local_write = tier is not StorageTier.EXTERNAL_REFERENCE
        if pressure is StoragePressure.CRITICAL and not artifact_class.protected and local_write:
            raise StorageError("STORAGE_CRITICAL_LOCAL_WRITE_REFUSED")
        limit = self.budget.local_budget_bytes
        if limit is not None and local_write and not artifact_class.protected and used + len(payload) > limit:
            raise StorageError("LOCAL_STORAGE_BUDGET_EXCEEDED")  # authoritative state is still written
        self._check_project_budget(index, project_id, len(payload), tier)
        backend = self.local
        if tier is StorageTier.EXTERNAL_REFERENCE:
            if self.external is None or not self.external.holds_bytes:
                raise StorageError("EXTERNAL_STORAGE_NOT_CONFIGURED")
            backend = self.external
        if backend.put(payload) != digest:
            raise StorageError("STORAGE_CONTENT_HASH_MISMATCH")
        entry = ArtifactEntry(digest=digest, size_bytes=len(payload), artifact_class=artifact_class,
                              project_id=project_id, references=(reference,),
                              placements=(Placement(backend_id=backend.backend_id, tier=tier, verified=True),))
        return index.with_entry(entry)

    def reference_external(self, index: PlacementIndex, *, digest: str, size_bytes: int, reference: str,
                           artifact_class: ArtifactClass, backend: StorageBackend,
                           location: str | None = None) -> PlacementIndex:
        """Record identity and provenance of bytes another system keeps (e.g. model weights): nothing is copied."""
        if backend.holds_bytes and artifact_class is ArtifactClass.MODEL_WEIGHTS:
            raise StorageError("MODEL_WEIGHTS_MUST_STAY_WITH_THEIR_RUNTIME")
        present = backend.has(digest)
        entry = index.entry(digest) or ArtifactEntry(digest=digest, size_bytes=size_bytes,
                                                     artifact_class=artifact_class)
        placement = Placement(backend_id=backend.backend_id, tier=StorageTier.EXTERNAL_REFERENCE, location=location,
                              verified=False)  # availability is observed, identity is verified only on read
        others = tuple(p for p in entry.placements if p.backend_id != backend.backend_id)
        refs = entry.references if reference in entry.references else (*entry.references, reference)
        if not present:
            raise StorageError("EXTERNAL_ARTIFACT_NOT_AVAILABLE")
        return index.with_entry(entry.model_copy(update={"placements": (*others, placement), "references": refs}))

    def read(self, index: PlacementIndex, reference: str, backends: Iterable[StorageBackend]) -> bytes:
        """Read from any placement; every backend's bytes are re-verified against the recorded identity."""
        entry = index.resolve(reference)
        if entry is None:
            raise StorageError("STORAGE_REFERENCE_UNKNOWN")
        by_id = {b.backend_id: b for b in backends}
        for placement in entry.placements:
            backend = by_id.get(placement.backend_id)
            if backend is not None and backend.has(entry.digest):
                return backend.get(entry.digest)
        raise StorageError("STORAGE_ARTIFACT_UNAVAILABLE")

    def replicate(self, index: PlacementIndex, digest: str, target: StorageBackend,
                  tier: StorageTier = StorageTier.COLD) -> PlacementIndex:
        """Copy an artifact to another byte-holding backend (verified), e.g. before externalizing it."""
        entry = index.entry(digest)
        if entry is None or not target.holds_bytes:
            raise StorageError("STORAGE_REPLICATION_INVALID")
        payload = self.read(index, entry.references[0], (self.local, *((self.external,) if self.external else ())))
        if target.put(payload) != digest:
            raise StorageError("STORAGE_CONTENT_HASH_MISMATCH")
        placement = Placement(backend_id=target.backend_id, tier=tier, verified=True)
        others = tuple(p for p in entry.placements if p.backend_id != target.backend_id)
        return index.with_entry(entry.model_copy(update={"placements": (*others, placement)}))

    def _check_project_budget(self, index: PlacementIndex, project_id: str | None, size: int,
                              tier: StorageTier) -> None:
        if project_id is None or tier is StorageTier.EXTERNAL_REFERENCE:
            return
        limit = self.budget.project_budget_bytes.get(project_id)
        if limit is None:
            return
        used = sum(e.local_bytes(self.local_backends) for e in index.entries.values() if e.project_id == project_id)
        if used + size > limit:
            raise StorageError("PROJECT_STORAGE_BUDGET_EXCEEDED")


# --- eviction (recommendation only) -------------------------------------------------------------------------------

class EvictionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    size_bytes: int
    reason: str  # REBUILDABLE | REPLICATED_ELSEWHERE


class EvictionPlan(BaseModel):
    """Which LOCAL copies could be dropped. A recommendation: deletion stays with ``pados.lifecycle.purge``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pressure: StoragePressure
    candidates: tuple[EvictionCandidate, ...]
    reclaimable_bytes: int
    protected_bytes: int
    authority: str = "NONE"
    deletes: bool = False  # never: this module performs no deletion


def plan_eviction(index: PlacementIndex, local_backend_id: str, pressure: StoragePressure,
                  budget: StorageBudget, *, cache_bytes_in_use: int | None = None) -> EvictionPlan:
    """Rebuildable local data first, then local copies with a verified replica elsewhere. Protected classes and
    unique unreplicated artifacts are never candidates, whatever the pressure."""
    rebuildable: list[EvictionCandidate] = []
    replicated: list[EvictionCandidate] = []
    protected = 0
    for entry in sorted(index.entries.values(), key=lambda e: (-e.size_bytes, e.digest)):
        local = any(p.backend_id == local_backend_id for p in entry.placements)
        if not local:
            continue
        if entry.artifact_class.protected:
            protected += entry.size_bytes
        elif entry.artifact_class.rebuildable:
            rebuildable.append(EvictionCandidate(digest=entry.digest, size_bytes=entry.size_bytes,
                                                 reason="REBUILDABLE"))
        elif entry.replicated_outside(local_backend_id):
            replicated.append(EvictionCandidate(digest=entry.digest, size_bytes=entry.size_bytes,
                                                reason="REPLICATED_ELSEWHERE"))
    over_cache = cache_bytes_in_use is not None and budget.cache_budget_bytes is not None \
        and cache_bytes_in_use > budget.cache_budget_bytes
    if pressure is StoragePressure.NORMAL:
        chosen = rebuildable if over_cache else []
    elif pressure is StoragePressure.PRESSURED:
        chosen = rebuildable
    else:
        chosen = rebuildable + replicated
    return EvictionPlan(pressure=pressure, candidates=tuple(chosen),
                        reclaimable_bytes=sum(c.size_bytes for c in chosen), protected_bytes=protected)


# --- advisory pressure snapshot (V1+; for a planner integration that may consume it later) -----------------------

PRESSURE_SNAPSHOT_SCHEMA: Literal["pados.lifecycle.storage-pressure.v1"] = "pados.lifecycle.storage-pressure.v1"


class StoragePressureSnapshot(BaseModel):
    """A read-only, advisory view of storage pressure. It carries no authority by construction: the planner,
    router, scheduler, authorization and semantic state never read it today, and a consumer may use it only
    inside its existing feasible set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.lifecycle.storage-pressure.v1"] = PRESSURE_SNAPSHOT_SCHEMA
    pressure: StoragePressure
    advice: tuple[PressureAdvice, ...]
    observed: bool  # False: free space unknown, so the disk condition is the fail-safe PRESSURED
    free_bytes: int | None = None
    total_bytes: int | None = None
    min_free_bytes: int = 0
    local_bytes: int | None = None
    local_budget_bytes: int | None = None
    authority: Literal["NONE"] = "NONE"
    affects_authorization: Literal[False] = False
    affects_semantic_state: Literal[False] = False
    recommendation_only: Literal[True] = True


def pressure_snapshot(observation: StorageObservation | None, budget: StorageBudget, *,
                      index: PlacementIndex | None = None,
                      local_backends: frozenset[str] = frozenset()) -> StoragePressureSnapshot:
    """Pure: the same inputs give the same snapshot; nothing is read from disk, written or decided."""
    local = index.local_bytes(local_backends) if index is not None else None
    pressure = pressure_of(observation, budget, local)
    return StoragePressureSnapshot(
        pressure=pressure, advice=pressure_advice(pressure), observed=observation is not None,
        free_bytes=None if observation is None else observation.free_bytes,
        total_bytes=None if observation is None else observation.total_bytes,
        min_free_bytes=budget.min_free_bytes, local_bytes=local, local_budget_bytes=budget.local_budget_bytes)
