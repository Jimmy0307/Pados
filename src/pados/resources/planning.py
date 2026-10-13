# SPDX-License-Identifier: AGPL-3.0-only
"""Resource-aware planning: task -> capability -> requirement -> feasible (runtime, model, resource) tuples.

The planner talks to hardware only through the Capability Fabric's ``ResourceInventory`` seam
(``match(requirement)``), so any inventory implementation can back it. Output is explained feasibility,
not a decision: tuples are listed in a fixed, documented order (resource_id, model_id, runtime declaration
order) with no scores. Choosing among them stays with existing authorities — policy
(``pados.capability.policy.evaluate`` with ``resource_fit``), the Worker Router for model inference, and the
receipt -> validation -> CAS path for acceptance. ``selection_authority`` is always ``NONE``.

Model-level filters (role, modality, context length, tool use, structured output, classification ceiling,
network ceiling) only exclude; they never authorize, and an excluded model is reported with its reasons.

Measured capabilities: a task may state a ``CapabilityRequirement`` (capability floors, context, latency,
throughput, concurrency). Then only models whose catalog description carries a REGISTERED measured profile are
considered, and every unmet, unmeasured or invalid measurement excludes the model with its reason. The filter
reads capabilities and numbers only (never a model, vendor or runtime name); without a requirement planning is
unchanged.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pados.capability.model import (
    CapabilityDescriptor,
    CapabilityFamily,
    ResourceRequirement,
    descriptor_digest,
)
from pados.capability.policy import network_permitted
from pados.capability.seams import ResourceInventory
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized
from pados.resources.hardware import RUNTIME_ORDER, AcceleratorRuntime
from pados.resources.inventory import requirement_for
from pados.resources.capability_profile import CapabilityRequirement, ModelCapabilityProfile, evidence_reasons
from pados.resources.models import ModelCatalog, ModelRole, ModelRuntimeProfile

PLAN_SCHEMA = "pados.resources.plan.v1"
MAX_EVIDENCE_CANDIDATES = 64
MAX_PLAN_CANDIDATES = 4096
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class PlanningTask:
    """What one stage needs. ``role`` set -> a model is required; unset -> the capability runs on its own."""

    task_id: str
    capability: CapabilityDescriptor
    classification: DataClassification
    network_policy: NetworkRequirement  # the most permissive network use the task allows
    role: ModelRole | None = None
    min_context_length: int = 0
    requires_tool_use: bool = False
    requires_structured_output: bool = False
    capability_requirement: CapabilityRequirement | None = None  # measured-capability floors (opt-in)

    def __post_init__(self) -> None:
        if not _TASK_ID.fullmatch(self.task_id):
            raise ValueError("PLANNING_TASK_ID_INVALID")
        if not 0 <= self.min_context_length <= 10_000_000:
            raise ValueError("PLANNING_CONTEXT_LENGTH_INVALID")
        if self.role is None and self.capability.family is CapabilityFamily.MODEL_RUNTIME:
            raise ValueError("PLANNING_MODEL_ROLE_REQUIRED")  # model inference never plans without a model


@dataclass(frozen=True)
class PlanCandidate:
    resource_id: str
    model_id: str | None
    runtime: AcceleratorRuntime | None
    feasible: bool
    reasons: tuple[str, ...]
    requirement: ResourceRequirement

    def sort_key(self) -> tuple[str, str, int]:
        return (self.resource_id, self.model_id or "",
                -1 if self.runtime is None else RUNTIME_ORDER[self.runtime])

    def as_record(self) -> dict[str, Any]:
        return {"resource_id": self.resource_id, "model_id": self.model_id,
                "runtime": None if self.runtime is None else self.runtime.value, "feasible": self.feasible,
                "reasons": list(self.reasons), "requirement": self.requirement.model_dump(mode="json")}


@dataclass(frozen=True)
class ModelExclusion:
    model_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResourcePlan:
    task_id: str
    capability_id: str
    capability_digest: str
    feasible: tuple[PlanCandidate, ...]
    infeasible: tuple[PlanCandidate, ...]
    excluded_models: tuple[ModelExclusion, ...]
    selection_authority: str = "NONE"

    def __post_init__(self) -> None:
        if self.selection_authority != "NONE":
            raise ValueError("RESOURCE_PLAN_CANNOT_CARRY_AUTHORITY")

    @property
    def plan_hash(self) -> str:
        return canonical_hash({
            "schema": PLAN_SCHEMA, "task_id": self.task_id, "capability_id": self.capability_id,
            "capability_digest": self.capability_digest,
            "feasible": [item.as_record() for item in self.feasible],
            "infeasible": [item.as_record() for item in self.infeasible],
            "excluded_models": [[item.model_id, list(item.reasons)] for item in self.excluded_models],
        })

    def resource_fit(self, resource_id: str, *, model_id: str | None = None,
                     runtime: AcceleratorRuntime | None = None) -> bool:
        """``resource_fit`` input for ``policy.evaluate``: True only for a listed feasible tuple."""
        return any(item.resource_id == resource_id and item.model_id == model_id
                   and (runtime is None or item.runtime is runtime) for item in self.feasible)

    def evidence(self) -> dict[str, Any]:
        """Bounded, content-free evidence (ids, codes, counts, hashes) that passes the minimization guard."""
        record = {
            "schema": PLAN_SCHEMA, "task_id": self.task_id, "capability_id": self.capability_id,
            "capability_digest": self.capability_digest, "plan_hash": self.plan_hash,
            "selection_authority": self.selection_authority,
            "feasible": [{"resource_id": item.resource_id, "model_id": item.model_id,
                          "runtime": None if item.runtime is None else item.runtime.value}
                         for item in self.feasible[:MAX_EVIDENCE_CANDIDATES]],
            "feasible_count": len(self.feasible), "infeasible_count": len(self.infeasible),
            "excluded_model_count": len(self.excluded_models),
        }
        assert_minimized(record, allowed_keys=tuple(record), max_string=256)
        return record


def model_exclusion_reasons(model: ModelRuntimeProfile, task: PlanningTask,
                            measured: ModelCapabilityProfile | None = None) -> tuple[str, ...]:
    capability = task.capability
    checks = (
        ("MODEL_ROLE_MISMATCH", task.role in model.roles),
        ("MODEL_MODALITY_MISMATCH", bool(set(model.modalities) & set(capability.modalities))),
        ("MODEL_CONTEXT_TOO_SHORT", model.context_length >= task.min_context_length),
        ("MODEL_TOOL_USE_UNSUPPORTED", model.tool_use or not task.requires_tool_use),
        ("MODEL_STRUCTURED_OUTPUT_UNSUPPORTED", model.structured_output or not task.requires_structured_output),
        ("MODEL_CLASSIFICATION_EXCEEDED", model.max_classification.permits(task.classification)),
        ("MODEL_NETWORK_NOT_PERMITTED", network_permitted(model.network_requirement, task.network_policy)),
        ("MODEL_WIDENS_CAPABILITY_NETWORK",
         network_permitted(model.network_requirement, capability.network_requirement)),
    )
    reasons = tuple(code for code, ok in checks if not ok)
    if task.capability_requirement is None:
        return reasons
    if measured is None:
        return (*reasons, "MODEL_CAPABILITY_EVIDENCE_ABSENT")
    rejected, _ = evidence_reasons(measured, task.capability_requirement)
    return (*reasons, *(f"MODEL_{code}" for code in rejected))


def merged_requirement(base: ResourceRequirement, path: ResourceRequirement) -> ResourceRequirement:
    """The capability's own baseline combined with a model runtime path (element-wise maximum)."""
    return ResourceRequirement(
        cpu_cores=max(base.cpu_cores, path.cpu_cores), ram_mb=max(base.ram_mb, path.ram_mb),
        gpu_required=base.gpu_required or path.gpu_required, vram_mb=max(base.vram_mb, path.vram_mb),
        unified_memory_mb=max(base.unified_memory_mb, path.unified_memory_mb), accelerators=path.accelerators,
    )


class ResourcePlanner:
    def __init__(self, inventory: ResourceInventory, catalog: ModelCatalog) -> None:
        self._inventory = inventory
        self._catalog = catalog

    def _candidates(self, requirement: ResourceRequirement, model_id: str | None,
                    runtime: AcceleratorRuntime | None, extra: tuple[str, ...] = ()) -> list[PlanCandidate]:
        candidates = []
        matches = tuple(self._inventory.match(requirement))
        if len({match.resource_id for match in matches}) != len(matches):
            raise ValueError("PLANNING_INVENTORY_DUPLICATE_RESOURCE")
        for match in matches:
            if not isinstance(match.fits, bool):
                raise ValueError("PLANNING_INVENTORY_MATCH_INVALID")
            reasons = (*extra, *match.reasons)
            candidates.append(PlanCandidate(match.resource_id, model_id, runtime, match.fits and not extra,
                                            tuple(reasons), requirement))
        return candidates

    def plan(self, task: PlanningTask) -> ResourcePlan:
        capability = task.capability
        if not capability.lifecycle.plannable:
            raise ValueError("PLANNING_CAPABILITY_NOT_PLANNABLE")
        base = capability.resources
        candidates: list[PlanCandidate] = []
        exclusions: list[ModelExclusion] = []
        if task.role is None:
            candidates = self._candidates(base, None, None)
        else:
            for model in self._catalog:
                excluded = model_exclusion_reasons(model, task, self._catalog.capability_profile(model.model_id))
                if excluded:
                    exclusions.append(ModelExclusion(model.model_id, excluded))
                    continue
                for runtime in sorted(model.runtimes, key=RUNTIME_ORDER.__getitem__):
                    requirement = merged_requirement(base, requirement_for(model, runtime))
                    allowed = not base.accelerators or runtime.value in base.accelerators
                    extra = () if allowed else ("RUNTIME_NOT_ALLOWED_BY_CAPABILITY",)
                    candidates.extend(self._candidates(requirement, model.model_id, runtime, extra))
                    if len(candidates) > MAX_PLAN_CANDIDATES:
                        raise ValueError("PLAN_TOO_LARGE")
        ordered = sorted(candidates, key=PlanCandidate.sort_key)
        return ResourcePlan(
            task_id=task.task_id, capability_id=capability.capability_id,
            capability_digest=descriptor_digest(capability),
            feasible=tuple(item for item in ordered if item.feasible),
            infeasible=tuple(item for item in ordered if not item.feasible),
            excluded_models=tuple(exclusions),
        )


def feasible_tuples(plan: ResourcePlan) -> Iterable[tuple[str | None, str | None, str]]:
    """(runtime, model_id, resource_id) in the plan's fixed order."""
    for item in plan.feasible:
        yield (None if item.runtime is None else item.runtime.value, item.model_id, item.resource_id)
