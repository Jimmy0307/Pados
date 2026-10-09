# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise source identity and lineage.

Physical identity reuses ``pados.knowledge.identity`` (content hash only; the
location is lineage, never identity). Every derived object keeps its source
lineage, and deletion/revocation is planned as a deterministic dry run: this
module never deletes anything and never touches model weights.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pados.enterprise.classification import DataClassification
from pados.knowledge.identity import SourceIdentity, source_identity_from_bytes

_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class ProcessingVersions:
    parser_version: str
    chunking_version: str | None = None
    embedding_version: str | None = None


@dataclass(frozen=True)
class EnterpriseSource:
    """An ingested enterprise source bound to one tenant and one PADOS project."""

    identity: SourceIdentity
    source_type: str
    location_ref: str
    ingested_at: str
    classification: DataClassification
    tenant_id: str
    project_id: str
    versions: ProcessingVersions
    allowed_principals: frozenset[str] = frozenset()
    allowed_roles: frozenset[str] = frozenset()
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _TENANT.fullmatch(self.tenant_id) or not _TENANT.fullmatch(self.project_id):
            raise ValueError("SOURCE_BINDING_INVALID")
        if not self.source_type or not self.location_ref:
            raise ValueError("SOURCE_METADATA_REQUIRED")

    @property
    def source_id(self) -> str:
        return self.identity.source_id

    def lineage(self) -> dict[str, Any]:
        """Content-free lineage record (no text, no ACL principal names)."""
        return {
            "source_id": self.source_id,
            "content_hash": self.identity.content_hash,
            "byte_size": self.identity.byte_size,
            "source_type": self.source_type,
            "location_ref": self.location_ref,
            "ingested_at": self.ingested_at,
            "classification": self.classification.value,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "parser_version": self.versions.parser_version,
            "chunking_version": self.versions.chunking_version,
            "embedding_version": self.versions.embedding_version,
            "provenance": list(self.provenance),
        }


def register_source(
    content: bytes, *, source_type: str, location_ref: str, classification: DataClassification,
    tenant_id: str, project_id: str, parser_version: str,
    allowed_principals: Iterable[str] = (), allowed_roles: Iterable[str] = (),
    provenance: Iterable[str] = (), ingested_at: str | None = None,
) -> EnterpriseSource:
    return EnterpriseSource(
        identity=source_identity_from_bytes(content),
        source_type=source_type,
        location_ref=location_ref,
        ingested_at=ingested_at or datetime.now(UTC).isoformat(),
        classification=classification,
        tenant_id=tenant_id,
        project_id=project_id,
        versions=ProcessingVersions(parser_version=parser_version),
        allowed_principals=frozenset(allowed_principals),
        allowed_roles=frozenset(allowed_roles),
        provenance=tuple(provenance),
    )


def derived_id(kind: str, parent_id: str, *parts: object) -> str:
    """Deterministic identifier of a derived object, bound to its parent."""
    payload = "\n".join([kind, parent_id, *(str(part) for part in parts)]).encode("utf-8")
    return f"{kind.lower()}:{hashlib.sha256(payload).hexdigest()[:32]}"


class LineageKind(StrEnum):
    SOURCE = "SOURCE"
    NORMALIZED_ARTIFACT = "NORMALIZED_ARTIFACT"
    CHUNK = "CHUNK"
    EMBEDDING = "EMBEDDING"
    RETRIEVAL_PROJECTION = "RETRIEVAL_PROJECTION"
    GRAPH_PROJECTION = "GRAPH_PROJECTION"
    CACHE = "CACHE"
    EVALUATION_ARTIFACT = "EVALUATION_ARTIFACT"
    DATASET_REFERENCE = "DATASET_REFERENCE"
    TRAINED_ARTIFACT = "TRAINED_ARTIFACT"


_ALLOWED_PARENTS: dict[LineageKind, frozenset[LineageKind]] = {
    LineageKind.SOURCE: frozenset(),
    LineageKind.NORMALIZED_ARTIFACT: frozenset({LineageKind.SOURCE}),
    LineageKind.CHUNK: frozenset({LineageKind.NORMALIZED_ARTIFACT}),
    LineageKind.EMBEDDING: frozenset({LineageKind.CHUNK}),
    LineageKind.RETRIEVAL_PROJECTION: frozenset({LineageKind.CHUNK, LineageKind.EMBEDDING}),
    LineageKind.GRAPH_PROJECTION: frozenset({LineageKind.CHUNK, LineageKind.NORMALIZED_ARTIFACT}),
    LineageKind.CACHE: frozenset({LineageKind.RETRIEVAL_PROJECTION, LineageKind.CHUNK}),
    LineageKind.EVALUATION_ARTIFACT: frozenset({LineageKind.CHUNK, LineageKind.RETRIEVAL_PROJECTION}),
    LineageKind.DATASET_REFERENCE: frozenset({LineageKind.CHUNK, LineageKind.EVALUATION_ARTIFACT}),
    LineageKind.TRAINED_ARTIFACT: frozenset({LineageKind.DATASET_REFERENCE}),
}
# Removing these is never automated: a human decides retraining/retirement.
HUMAN_REVIEW_KINDS = frozenset({LineageKind.DATASET_REFERENCE, LineageKind.TRAINED_ARTIFACT})


@dataclass
class LineageGraph:
    nodes: dict[str, LineageKind] = field(default_factory=dict)
    children: dict[str, set[str]] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)

    def add(self, node_id: str, kind: LineageKind, parents: Iterable[str] = ()) -> str:
        parent_ids = tuple(parents)
        if kind is LineageKind.SOURCE and parent_ids:
            raise ValueError("SOURCE_HAS_NO_PARENT")
        if kind is not LineageKind.SOURCE and not parent_ids:
            raise ValueError("DERIVED_OBJECT_REQUIRES_LINEAGE")
        for parent in parent_ids:
            if self.nodes.get(parent) not in _ALLOWED_PARENTS[kind]:
                raise ValueError("LINEAGE_PARENT_INVALID")
        existing = self.nodes.get(node_id)
        if existing is not None and existing is not kind:
            raise ValueError("LINEAGE_NODE_KIND_CONFLICT")
        self.nodes[node_id] = kind
        self.children.setdefault(node_id, set())
        for parent in parent_ids:
            self.children[parent].add(node_id)
        return node_id

    def descendants(self, node_id: str) -> list[str]:
        if node_id not in self.nodes:
            raise KeyError("LINEAGE_NODE_UNKNOWN")
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for child in self.children.get(current, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return sorted(seen)

    def plan_revocation(self, source_id: str, *, reason: str) -> dict[str, Any]:
        """Dry-run plan for a delete/revoke event. Nothing is changed."""
        if self.nodes.get(source_id) is not LineageKind.SOURCE:
            raise KeyError("SOURCE_UNKNOWN")
        affected = self.descendants(source_id)
        by_kind: dict[str, list[str]] = {kind.value: [] for kind in LineageKind if kind is not LineageKind.SOURCE}
        for node in affected:
            by_kind[self.nodes[node].value].append(node)
        review = sorted(node for node in affected if self.nodes[node] in HUMAN_REVIEW_KINDS)
        automatic = sorted(node for node in affected if self.nodes[node] not in HUMAN_REVIEW_KINDS)
        return {
            "dry_run": True,
            "source_id": source_id,
            "reason": reason,
            "affected": by_kind,
            "affected_count": len(affected),
            "invalidate_automatically": automatic,
            "human_review_required": bool(review),
            "human_review_items": review,
            "model_weights_deleted": False,
            "plan_hash": hashlib.sha256(
                ("\n".join([source_id, *affected])).encode("utf-8")).hexdigest(),
        }

    def apply_revocation(self, plan: dict[str, Any]) -> list[str]:
        """Mark the automatic projections revoked so retrieval excludes them.

        Human-review items are left untouched; this is a projection mask, not
        deletion, and it is idempotent for the same plan.
        """
        if plan.get("dry_run") is not True or plan.get("source_id") not in self.nodes:
            raise ValueError("REVOCATION_PLAN_INVALID")
        marked = [plan["source_id"], *plan["invalidate_automatically"]]
        self.revoked.update(marked)
        return sorted(marked)

    def is_revoked(self, node_id: str) -> bool:
        return node_id in self.revoked
