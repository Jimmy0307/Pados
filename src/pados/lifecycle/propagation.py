# SPDX-License-Identifier: AGPL-3.0-only
"""Deletion / revoke propagation over the existing lineage graph.

This module does not own a graph: it builds on
``pados.enterprise.lineage.LineageGraph.plan_revocation`` and turns its dry-run
plan into an explicit, staged propagation plan that walks
source -> projection -> chunk -> embedding -> cache -> derived artifact, with a
status for every lineage kind. Caches derived from the source are invalidated;
dataset references and trained artifacts are HUMAN_REVIEW_REQUIRED and model
weight deletion is explicitly unsupported (a retraining decision is a human
decision). Authority metadata is retained: the source identity hash and the
audit chain stay, and the source payload ends as
``METADATA_RETAINED_PAYLOAD_PURGED``. Applying the plan delegates to
``LineageGraph.apply_revocation`` (a projection mask), only with the exact hash.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from pados.enterprise.lineage import HUMAN_REVIEW_KINDS, LineageGraph, LineageKind
from pados.execution.context import canonical_hash
from pados.lifecycle.retention import DataKind, PayloadState, policy_for

MODEL_WEIGHT_DELETION = "UNSUPPORTED_REQUIRES_RETRAINING_DECISION"
PLAN_SCHEMA = "pados.lifecycle.propagation-plan.v1"


class Stage(StrEnum):
    SOURCE = "SOURCE"
    PROJECTION = "PROJECTION"
    CHUNK = "CHUNK"
    EMBEDDING = "EMBEDDING"
    CACHE = "CACHE"
    DERIVED_ARTIFACT = "DERIVED_ARTIFACT"
    TRAINING_LINEAGE = "TRAINING_LINEAGE"


class StageStatus(StrEnum):
    PAYLOAD_PURGE_PLANNED = "PAYLOAD_PURGE_PLANNED"
    INVALIDATION_PLANNED = "INVALIDATION_PLANNED"
    CACHE_INVALIDATION_PLANNED = "CACHE_INVALIDATION_PLANNED"
    NOTHING_TO_PROPAGATE = "NOTHING_TO_PROPAGATE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


# Walk order (discovery). Execution runs in reverse so no derived path keeps
# serving revoked data while upstream objects are being removed.
STAGES: tuple[tuple[Stage, tuple[LineageKind, ...]], ...] = (
    (Stage.SOURCE, (LineageKind.SOURCE,)),
    (Stage.PROJECTION, (LineageKind.NORMALIZED_ARTIFACT, LineageKind.RETRIEVAL_PROJECTION,
                        LineageKind.GRAPH_PROJECTION)),
    (Stage.CHUNK, (LineageKind.CHUNK,)),
    (Stage.EMBEDDING, (LineageKind.EMBEDDING,)),
    (Stage.CACHE, (LineageKind.CACHE,)),
    (Stage.DERIVED_ARTIFACT, (LineageKind.EVALUATION_ARTIFACT,)),
    (Stage.TRAINING_LINEAGE, (LineageKind.DATASET_REFERENCE, LineageKind.TRAINED_ARTIFACT)),
)
DERIVED_CACHE_KINDS = (LineageKind.CACHE, LineageKind.RETRIEVAL_PROJECTION, LineageKind.GRAPH_PROJECTION)
RETAINED_METADATA = (DataKind.SOURCE_IDENTITY, DataKind.AUDIT_CHAIN, DataKind.ARTIFACT_HASH)


@dataclass(frozen=True)
class StagePlan:
    stage: Stage
    kinds: tuple[LineageKind, ...]
    status: StageStatus
    node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.value, "kinds": [k.value for k in self.kinds], "status": self.status.value,
                "node_ids": list(self.node_ids), "count": len(self.node_ids)}


@dataclass(frozen=True)
class PropagationPlan:
    source_id: str
    reason: str
    stages: tuple[StagePlan, ...]
    kind_status: tuple[tuple[LineageKind, StageStatus], ...]
    revocation_plan: dict[str, Any]  # LineageGraph.plan_revocation output, unchanged
    node_kinds: tuple[tuple[str, LineageKind], ...]
    plan_hash: str

    def status_of(self, kind: LineageKind) -> StageStatus:
        return dict(self.kind_status)[kind]

    def stage(self, stage: Stage) -> StagePlan:
        return next(s for s in self.stages if s.stage is stage)

    def body(self) -> dict[str, Any]:
        cache_ids = sorted(node for node, kind in self.node_kinds if kind in DERIVED_CACHE_KINDS)
        review = list(self.revocation_plan["human_review_items"])
        return {
            "schema": PLAN_SCHEMA,
            "dry_run": True,
            "autonomous_deletion": False,
            "source_id": self.source_id,
            "reason": self.reason,
            "stages": [s.to_dict() for s in self.stages],
            "execution_order": [s.stage.value for s in reversed(self.stages)],
            "kind_status": {k.value: v.value for k, v in self.kind_status},
            "cache_invalidation": {"node_ids": cache_ids, "kinds": [k.value for k in DERIVED_CACHE_KINDS],
                                   "action": "INVALIDATE"},
            "retained_metadata": {
                "kinds": [k.value for k in RETAINED_METADATA],
                "retention": sorted({policy_for(k).retention.value for k in RETAINED_METADATA}),
                "source_identity_hash": self.source_id,
                "source_payload_state": PayloadState.METADATA_RETAINED_PAYLOAD_PURGED.value,
            },
            "human_review_required": bool(review),
            "human_review_items": review,
            "model_weights_deleted": False,
            "model_weight_deletion": MODEL_WEIGHT_DELETION,
            "revocation_plan_hash": self.revocation_plan["plan_hash"],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "plan_hash": self.plan_hash}


def _stage_status(stage: Stage, node_ids: list[str]) -> StageStatus:
    if stage is Stage.SOURCE:
        return StageStatus.PAYLOAD_PURGE_PLANNED
    if stage is Stage.TRAINING_LINEAGE:
        return StageStatus.HUMAN_REVIEW_REQUIRED if node_ids else StageStatus.NOTHING_TO_PROPAGATE
    if not node_ids:
        return StageStatus.NOTHING_TO_PROPAGATE
    return StageStatus.CACHE_INVALIDATION_PLANNED if stage is Stage.CACHE else StageStatus.INVALIDATION_PLANNED


def _kind_status(kind: LineageKind, node_ids: list[str]) -> StageStatus:
    if kind is LineageKind.SOURCE:
        return StageStatus.PAYLOAD_PURGE_PLANNED
    if kind in HUMAN_REVIEW_KINDS:
        # Always human review, even when no node exists yet: weights are never auto-deleted.
        return StageStatus.HUMAN_REVIEW_REQUIRED
    if not node_ids:
        return StageStatus.NOTHING_TO_PROPAGATE
    return StageStatus.CACHE_INVALIDATION_PLANNED if kind in DERIVED_CACHE_KINDS else StageStatus.INVALIDATION_PLANNED


def plan_propagation(graph: LineageGraph, source_id: str, *, reason: str) -> PropagationPlan:
    """Dry-run propagation plan for a source delete/revoke. Nothing is changed."""
    base = graph.plan_revocation(source_id, reason=reason)
    affected: dict[str, list[str]] = {kind: list(ids) for kind, ids in base["affected"].items()}
    affected[LineageKind.SOURCE.value] = [source_id]
    node_kinds = tuple(sorted((node, LineageKind(kind)) for kind, ids in affected.items() for node in ids))
    stages = tuple(
        StagePlan(stage, kinds, _stage_status(stage, ids), tuple(ids))
        for stage, kinds in STAGES
        for ids in [sorted(n for k in kinds for n in affected.get(k.value, []))]
    )
    kind_status = tuple((kind, _kind_status(kind, affected.get(kind.value, []))) for kind in LineageKind)
    draft = PropagationPlan(source_id, reason, stages, kind_status, base, node_kinds, "")
    return replace(draft, plan_hash=canonical_hash(draft.body()))


def apply_propagation(graph: LineageGraph, plan: PropagationPlan, *, approved_plan_hash: str) -> list[str]:
    """Mask the automatic projections via ``LineageGraph.apply_revocation`` (exact hash only).

    Human-review items (dataset references, trained artifacts) are left untouched
    and no model weight is deleted.
    """
    if not isinstance(approved_plan_hash, str) or not hmac.compare_digest(
            approved_plan_hash.encode("utf-8"), plan.plan_hash.encode("utf-8")):
        raise PermissionError("PROPAGATION_PLAN_HASH_NOT_APPROVED")
    current = plan_propagation(graph, plan.source_id, reason=plan.reason)
    if current.plan_hash != plan.plan_hash:
        raise ValueError("PROPAGATION_PLAN_STALE")
    return graph.apply_revocation(dict(plan.revocation_plan))
