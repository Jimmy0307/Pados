"""Stage B M4: the existing ResourcePlanner consumes REGISTERED measured capabilities (opt-in, exclude-only)."""
from __future__ import annotations

from capability_profile_support import LICENSE, make_profile
from resource_fixtures import AS_OF, WINDOWS_NVIDIA

from pados.capability.model import (
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Modality,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.resources.capability_profile import CapabilityFloor, CapabilityRequirement, MeasuredCapability
from pados.resources.inventory import StaticResourceInventory
from pados.resources.models import ModelCatalog, ModelRole, profile_from_capability
from pados.resources.planning import PlanningTask, ResourcePlanner


def requirement(*floors: CapabilityFloor) -> CapabilityRequirement:
    return CapabilityRequirement(floors=floors)


def floor(capability: MeasuredCapability, lower: float = 0.0) -> CapabilityFloor:
    return CapabilityFloor(capability=capability, min_success_lower_bound=lower)


def _planning(catalog: ModelCatalog, req: CapabilityRequirement | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    capability = CapabilityDescriptor(
        capability_id="LOCAL_TEXT", version="1", family=CapabilityFamily.MODEL_RUNTIME, operation="GENERATE_TEXT",
        input_contract="text.in/v1", output_contract="text.out/v1", determinism=Determinism.PROBABILISTIC,
        modalities=(Modality.TEXT,), verification=(VerificationMethod.HUMAN_REVIEW,))
    inventory = StaticResourceInventory([WINDOWS_NVIDIA], as_of=AS_OF)
    task = PlanningTask(task_id="t1", capability=capability, classification=DataClassification.INTERNAL,
                        network_policy=NetworkRequirement.LOCAL_ONLY, role=ModelRole.TEXT,
                        capability_requirement=req)
    plan = ResourcePlanner(inventory, catalog).plan(task)
    return (tuple(str(item.model_id) for item in plan.feasible),
            tuple(code for item in plan.excluded_models for code in item.reasons))


def test_resource_planner_consumes_measured_capabilities_only_when_asked() -> None:
    measured, unmeasured = make_profile(), make_profile(manifest_seed="7")
    catalog = ModelCatalog([profile_from_capability(measured, license=LICENSE),
                            profile_from_capability(unmeasured, license=LICENSE)]).register_capability_profile(measured)
    feasible, _ = _planning(catalog, None)
    assert len(feasible) == 2  # no requirement: planning unchanged
    feasible, excluded = _planning(catalog, requirement(floor(MeasuredCapability.REASONING, 0.4)))
    assert feasible == (measured.artifact.model_ref,) and excluded == ("MODEL_CAPABILITY_EVIDENCE_ABSENT",)
    feasible, excluded = _planning(catalog, requirement(floor(MeasuredCapability.SUMMARIZATION, 0.3)))
    assert feasible == () and "MODEL_CAPABILITY_BELOW_FLOOR:SUMMARIZATION" in excluded
