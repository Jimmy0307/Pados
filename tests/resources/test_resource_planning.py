"""Planner: explained feasible (runtime, model, resource) tuples through the ResourceInventory seam only."""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import pytest
from resource_fixtures import (
    ALL_MODELS,
    ALL_NODES,
    AS_OF,
    CLOUD_TEXT,
    EMBED_SMALL,
    INFERENCE,
    OCR,
    TEXT_7B,
    TEXT_70B,
    capability,
    hardware,
)

from pados.capability.model import LifecycleState, ResourceRequirement
from pados.capability.seams import ResourceMatch
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.lifecycle.minimization import assert_minimized
from pados.resources.hardware import AcceleratorRuntime
from pados.resources.inventory import StaticResourceInventory
from pados.resources.models import ModelCatalog, ModelRole
from pados.resources.planning import (
    PlanningTask,
    ResourcePlan,
    ResourcePlanner,
    feasible_tuples,
)

CATALOG = ModelCatalog(ALL_MODELS)
INVENTORY = StaticResourceInventory(ALL_NODES, as_of=AS_OF)
PLANNER = ResourcePlanner(INVENTORY, CATALOG)
B7, B70 = TEXT_7B.model_id, TEXT_70B.model_id


def task(**overrides: object) -> PlanningTask:
    values: dict[str, object] = dict(task_id="task-1", capability=INFERENCE, classification=DataClassification.INTERNAL,
                                     network_policy=NetworkRequirement.LOCAL_ONLY, role=ModelRole.TEXT)
    values.update(overrides)
    return PlanningTask(**values)  # type: ignore[arg-type]


def test_text_inference_plan_lists_feasible_tuples_in_fixed_order() -> None:
    plan = PLANNER.plan(task())
    assert list(feasible_tuples(plan)) == [
        ("CPU", B7, "future-npu-node"),
        ("CPU", B7, "linux-cpu-node"),
        ("CPU", B70, "mac-unified-node"), ("METAL", B70, "mac-unified-node"),
        ("CPU", B7, "mac-unified-node"), ("METAL", B7, "mac-unified-node"),
        ("CPU", B70, "win-nvidia-node"),
        ("CPU", B7, "win-nvidia-node"), ("CUDA", B7, "win-nvidia-node"),
    ]
    assert plan.selection_authority == "NONE"
    excluded = {item.model_id: item.reasons for item in plan.excluded_models}
    assert excluded == {CLOUD_TEXT.model_id: ("MODEL_NETWORK_NOT_PERMITTED", "MODEL_WIDENS_CAPABILITY_NETWORK"),
                        EMBED_SMALL.model_id: ("MODEL_ROLE_MISMATCH",)}
    cuda_70b = next(item for item in plan.infeasible
                    if item.model_id == B70 and item.runtime is AcceleratorRuntime.CUDA
                    and item.resource_id == "win-nvidia-node")
    assert "VRAM_INSUFFICIENT:required_mb=40960:available_mb=16384" in cuda_70b.reasons
    assert len(plan.feasible) + len(plan.infeasible) == 2 * 3 * len(ALL_NODES)


@pytest.mark.parametrize(("overrides", "models"), [
    (dict(requires_tool_use=True), {B70}),
    (dict(min_context_length=16_384), {B70}),
    (dict(requires_structured_output=True), {B7, B70}),
    (dict(classification=DataClassification.RESTRICTED), set()),
    (dict(role=ModelRole.EMBEDDING), {EMBED_SMALL.model_id}),  # the role filter keeps only embedding models
    (dict(role=ModelRole.OCR), set()),
])
def test_model_filters_only_exclude(overrides: dict[str, object], models: set[str]) -> None:
    plan = PLANNER.plan(task(**overrides))
    assert {item.model_id for item in plan.feasible} == models


def test_embedding_plan_reaches_npu() -> None:
    embed_capability = capability(capability_id="TEXT_EMBEDDING", operation="EMBED_TEXT")
    plan = PLANNER.plan(task(capability=embed_capability, role=ModelRole.EMBEDDING))
    assert ("NPU", EMBED_SMALL.model_id, "future-npu-node") in list(feasible_tuples(plan))


def test_network_ceiling_cannot_be_widened_by_a_model() -> None:
    plan = PLANNER.plan(task(network_policy=NetworkRequirement.WAN_REQUIRED))
    excluded = {item.model_id: item.reasons for item in plan.excluded_models}
    assert excluded[CLOUD_TEXT.model_id] == ("MODEL_WIDENS_CAPABILITY_NETWORK",)


def test_capability_accelerator_allowlist_restricts_runtimes() -> None:
    cuda_only = capability(resources=ResourceRequirement(accelerators=("CUDA",)))
    plan = PLANNER.plan(task(capability=cuda_only))
    assert list(feasible_tuples(plan)) == [("CUDA", B7, "win-nvidia-node")]
    blocked = [item for item in plan.infeasible if item.runtime is AcceleratorRuntime.CPU]
    assert blocked and all(item.reasons[0] == "RUNTIME_NOT_ALLOWED_BY_CAPABILITY" for item in blocked)


def test_capability_baseline_is_merged_into_model_requirement() -> None:
    heavy = capability(resources=ResourceRequirement(cpu_cores=12, ram_mb=16_384))
    plan = PLANNER.plan(task(capability=heavy))
    assert {(item.resource_id, item.model_id, item.runtime) for item in plan.feasible} == {
        ("mac-unified-node", B70, AcceleratorRuntime.CPU), ("mac-unified-node", B70, AcceleratorRuntime.METAL),
        ("mac-unified-node", B7, AcceleratorRuntime.CPU), ("mac-unified-node", B7, AcceleratorRuntime.METAL),
        ("win-nvidia-node", B70, AcceleratorRuntime.CPU), ("win-nvidia-node", B7, AcceleratorRuntime.CPU),
        ("win-nvidia-node", B7, AcceleratorRuntime.CUDA)}
    metal = next(item for item in plan.feasible if item.runtime is AcceleratorRuntime.METAL and item.model_id == B7)
    assert metal.requirement == ResourceRequirement(cpu_cores=12, ram_mb=16_384, gpu_required=True,
                                                    unified_memory_mb=7168, accelerators=("METAL",))


def test_resource_only_capability_plans_without_models() -> None:
    plan = PLANNER.plan(task(capability=OCR, role=None))
    assert [(item.resource_id, item.model_id, item.runtime) for item in plan.feasible] == [
        (item, None, None) for item in sorted(node.resource_id for node in ALL_NODES)]
    assert plan.excluded_models == ()


class SeamOnlyInventory:
    """Exposes only the fabric seam; the planner must not need anything else."""

    def __init__(self, fits: Sequence[str], *, duplicate: bool = False) -> None:
        self.fits = fits
        self.duplicate = duplicate
        self.requests: list[ResourceRequirement] = []

    def match(self, requirement: ResourceRequirement) -> Sequence[ResourceMatch]:
        self.requests.append(requirement)
        items = [ResourceMatch("node-a", "node-a" in self.fits, ("STUB",)),
                 ResourceMatch("node-b", "node-b" in self.fits, ("STUB",))]
        return items + items[:1] if self.duplicate else items


def test_planner_integrates_only_through_the_seam() -> None:
    stub = SeamOnlyInventory(["node-b"])
    plan = ResourcePlanner(stub, ModelCatalog([TEXT_7B])).plan(task())
    assert list(feasible_tuples(plan)) == [("CPU", B7, "node-b"), ("CUDA", B7, "node-b"), ("METAL", B7, "node-b")]
    assert len(stub.requests) == 3
    with pytest.raises(ValueError, match="PLANNING_INVENTORY_DUPLICATE_RESOURCE"):
        ResourcePlanner(SeamOnlyInventory([], duplicate=True), ModelCatalog([TEXT_7B])).plan(task())


def test_plan_is_deterministic_and_bound_to_inputs() -> None:
    first, second = PLANNER.plan(task()), PLANNER.plan(task())
    assert first == second and first.plan_hash == second.plan_hash
    changed = ResourcePlanner(StaticResourceInventory([*ALL_NODES[:3], hardware(resource_id="future-npu-node",
                                                                                stale=True)], as_of=AS_OF), CATALOG)
    assert changed.plan(task()).plan_hash != first.plan_hash


def test_stale_resources_never_appear_feasible() -> None:
    stale = StaticResourceInventory([hardware(stale=True)], as_of=AS_OF)
    plan = ResourcePlanner(stale, CATALOG).plan(task())
    assert plan.feasible == () and all("TELEMETRY_MARKED_STALE" in item.reasons for item in plan.infeasible)


def test_evidence_is_minimized_and_carries_no_authority() -> None:
    plan = PLANNER.plan(task())
    evidence = plan.evidence()
    assert_minimized(evidence, allowed_keys=tuple(evidence))
    assert evidence["selection_authority"] == "NONE" and evidence["feasible_count"] == 9
    assert evidence["plan_hash"] == plan.plan_hash
    assert not any("select" in field.name and field.name != "selection_authority"
                   for field in dataclasses.fields(ResourcePlan))
    with pytest.raises(ValueError, match="RESOURCE_PLAN_CANNOT_CARRY_AUTHORITY"):
        dataclasses.replace(plan, selection_authority="SELECTED")


def test_plan_resource_fit_is_exact() -> None:
    plan = PLANNER.plan(task())
    assert plan.resource_fit("win-nvidia-node", model_id=B7, runtime=AcceleratorRuntime.CUDA)
    assert not plan.resource_fit("win-nvidia-node", model_id=B70, runtime=AcceleratorRuntime.CUDA)
    assert not plan.resource_fit("linux-cpu-node", model_id=B70)
    assert not plan.resource_fit("unknown-node", model_id=B7)


def test_planner_fails_closed_on_invalid_tasks() -> None:
    with pytest.raises(ValueError, match="PLANNING_CAPABILITY_NOT_PLANNABLE"):
        PLANNER.plan(task(capability=capability(lifecycle=LifecycleState.REVOKED)))
    with pytest.raises(ValueError, match="PLANNING_TASK_ID_INVALID"):
        task(task_id="../x y")
    with pytest.raises(ValueError, match="PLANNING_CONTEXT_LENGTH_INVALID"):
        task(min_context_length=-1)
    with pytest.raises(ValueError, match="PLANNING_MODEL_ROLE_REQUIRED"):
        task(role=None)
