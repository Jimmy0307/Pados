# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded Markdown documents inside a project folder, for the Control Center ("add a Markdown file").

The only files this module writes are ``.md`` files strictly inside the root of a project whose root lies inside the
approved workspace root (``PADOS_PRODUCT_WORKSPACE_ROOT``): never a project registered elsewhere by the CLI, never a
path outside the root, never through a symbolic link or junction. A name is 1-3 safe segments ending in ``.md``; the
content is UTF-8, at most 256 KiB, without NUL and without secret-like text. An existing file is replaced only when
the caller says ``replace`` and names the SHA-256 it read (optimistic concurrency); the write is atomic (a temporary
file in the same folder, then ``os.replace``). Every write is audited as a project sync event by the route.

Reading lists ``.md`` files (bounded walk, links never followed) and returns one file's text; reads work for any
registered project root, writes only inside the workspace.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pados.control.product_errors import ProductControlError
from pados.memory.security import contains_secret

DOCUMENT_MAX_BYTES = 256 * 1024
LIST_MAX_ENTRIES = 200
LIST_MAX_DEPTH = 4
HASH_MAX_BYTES = 1024 * 1024
MAX_SEGMENTS = 3
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProjectRoot:
    path: Path
    writable: bool


def validate_document_path(raw: str) -> PurePosixPath:
    """A relative name of 1-3 safe segments ending in ``.md`` (``notes.md``, ``docs/guide.md``)."""
    value = str(raw or "").strip()
    if not value or len(value) > 200 or "\\" in value or "\x00" in value or value.startswith("/"):
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    parts = value.split("/")
    if len(parts) > MAX_SEGMENTS:
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    for part in parts:
        stem = part.split(".", 1)[0].lower()
        if not _SEGMENT.fullmatch(part) or ".." in part or part.endswith(".") or stem in _WINDOWS_RESERVED:
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    if not parts[-1].endswith(".md") or len(parts[-1]) <= 3:
        raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
    return PurePosixPath(*parts)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def resolve_project_root(root_path: str, workspace_root: str) -> ProjectRoot:
    """The project's registered root; writable only when it lies strictly inside the approved workspace root."""
    try:
        root = Path(root_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductControlError("PROJECT_ROOT_UNAVAILABLE", 409) from exc
    if not root.is_dir():
        raise ProductControlError("PROJECT_ROOT_UNAVAILABLE", 409)
    writable = False
    if workspace_root and Path(workspace_root).expanduser().is_absolute():
        workspace = Path(workspace_root).expanduser().resolve()
        writable = workspace in root.parents
    return ProjectRoot(path=root, writable=writable)


def _target(root: Path, relative: PurePosixPath) -> Path:
    """The file under ``root``; no existing component may be a link, and the result stays strictly inside."""
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise ProductControlError("DOCUMENT_PATH_OUTSIDE_PROJECT", 403)
    resolved = current.resolve()
    if root not in resolved.parents:
        raise ProductControlError("DOCUMENT_PATH_OUTSIDE_PROJECT", 403)
    return resolved


def encode_content(content: str) -> bytes:
    """UTF-8 bytes of a document, refused when too large, not encodable, containing NUL or secret-like text."""
    if "\x00" in content:
        raise ProductControlError("DOCUMENT_CONTENT_INVALID", 422)
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError as exc:  # a lone surrogate from JSON
        raise ProductControlError("DOCUMENT_ENCODING_INVALID", 422) from exc
    if len(data) > DOCUMENT_MAX_BYTES:
        raise ProductControlError("DOCUMENT_TOO_LARGE", 413)
    if contains_secret(content):
        raise ProductControlError("DOCUMENT_CONTENT_REJECTED", 422)
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(target: Path, data: bytes) -> None:
    """Write through a new temporary file in the same folder, then replace: readers see old or new, never partial."""
    temporary = target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_regular(path: Path, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes of a regular, unlinked file; ``None`` when it is not one or is larger."""
    if _is_link(path):
        return None
    try:
        status = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
        return None
    data = path.read_bytes()
    return None if len(data) > limit else data


def write_document(root: ProjectRoot, relative: PurePosixPath, data: bytes, *, replace: bool,
                   expected_sha256: str | None) -> dict[str, Any]:
    """Create or (with ``replace`` and the current SHA-256) replace one Markdown file; returns its receipt."""
    if not root.writable:
        raise ProductControlError("PROJECT_ROOT_OUTSIDE_WORKSPACE", 403)
    target = _target(root.path, relative)
    previous: str | None = None
    if target.exists():
        if not target.is_file():
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
        if not replace:
            raise ProductControlError("DOCUMENT_EXISTS", 409)
        current = _read_regular(target, HASH_MAX_BYTES)
        if current is None:
            raise ProductControlError("DOCUMENT_PATH_INVALID", 422)
        previous = sha256_hex(current)
        if not expected_sha256 or expected_sha256.lower() != previous:
            raise ProductControlError("DOCUMENT_CHANGED", 409)
    elif replace:
        raise ProductControlError("DOCUMENT_NOT_FOUND", 404)
    parent = target.parent
    if parent != root.path:
        parent.mkdir(parents=True, exist_ok=True)
        _target(root.path, relative)  # re-check: a folder created meanwhile must not be a link
    atomic_write(target, data)
    return {"path": relative.as_posix(), "size": len(data), "sha256": sha256_hex(data), "replaced": previous is not None,
            "previous_sha256": previous}


def read_document(root: ProjectRoot, relative: PurePosixPath) -> dict[str, Any]:
    target = _target(root.path, relative)
    if not target.exists():
        raise ProductControlError("DOCUMENT_NOT_FOUND", 404)
    data = _read_regular(target, DOCUMENT_MAX_BYTES)
    if data is None:
        raise ProductControlError("DOCUMENT_TOO_LARGE", 413)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductControlError("DOCUMENT_ENCODING_INVALID", 422) from exc
    return {"path": relative.as_posix(), "size": len(data), "sha256": sha256_hex(data), "content": content}


def list_documents(root: ProjectRoot) -> dict[str, Any]:
    """The project's ``.md`` files (bounded, sorted, links never followed) with size, SHA-256 and change time."""
    found: list[dict[str, Any]] = []
    truncated = False
    for directory, subdirs, files in os.walk(root.path):
        base = Path(directory)
        depth = len(base.relative_to(root.path).parts)
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")
                            and depth + 1 < LIST_MAX_DEPTH and not _is_link(base / d))
        for name in sorted(files):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            if len(found) >= LIST_MAX_ENTRIES:
                truncated = True
                break
            path = base / name
            data = _read_regular(path, HASH_MAX_BYTES)
            try:
                status = path.stat()
            except OSError:
                continue
            found.append({"path": path.relative_to(root.path).as_posix(), "size": status.st_size,
                          "sha256": sha256_hex(data) if data is not None else None,
                          "modified_at": datetime.fromtimestamp(status.st_mtime, UTC).isoformat()})
        if truncated:
            break
    return {"documents": found, "truncated": truncated, "writable": root.writable}


def expected_sha(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _SHA256.fullmatch(value.lower()):
        raise ProductControlError("DOCUMENT_SHA256_INVALID", 422)
    return value.lower()
