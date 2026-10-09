# SPDX-License-Identifier: AGPL-3.0-only
"""Storage Flexibility V1+: persistence of the placement index (a registered, content-free persistence point).

The document holds digests, sizes, artifact classes, tiers, backend ids and safe labels only. It is registered
as ``storage_placement_index`` in ``pados.lifecycle.inventory`` (``DataKind.STORAGE_PLACEMENT_INDEX``: R2,
human decision only, never age-expired, no authority). Writes use the existing operational-store helpers
(``atomic_write_json`` under ``store_lock``: owner-only temporary file, fsync, atomic replace), after
``assert_minimized`` with an explicit schema. The document carries ``index_digest``, the canonical SHA-256 of
its body. A load validates everything or refuses: wrong schema, digest mismatch (``STORAGE_INDEX_TAMPERED``),
unexpected fields, an invalid entry or a duplicate digest.

The embedded digest detects accidental or naive tampering only. A caller that needs tamper evidence against
someone able to rewrite the file keeps the digest returned by ``save`` elsewhere and passes it as
``expected_digest``. The index is placement bookkeeping: it is not semantic truth (that stays with
``StateProofService.commit_result``) and losing it never loses bytes, which keep their content address.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from pados.connectivity.storage import atomic_write_json, store_lock
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.storage_flexibility import ArtifactEntry, PlacementIndex

INDEX_SCHEMA = "pados.lifecycle.placement-index.v1"
MAX_INDEX_ENTRIES = 65_536
MAX_INDEX_BYTES = 64 * 1024 * 1024
_TOP_KEYS = ("schema_version", "entries", "index_digest")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PlacementIndexError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_label(value: str) -> bool:
    return bool(_LABEL.fullmatch(value)) and ".." not in value and "?" not in value \
        and "@" not in value.split("/")[0] and "://" not in value


def _body(index: PlacementIndex) -> dict[str, Any]:
    entries = []
    for digest in sorted(index.entries):
        entry = index.entries[digest]
        labels = (*entry.references, *(p.location for p in entry.placements if p.location is not None),
                  *((entry.project_id,) if entry.project_id is not None else ()))
        if not all(_safe_label(label) for label in labels):
            raise PlacementIndexError("STORAGE_INDEX_LABEL_NOT_SAFE")  # references are labels, never content
        entries.append(entry.model_dump(mode="json"))
    if len(entries) > MAX_INDEX_ENTRIES:
        raise PlacementIndexError("STORAGE_INDEX_TOO_LARGE")
    return {"schema_version": INDEX_SCHEMA, "entries": entries}


def index_digest(index: PlacementIndex) -> str:
    """The canonical SHA-256 of the persisted body (what ``save`` returns and ``load`` verifies)."""
    return canonical_hash(_body(index))


def index_document(index: PlacementIndex) -> dict[str, Any]:
    body = _body(index)
    document = {**body, "index_digest": canonical_hash(body)}
    try:
        assert_minimized(document, allowed_keys=_TOP_KEYS, max_string=256, max_items=MAX_INDEX_ENTRIES,
                         max_total_bytes=MAX_INDEX_BYTES)
    except DataMinimizationViolation as exc:
        raise PlacementIndexError(f"STORAGE_INDEX_NOT_MINIMIZED:{exc.code}") from None
    return document


def index_from_document(document: Any, *, expected_digest: str | None = None) -> PlacementIndex:
    """Validate or refuse; never repairs a document."""
    if not isinstance(document, dict) or set(document) != set(_TOP_KEYS):
        raise PlacementIndexError("STORAGE_INDEX_SHAPE_INVALID")
    if document["schema_version"] != INDEX_SCHEMA:
        raise PlacementIndexError("STORAGE_INDEX_SCHEMA_UNSUPPORTED")
    recorded = document["index_digest"]
    if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
        raise PlacementIndexError("STORAGE_INDEX_DIGEST_INVALID")
    body = {"schema_version": document["schema_version"], "entries": document["entries"]}
    if canonical_hash(body) != recorded:
        raise PlacementIndexError("STORAGE_INDEX_TAMPERED")
    if expected_digest is not None and recorded != expected_digest:
        raise PlacementIndexError("STORAGE_INDEX_NOT_THE_EXPECTED_VERSION")
    try:
        assert_minimized(document, allowed_keys=_TOP_KEYS, max_string=256, max_items=MAX_INDEX_ENTRIES,
                         max_total_bytes=MAX_INDEX_BYTES)
        if not isinstance(document["entries"], list):
            raise ValueError("STORAGE_INDEX_ENTRIES_INVALID")
        entries = [ArtifactEntry.model_validate(item) for item in document["entries"]]
    except ValueError as exc:  # includes pydantic ValidationError and DataMinimizationViolation
        raise PlacementIndexError("STORAGE_INDEX_ENTRY_INVALID") from exc
    if len({e.digest for e in entries}) != len(entries):
        raise PlacementIndexError("STORAGE_INDEX_DUPLICATE_DIGEST")
    index = PlacementIndex(entries={e.digest: e for e in entries})
    if index_digest(index) != recorded:  # canonical round trip: ordering or defaults were not as written
        raise PlacementIndexError("STORAGE_INDEX_NOT_CANONICAL")
    return index


class PlacementIndexStore:
    """One owner-only JSON file. ``save`` returns the digest; ``load`` validates or refuses."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, *, expected_digest: str | None = None, fresh_store: bool = False) -> PlacementIndex:
        """A missing file is an empty index only when the caller states this is a fresh store (``fresh_store=True``)
        and expects no specific version; otherwise a deleted index is refused (``STORAGE_INDEX_MISSING``, red-team
        F-11). Anything else fails closed."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            if expected_digest is not None or fresh_store is not True:
                raise PlacementIndexError("STORAGE_INDEX_MISSING") from None
            return PlacementIndex()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_INDEX_BYTES:
            raise PlacementIndexError("STORAGE_INDEX_NOT_A_REGULAR_FILE")
        try:
            document = json.loads(self.path.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise PlacementIndexError("STORAGE_INDEX_UNREADABLE") from exc
        return index_from_document(document, expected_digest=expected_digest)

    def save(self, index: PlacementIndex) -> str:
        document = index_document(index)
        with store_lock(self.path):
            atomic_write_json(self.path, document)
        return str(document["index_digest"])
