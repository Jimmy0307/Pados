# SPDX-License-Identifier: AGPL-3.0-only
"""Seams the planner consumes; implemented by separate packages (no dependency cycle, no new authority).

* ``ResourceInventory`` (Resource-Aware Planning): which hardware/runtime resources exist and whether a
  requirement fits one of them. Descriptive; registration is not authorization.
* ``ScoreAdvisor`` (Outcome Learning): advisory scores for already-feasible, already-authorized adapters.
  ``apply_advice`` is the only way advice reaches a plan: it validates advice, drops anything outside the
  feasible set, rejects duplicates and non-finite values, and returns one bounded adjustment per candidate.
  Advice can never make an infeasible or unauthorized candidate feasible and never changes policy, ACL,
  classification, grants or human-approval requirements.
* ``ModelInferenceDelegate``: model-inference stages are resolved by the existing PADOS Worker Router
  (``pados.worker.router.select_worker``) through a concrete bridge; the fabric records the router's
  selection evidence and never ranks language-model providers itself.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pados.capability.model import AdapterDescriptor, ResourceRequirement
from pados.enterprise.classification import DataClassification

ADVICE_BOUND = 0.25  # one aggregate adjustment per candidate, in [-ADVICE_BOUND, +ADVICE_BOUND]


@dataclass(frozen=True)
class ResourceMatch:
    resource_id: str
    fits: bool
    reasons: tuple[str, ...]


class ResourceInventory(Protocol):
    def match(self, requirement: ResourceRequirement) -> Sequence[ResourceMatch]: ...


@dataclass(frozen=True)
class Advice:
    adapter_id: str
    score: float
    sample_size: int
    explanation: str
    profile_hash: str
    context_hash: str  # binds the advice to the task/profile context it was computed for

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValueError("ADVICE_SCORE_NOT_FINITE")
        if self.sample_size < 0:
            raise ValueError("ADVICE_SAMPLE_SIZE_INVALID")
        if len(self.explanation) > 300:
            raise ValueError("ADVICE_EXPLANATION_TOO_LONG")


class ScoreAdvisor(Protocol):
    """Advice is bound to the exact decision it was computed for: the FEASIBLE candidate set (sorted
    ``(adapter_id, adapter_version)`` pairs), the task classification value and an advice-independent
    ``plan_binding_hash`` supplied by the planner. Advice bound to anything else is ignored."""

    def advise(self, *, task_type: str, capability_id: str, candidates: Sequence[AdapterDescriptor],
               context_features: Mapping[str, str], classification: str,
               plan_binding_hash: str | None = None) -> Sequence[Advice]: ...

    def context_hash(self, *, task_type: str, capability_id: str, context_features: Mapping[str, str],
                     candidates: tuple[tuple[str, str], ...], classification: str,
                     plan_binding_hash: str | None = None) -> str:
        """The hash the planner passes to ``apply_advice``; computed by the advisor (it owns feature validation)."""

    def issued(self, plan_binding_hash: str, *,
               candidates: Sequence[tuple[str, str]]) -> Sequence[Advice] | None:
        """Exactly the advice this advisor issued AND durably recorded for ``plan_binding_hash`` (its latest
        issuance for that binding), ``()`` when it issued an empty set, ``None`` when it issued nothing.
        ``candidates`` are the feasible sorted ``(adapter_id, adapter_version)`` pairs of the binding (an
        advisor that stores only identity digests maps its advice back through them).

        The planner records only this set and reproduction requires it again, so advice the advisor never
        issued (fabricated, or returned but never logged) cannot enter or survive in a plan."""


class AdviceRejected(ValueError):
    pass


def apply_advice(feasible_adapter_ids: Iterable[str], advice: Sequence[Advice], *,
                 context_hash: str) -> dict[str, float]:
    """One bounded adjustment per feasible candidate; everything else is dropped (and never feasible-izing)."""
    feasible = set(feasible_adapter_ids)
    seen: set[str] = set()
    adjustments: dict[str, float] = {}
    for item in advice:
        if item.adapter_id in seen:
            raise AdviceRejected("ADVICE_DUPLICATE_CANDIDATE")
        seen.add(item.adapter_id)
        if item.adapter_id not in feasible or item.context_hash != context_hash:
            continue
        adjustments[item.adapter_id] = max(-ADVICE_BOUND, min(ADVICE_BOUND, item.score))
    return adjustments


@dataclass(frozen=True)
class InferenceRequest:
    project_id: str
    stage_id: str
    capability_id: str
    risk_level: str
    classification: DataClassification
    network_policy: str
    grant_hash: str


@dataclass(frozen=True)
class InferenceSelection:
    selected_worker_id: str | None
    status: str
    explanation: str
    selection_hash: str  # canonical hash of the router's decision (considered candidates, reasons)


class ModelInferenceDelegate(Protocol):
    def select(self, request: InferenceRequest) -> InferenceSelection: ...
