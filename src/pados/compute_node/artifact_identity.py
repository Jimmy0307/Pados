# SPDX-License-Identifier: AGPL-3.0-only
"""Verifiable identity of a local single-file model artifact that a runtime loads by PATH and reports no digest for.

Some runtimes (e.g. a llama.cpp server) report which file they serve, but no content digest. PADOS then derives the
identity itself, from the bytes, and only for files inside an operator-configured artifact root:

    layer digest    = sha256(file bytes)
    manifest digest = sha256(canonical JSON {"schema", "media_type", "layers": [{"digest", "size_bytes"}]})

The identity is content-addressed (file name, path and timestamps are not part of it), deterministic and reproducible
by anyone holding the file (``sha256sum`` plus the canonical manifest below). No digest is ever fabricated: a path
outside the root, a relative path (the runtime's working directory is unknown), a link, a non-regular file, a file
without the format's magic bytes, a split (multi-file) model, a file that changed while it was hashed or any I/O
failure gives ``None``, and a binding that needs the digest is then ``MODEL_ARTIFACT_NOT_PRESENT`` (fail closed).

Reads only: the configured root's files, by the path the runtime reported. Nothing is written, moved or downloaded.
Hashes are cached by (resolved path, size, modification time, status-change time, inode, device), so a
re-observation re-reads nothing unless the file changed; the cache is bounded. The open file is re-stat'ed after
hashing and must still carry the same key (otherwise ``None``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ARTIFACT_MANIFEST_SCHEMA = "pados.local-artifact-manifest.v1"
GGUF_MEDIA_TYPE = "application/x-gguf"
GGUF_MAGIC = b"GGUF"
MAX_REPORTED_PATH = 1024
_CHUNK = 1 << 20
_CACHE_LIMIT = 64
_SPLIT = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)


@dataclass(frozen=True)
class LocalArtifact:
    manifest_digest: str  # sha256:<hex> of the canonical manifest
    layer_digest: str  # sha256:<hex> of the file bytes
    size_bytes: int


def manifest_digest_for(layer_digest: str, size_bytes: int) -> str:
    """The canonical manifest digest of a single-layer local artifact (reproducible without PADOS)."""
    document = {"schema": ARTIFACT_MANIFEST_SCHEMA, "media_type": GGUF_MEDIA_TYPE,
                "layers": [{"digest": layer_digest, "size_bytes": size_bytes}]}
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


_CacheKey = tuple[str, int, int, int, int, int]
_cache: dict[_CacheKey, LocalArtifact] = {}
_cache_lock = threading.Lock()


class LocalArtifactIdentity:
    """Derives ``LocalArtifact`` identities for files under ONE operator-configured root (absolute path)."""

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("ARTIFACT_ROOT_MUST_BE_ABSOLUTE")
        self.root = path.resolve()

    def identify(self, reported_path: object) -> LocalArtifact | None:
        """The identity of the file a runtime reported it serves, or ``None`` (never a guessed digest)."""
        if not isinstance(reported_path, str) or not reported_path or len(reported_path) > MAX_REPORTED_PATH \
                or "\x00" in reported_path:
            return None
        candidate = Path(reported_path)
        if not candidate.is_absolute() or _SPLIT.search(candidate.name) or candidate.suffix.lower() != ".gguf":
            return None
        try:
            if stat.S_ISLNK(candidate.lstat().st_mode):
                return None
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(self.root) or not stat.S_ISREG(resolved.stat().st_mode):
                return None
            with resolved.open("rb") as handle:
                key = _signature(resolved, os.fstat(handle.fileno()))
                with _cache_lock:
                    cached = _cache.get(key)
                if cached is not None and _same_file(resolved, key):
                    return cached
                artifact = _hash_handle(handle, key[1])
                after = _signature(resolved, os.fstat(handle.fileno()))  # re-stat the OPEN file (red-team F-2)
            if artifact is None or after != key or not _same_file(resolved, key):
                return None  # not the expected format, the file changed while it was read, or the path was swapped
        except (OSError, ValueError, RuntimeError):
            return None
        with _cache_lock:
            if len(_cache) >= _CACHE_LIMIT:
                _cache.pop(next(iter(_cache)))
            _cache[key] = artifact
        return artifact


def _signature(path: Path, observed: os.stat_result) -> _CacheKey:
    """Everything a stat tells about the file's identity and content version (size, mtime, ctime, inode, device).
    On POSIX ``st_ctime_ns`` cannot be set by ``utime``, so an in-place rewrite that restores the mtime still changes
    the key; on Windows it is the creation time, which leaves that case to write protection of the root."""
    return (str(path), observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns, observed.st_ino, observed.st_dev)


def _same_file(path: Path, key: _CacheKey) -> bool:
    """The path still names the opened file, unchanged in size and mtime (Windows reports ``st_ctime`` differently
    through a path and a handle, so it is compared only between handle observations)."""
    current = path.stat()
    return (current.st_size, current.st_mtime_ns, current.st_ino, current.st_dev) == (key[1], key[2], key[4], key[5])


def _hash_handle(handle: BinaryIO, expected_size: int) -> LocalArtifact | None:
    digest = hashlib.sha256()
    size = 0
    head = handle.read(len(GGUF_MAGIC))
    if head != GGUF_MAGIC:
        return None
    digest.update(head)
    size += len(head)
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    if size != expected_size:
        return None
    layer = "sha256:" + digest.hexdigest()
    return LocalArtifact(manifest_digest=manifest_digest_for(layer, size), layer_digest=layer, size_bytes=size)
