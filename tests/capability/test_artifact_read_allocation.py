# SPDX-License-Identifier: AGPL-3.0-only
"""DI-9: ``ObjectStoreArtifacts.get`` must not ask the OS for a 256 MiB buffer to read a small artifact.

``read(MAX_READ_BYTES + 1)`` makes CPython allocate (and on Windows commit) the full 256 MiB buffer on every call:
about 64 ms per read on the Windows reference host against 0.06 ms for a size-bounded read, and it dominated every
governed stage (86% of a profiled BM25 + evidence task). The bound, the hash check and the growth check must stay
exactly as they were; only the requested buffer changes.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from pados.capability import artifacts as artifacts_module
from pados.capability.artifacts import ArtifactIntegrityError, ObjectStoreArtifacts


def _recording_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    requested: list[int] = []
    original = os.fdopen

    class Recording:
        def __init__(self, inner: io.BufferedReader) -> None:
            self.inner = inner

        def read(self, size: int = -1) -> bytes:
            requested.append(size)
            return self.inner.read(size)

        def fileno(self) -> int:
            return self.inner.fileno()

        def __enter__(self) -> Recording:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()

    def fdopen(descriptor: int, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = original(descriptor, mode, *args, **kwargs)  # type: ignore[call-overload]
        return Recording(handle) if mode == "rb" else handle

    monkeypatch.setattr(artifacts_module.os, "fdopen", fdopen)
    return requested


def test_a_read_requests_only_the_file_plus_one_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"x" * 5000, media_type="application/json", kind="test.kind")
    requested = _recording_reads(monkeypatch)
    assert store.get(ref) == b"x" * 5000
    assert requested and max(requested) <= 5001


def test_the_bound_and_integrity_checks_are_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    ref = store.put(b"payload", media_type="application/json", kind="test.kind")
    path = tmp_path / "objects" / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
    os.chmod(path, 0o600)
    path.write_bytes(b"payload-grown")  # grown after it was written: still a hash mismatch, never accepted
    with pytest.raises(ArtifactIntegrityError) as excinfo:
        store.get(ref)
    assert excinfo.value.args[0] == "ARTIFACT_HASH_MISMATCH"
    monkeypatch.setattr(artifacts_module, "MAX_READ_BYTES", 4)
    with pytest.raises(ArtifactIntegrityError) as excinfo:
        store.get(ref)
    assert excinfo.value.args[0] == "ARTIFACT_TOO_LARGE"
