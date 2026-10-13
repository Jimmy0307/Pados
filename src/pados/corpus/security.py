# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
from pathlib import Path

from .ingestion import MAX_PILOT_DOCUMENTS, sha256_file


class CorpusBoundaryError(ValueError):
    """Raised when a corpus path is outside an approved root."""


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CorpusBoundaryError(f"path does not exist: {path}") from exc


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_approved_root(root: str | os.PathLike[str]) -> Path:
    raw_root = Path(root)
    if not raw_root.is_absolute():
        raise CorpusBoundaryError("approved corpus root must be absolute")
    resolved = _resolved(raw_root)
    if not resolved.is_dir():
        raise CorpusBoundaryError("approved corpus root must be a directory")
    if any(part in {".", ".."} for part in Path(root).parts):
        raise CorpusBoundaryError("relative or traversal corpus roots are not allowed")
    return resolved


def resolve_child_path(approved_root: Path, relative_path: str | os.PathLike[str]) -> Path:
    root = resolve_approved_root(approved_root)
    raw = Path(relative_path)
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise CorpusBoundaryError("path traversal is not allowed")
    source_candidate = root / raw
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise CorpusBoundaryError("symlink/reparse corpus paths are not allowed")
    candidate = _resolved(source_candidate)
    if not _is_within(candidate, root):
        raise CorpusBoundaryError("path escapes approved corpus root")
    return candidate


def discover_pdf_files(
    approved_root: str | os.PathLike[str],
    *,
    max_documents: int = MAX_PILOT_DOCUMENTS,
) -> list[Path]:
    if max_documents < 1 or max_documents > MAX_PILOT_DOCUMENTS:
        raise CorpusBoundaryError(f"pilot document cap must be between 1 and {MAX_PILOT_DOCUMENTS}")
    root = resolve_approved_root(approved_root)
    files: list[Path] = []
    for candidate in sorted(root.rglob("*.pdf"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative = candidate.relative_to(root)
        try:
            safe = resolve_child_path(root, relative)
        except CorpusBoundaryError:
            continue
        if safe.is_file() and not safe.is_symlink():
            files.append(safe)
    return files[:max_documents]


def inventory_pdf_files(approved_root: str | os.PathLike[str]) -> list[dict[str, object]]:
    root = resolve_approved_root(approved_root)
    return [
        {
            "relative_path": path.relative_to(root).as_posix(),
            "file_size": path.stat().st_size,
            "content_hash": sha256_file(path),
        }
        for path in discover_pdf_files(root)
    ]
