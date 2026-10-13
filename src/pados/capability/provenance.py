# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted input provenance: the project and classification an input artifact REALLY has.

A ``TaskInput`` carries caller-supplied ``project_id`` / ``classification`` labels; labels are claims, not
facts. ``InputProvenance`` is the seam an operator or integrator wires to the governed source of truth
(an ingestion registry, lineage store, ...). Every task input is resolved by content hash: an unknown
artifact is refused, the resolved project must equal the task project, the resolved classification must
not exceed the task ceiling (policy additionally bounds the task by capability and adapter), and a label
that differs from the resolved value is refused (it is never trusted, in either direction). The planner
checks this at planning time and the executor again immediately before each stage invocation; neither
runs without a resolver (fail closed). This module reads nothing and writes nothing on its own.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import NamedTuple, Protocol

from pados.capability.task import TaskContract, TaskInput
from pados.enterprise.classification import DataClassification

MAX_PROVENANCE_ENTRIES = 100_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class ArtifactProvenance(NamedTuple):
    project_id: str
    classification: DataClassification


class InputProvenance(Protocol):
    def resolve(self, sha256: str) -> ArtifactProvenance | tuple[str, DataClassification] | None:
        """The governed (project_id, classification) of one artifact, or ``None`` when unknown."""


class ProvenanceRefused(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InMemoryInputProvenance:
    """A bounded, operator-populated resolver (tests and small integrations). Entries never change."""

    def __init__(self, entries: Mapping[str, tuple[str, DataClassification]] | None = None) -> None:
        self._entries: dict[str, ArtifactProvenance] = {}
        for sha256, (project_id, classification) in (entries or {}).items():
            self.register(sha256, project_id=project_id, classification=classification)

    def register(self, sha256: str, *, project_id: str, classification: DataClassification) -> None:
        if not (isinstance(sha256, str) and _SHA256.fullmatch(sha256) and isinstance(project_id, str)
                and _PROJECT.fullmatch(project_id) and isinstance(classification, DataClassification)):
            raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_INVALID")
        entry = ArtifactProvenance(project_id, classification)
        existing = self._entries.get(sha256)
        if existing is not None:
            if existing != entry:
                raise ProvenanceRefused("INPUT_PROVENANCE_ENTRY_CONFLICT")
            return
        if len(self._entries) >= MAX_PROVENANCE_ENTRIES:
            raise ProvenanceRefused("INPUT_PROVENANCE_FULL")
        self._entries[sha256] = entry

    def resolve(self, sha256: str) -> ArtifactProvenance | None:
        return self._entries.get(sha256)


def resolved_provenance(provenance: InputProvenance | None, sha256: str) -> ArtifactProvenance:
    """Resolve one artifact through the trusted seam; anything but a well-formed answer is refused."""
    if provenance is None:
        raise ProvenanceRefused("INPUT_PROVENANCE_RESOLVER_MISSING")
    try:
        raw = provenance.resolve(sha256)
    except Exception as exc:  # noqa: BLE001 - a resolver that cannot answer is a refusal, never a pass
        raise ProvenanceRefused("INPUT_PROVENANCE_UNAVAILABLE") from exc
    if raw is None:
        raise ProvenanceRefused("INPUT_PROVENANCE_UNKNOWN")
    if not (isinstance(raw, tuple) and len(raw) == 2):
        raise ProvenanceRefused("INPUT_PROVENANCE_INVALID")
    project_id, classification = raw
    if not (isinstance(project_id, str) and _PROJECT.fullmatch(project_id)
            and isinstance(classification, DataClassification)):
        raise ProvenanceRefused("INPUT_PROVENANCE_INVALID")
    return ArtifactProvenance(project_id, classification)


def check_task_input(task: TaskContract, item: TaskInput, provenance: InputProvenance | None) -> ArtifactProvenance:
    resolved = resolved_provenance(provenance, item.ref.sha256)
    if resolved.project_id != task.project_id:
        raise ProvenanceRefused("INPUT_PROVENANCE_PROJECT_MISMATCH")
    if not task.classification.permits(resolved.classification):
        raise ProvenanceRefused("INPUT_CLASSIFICATION_EXCEEDS_TASK")
    if item.project_id != resolved.project_id:
        raise ProvenanceRefused("INPUT_PROJECT_LABEL_MISMATCH")
    if item.classification is not resolved.classification:
        raise ProvenanceRefused("INPUT_CLASSIFICATION_LABEL_MISMATCH")
    return resolved


def check_task_inputs(task: TaskContract, provenance: InputProvenance | None) -> None:
    """Every task input must resolve, belong to the task project and fit the task classification."""
    for item in task.inputs:
        check_task_input(task, item, provenance)
