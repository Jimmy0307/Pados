# SPDX-License-Identifier: AGPL-3.0-only
"""Artifact references for capability inputs and outputs (immutable, content-addressed).

Capability stages exchange bytes only through hashes. Writing an artifact is never a semantic commit; the
existing ``pados.state_proof.ObjectStore`` (filesystem, SHA-256 addressed) is reused as the default store.

Adapters never receive the store itself: the executor hands them a ``ScopedArtifacts`` facade that can read
only the stage's authorized input references and write only within the stage's budget. ``media_type`` and
``kind`` are claims recorded with the hash; consumers re-validate content against their own contracts.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.state_proof.object_store import ObjectStore

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,80}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
MAX_READ_BYTES = 256 * 1024 * 1024


class ArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha256: str
    media_type: str
    size_bytes: int = Field(ge=0, le=2**40)
    kind: str

    @field_validator("sha256")
    @classmethod
    def sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("ARTIFACT_SHA256_INVALID")
        return value

    @field_validator("media_type")
    @classmethod
    def media(cls, value: str) -> str:
        if not _MEDIA.fullmatch(value):
            raise ValueError("ARTIFACT_MEDIA_TYPE_INVALID")
        return value

    @field_validator("kind")
    @classmethod
    def kind_name(cls, value: str) -> str:
        if not _KIND.fullmatch(value):
            raise ValueError("ARTIFACT_KIND_INVALID")
        return value


class ArtifactIntegrityError(ValueError):
    pass


class ArtifactAccessDenied(PermissionError):
    pass


class ArtifactStore(Protocol):
    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef: ...

    def get(self, ref: ArtifactRef) -> bytes: ...


class ObjectStoreArtifacts:
    """Trusted ArtifactStore over the existing ObjectStore (executor-side only; never handed to adapters)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._store = ObjectStore(self._root)

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        digest = hashlib.sha256(payload).hexdigest()
        # Validate the claimed metadata before anything is written.
        ref = ArtifactRef(sha256=digest, media_type=media_type, size_bytes=len(payload), kind=kind)
        stored = self._store.put_bytes(payload, media_type=media_type, object_kind=kind)
        if stored.content_hash != digest:
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")
        return ref

    def get(self, ref: ArtifactRef) -> bytes:
        path = self._root / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
        # The real path must be exactly the expected in-root location (no symlinked components).
        if os.path.realpath(path) != str(path):
            raise ArtifactIntegrityError("ARTIFACT_PATH_INVALID")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ArtifactIntegrityError("ARTIFACT_MISSING") from exc
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(MAX_READ_BYTES + 1)
        if len(payload) > MAX_READ_BYTES:
            raise ArtifactIntegrityError("ARTIFACT_TOO_LARGE")
        if hashlib.sha256(payload).hexdigest() != ref.sha256 or len(payload) != ref.size_bytes:
            raise ArtifactIntegrityError("ARTIFACT_HASH_MISMATCH")
        return payload


class ScopedArtifacts:
    """The only artifact access an adapter gets: authorized inputs readable, bounded writes."""

    def __init__(self, store: ArtifactStore, *, readable: Iterable[ArtifactRef], max_outputs: int = 32,
                 max_output_bytes: int = 64 * 1024 * 1024) -> None:
        self._store = store
        self._readable = {ref.sha256: ref for ref in readable}
        self._max_outputs = max_outputs
        self._budget = max_output_bytes
        self.written: list[ArtifactRef] = []

    def get(self, ref: ArtifactRef) -> bytes:
        allowed = self._readable.get(ref.sha256)
        if allowed is None or allowed != ref:
            raise ArtifactAccessDenied("ARTIFACT_NOT_AN_AUTHORIZED_INPUT")
        return self._store.get(allowed)

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        if len(self.written) >= self._max_outputs or len(payload) > self._budget:
            raise ArtifactAccessDenied("ARTIFACT_OUTPUT_BUDGET_EXCEEDED")
        ref = self._store.put(payload, media_type=media_type, kind=kind)
        self._budget -= len(payload)
        self.written.append(ref)
        return ref
