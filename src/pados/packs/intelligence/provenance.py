# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic provenance registration for the pipeline report (CR-16b H-08; operator side, never authority).

``AtomicInputProvenance`` is a bounded, operator-populated implementation of the fabric's ``InputProvenance``
seam (``resolve``) that also offers ``register_many``: insert-if-absent with conflict checking, ALL OR
NOTHING, under one lock. Every entry of a batch is validated (well-formed hash, project and classification;
no conflict with an existing entry or with another entry of the same batch; capacity) before any entry is
inserted, so a refused batch leaves no registration behind. An identical existing entry is accepted
(idempotent); entries never change once registered.

The fabric's ``InMemoryInputProvenance`` has no batch method and is not modified here; the report builder
requires a registrar with ``register_many`` and refuses any other (``REPORT_PROVENANCE_NOT_ATOMIC``). The
lock is a plain ``threading.Lock``; this module starts no thread.
"""
from __future__ import annotations

import re
import threading
from itertools import islice
from collections.abc import Iterable, Mapping, Sequence

from pados.capability.provenance import MAX_PROVENANCE_ENTRIES, ArtifactProvenance, ProvenanceRefused
from pados.enterprise.classification import DataClassification

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_BATCH = 4096

Registration = tuple[str, str, DataClassification]  # (sha256, project_id, classification)


def _entry(item: object) -> tuple[str, ArtifactProvenance]:
    if not (isinstance(item, tuple) and len(item) == 3):
        raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_INVALID")
    sha256, project_id, classification = item
    if not (isinstance(sha256, str) and _SHA256.fullmatch(sha256) and isinstance(project_id, str)
            and _PROJECT.fullmatch(project_id) and isinstance(classification, DataClassification)):
        raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_INVALID")
    return sha256, ArtifactProvenance(project_id, classification)


class AtomicInputProvenance:
    """``InputProvenance`` with all-or-nothing batch registration (``register_many``)."""

    __slots__ = ("_entries", "_capacity", "_lock")

    def __init__(self, entries: Mapping[str, tuple[str, DataClassification]] | None = None, *,
                 capacity: int = MAX_PROVENANCE_ENTRIES) -> None:
        if type(capacity) is not int or not 1 <= capacity <= MAX_PROVENANCE_ENTRIES:
            raise ValueError("INPUT_PROVENANCE_CAPACITY_INVALID")
        self._entries: dict[str, ArtifactProvenance] = {}
        self._capacity = capacity
        self._lock = threading.Lock()
        self.register_many([(sha256, project_id, classification)
                            for sha256, (project_id, classification) in (entries or {}).items()])

    def resolve(self, sha256: str) -> ArtifactProvenance | None:
        with self._lock:
            return self._entries.get(sha256)

    def register(self, sha256: str, *, project_id: str, classification: DataClassification) -> None:
        self.register_many([(sha256, project_id, classification)])

    def register_many(self, entries: Iterable[Registration]) -> int:
        """Insert every absent entry, or none: any invalid or conflicting entry refuses the whole batch.
        Returns how many entries were newly inserted."""
        # CR-16c H-10: consume at most MAX_BATCH + 1 items, so an oversized or endless iterable is refused
        # before it is materialized.
        batch = list(islice(entries, MAX_BATCH + 1)) if not isinstance(entries, (str, bytes)) else None
        if batch is None or len(batch) > MAX_BATCH:
            raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_INVALID")
        parsed = [_entry(item) for item in batch]
        with self._lock:
            pending = self._pending(parsed)
            if len(self._entries) + len(pending) > self._capacity:
                raise ProvenanceRefused("INPUT_PROVENANCE_FULL")
            self._entries.update(pending)  # every check passed: nothing below can refuse
            return len(pending)

    def _pending(self, parsed: Sequence[tuple[str, ArtifactProvenance]]) -> dict[str, ArtifactProvenance]:
        pending: dict[str, ArtifactProvenance] = {}
        for sha256, entry in parsed:
            existing = self._entries.get(sha256, pending.get(sha256))
            if existing is not None and existing != entry:
                raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_CONFLICT")
            if sha256 not in self._entries:
                pending[sha256] = entry
        return pending


__all__ = ["MAX_BATCH", "AtomicInputProvenance", "Registration"]
