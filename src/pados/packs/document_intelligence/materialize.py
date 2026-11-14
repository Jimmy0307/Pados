# SPDX-License-Identifier: AGPL-3.0-only
"""DI-2 hardening: load exactly the bytes that were verified.

``verify_model_dir`` hashes the operator's files and the engine later reopens them by path, so anyone able to write
the operator directory in between could swap a file. ``materialize_verified`` closes that window: it copies every
pinned file into a fresh directory that this process creates (``mkdtemp``: owner-only on POSIX, inside the user's
own temp directory on Windows), hashing the bytes AS THEY ARE WRITTEN, and refuses unless each written file has the
pinned size and SHA-256. The copy is then made read-only and is the only directory an engine is given. Source files
are opened without following a final symlink where the platform allows (``O_NOFOLLOW``) and must be regular files.

The copy costs disk space and one read of the model per load (about 160 MB for the two built-in pins). It is used
by the subprocess worker (``model_worker``) and, opt-in, by the in-process engines (``private_copy=True``).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from pados.packs.document_intelligence.model_pin import ModelPin

_CHUNK = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class MaterializeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _copy_hashed(source: Path, target: Path, limit: int) -> tuple[int, str]:
    descriptor = os.open(source, os.O_RDONLY | _NOFOLLOW | _BINARY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MaterializeError("MODEL_FILE_INVALID")
        digest, size = hashlib.sha256(), 0
        target.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(descriptor, "rb", closefd=False) as reader, open(target, "xb") as writer:
            while chunk := reader.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise MaterializeError("MODEL_FILE_SIZE_MISMATCH")
                digest.update(chunk)  # the hash covers exactly the bytes written to the private copy
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        os.close(descriptor)
    return size, digest.hexdigest()


def materialize_verified(pin: ModelPin, source: Path, *, parent: Path | None = None) -> Path:
    """A fresh, private, read-only copy of the pinned files whose written bytes match the pin, or a closed refusal.
    The caller owns the returned directory and removes it with ``discard``."""
    target = Path(tempfile.mkdtemp(prefix=f"pados-model-{pin.pin_id}-", dir=parent))
    try:
        for item in pin.files:
            relative = PurePosixPath(item.path)
            origin = source.joinpath(*relative.parts)
            if any(part.is_symlink() for part in (origin, *origin.parents) if source in (part, *part.parents)
                   and part != source):
                raise MaterializeError("MODEL_FILE_SYMLINK")
            try:
                size, sha256 = _copy_hashed(origin, target.joinpath(*relative.parts), item.size)
            except FileNotFoundError:
                raise MaterializeError("MODEL_FILE_MISSING") from None
            except OSError:
                raise MaterializeError("MODEL_FILE_INVALID") from None  # a symlink refused by O_NOFOLLOW, etc.
            if size != item.size:
                raise MaterializeError("MODEL_FILE_SIZE_MISMATCH")
            if sha256 != item.sha256:
                raise MaterializeError("MODEL_FILE_DIGEST_MISMATCH")
        for path in target.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IRUSR)
    except BaseException:
        discard(target)
        raise
    return target


def discard(directory: Path) -> None:
    """Remove a materialized copy (read-only files included); never raises."""
    def writable(function: object, path: str, _info: object) -> None:
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        os.unlink(path)

    shutil.rmtree(directory, onerror=writable)  # noqa: S112 - best effort cleanup of our own temp copy
