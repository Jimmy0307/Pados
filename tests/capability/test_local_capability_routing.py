"""Stage B M4: measured local capability profiles as Worker Router candidates (advisory, policy- and
authorization-bound). The Worker Router stays the only selector; the planner never sees a model or runtime name."""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
from capability_profile_support import LICENSE, make_profile, observations  # noqa: E402

import pados  # noqa: E402
from pados.capability.inference import (  # noqa: E402
    LocalProfileCandidate,
    MeasuredLocalCandidates,
    WorkerRouterBridge,
    registration_authorizes,
)
from pados.capability.seams import InferenceRequest  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.registry import NodeRegistration  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.resources.capability_profile import (  # noqa: E402
    CandidateState,
    CapabilityFloor,
    CapabilityRequirement,
    MeasuredCapability,
    ModelCapabilityProfile,
    ResourceHeadroom,
)
from pados.resources.models import ModelCatalog, profile_from_capability  # noqa: E402
from pados.worker.schemas import (  # noqa: E402
    CapabilityId,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)

SRC = Path(pados.__file__).resolve().parent
OK = CandidateState.AVAILABLE
ROOMY = ResourceHeadroom(free_gpu_memory_mib=8000, available_ram_mib=16000)
REASONING = CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.REASONING,
                                                          min_success_lower_bound=0.4),))
CLOUD = WorkerSpec(worker_id="remote-model", worker_type=WorkerType.CLAUDE_CODE, display_name="remote",
                   availability=WorkerAvailability.AVAILABLE, capabilities=[CapabilityId.SUMMARIZE],
                   risk_ceiling=RiskLevel.MEDIUM, supports_structured_output=True, supports_file_changes=False,
                   supports_tool_calls=False, supports_streaming=False, max_context_tokens=100_000,
                   default_timeout_seconds=60)


def registration(*models: str, authorized: bool = True, node: str = "a", project: str = "p") -> NodeRegistration:
    return NodeRegistration(node_id="lcn-" + node * 20, endpoint="http://127.0.0.1:8765",
                            auth_ref="secretref://env/PADOS_TEST_NODE_KEY", authorized=authorized,
                            allowed_capabilities=(NodeCapability.LOCAL_INFERENCE,),
                            max_classification=DataClassification.RESTRICTED, project_ids=(project,),
                            allowed_models=models)


@dataclass
class World:
    """Mutable trusted state the bridge reads on EVERY selection (catalog, registrations, runtime/device state)."""

    catalog: ModelCatalog
    registrations: list[NodeRegistration] = field(default_factory=list)
    runtime: CandidateState = OK
    device: CandidateState = OK
    headroom: ResourceHeadroom | None = ROOMY
    cloud: dict[str, WorkerSpec] = field(default_factory=lambda: {"remote-model": CLOUD})
    requirement: CapabilityRequirement = REASONING

    def candidates(self) -> list[LocalProfileCandidate]:
        return [LocalProfileCandidate(profile, self.runtime, self.device, self.headroom)
                for profile in self.catalog.capability_profiles()]

    def bridge(self) -> WorkerRouterBridge:
        source = MeasuredLocalCandidates(requirements={"MODEL_SUMMARY": self.requirement},
                                         candidates=self.candidates,
                                         authorization=registration_authorizes(lambda: self.registrations))
        return WorkerRouterBridge(lambda: self.cloud, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE},
                                  measured_local=source)

    def select(self, **overrides: object) -> str | None:
        values: dict[str, object] = dict(project_id="p", stage_id="summary", capability_id="MODEL_SUMMARY",
                                         risk_level="LOW", classification=DataClassification.INTERNAL,
                                         network_policy="WAN_REQUIRED", grant_hash="g" * 64)
        values.update(overrides)
        return self.bridge().select(InferenceRequest(**values)).selected_worker_id  # type: ignore[arg-type]


def registered(*profiles: ModelCapabilityProfile) -> ModelCatalog:
    catalog = ModelCatalog([profile_from_capability(p, license=LICENSE) for p in profiles])
    for profile in profiles:
        catalog = catalog.register_capability_profile(profile)
    return catalog


def worker_id(profile: ModelCapabilityProfile) -> str:
    return "lcp-" + profile.profile_id.removeprefix("mcp-")[:24]


@pytest.fixture
def world() -> World:
    return World(registered(PROFILE))


PROFILE = make_profile()
LOCAL = worker_id(PROFILE)


# --- authorization boundary -------------------------------------------------------------------------------------

def test_registered_but_unauthorized_is_not_routable(world: World) -> None:
    assert world.select() == "remote-model"  # the router moved on to the next permitted worker
    assert world.select(network_policy="LOCAL_ONLY") is None  # no local candidate -> no cloud either
    world.registrations = [registration()]  # authorized node, but the model is not named
    assert world.select() == "remote-model"


def test_authorized_capable_candidate_is_selected_by_the_router(world: World) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    assert world.select() == LOCAL
    world.registrations = [registration(PROFILE.artifact.model_ref, authorized=False)]
    assert world.select() == "remote-model"


def test_revocation_takes_effect_on_the_next_selection(world: World) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    assert world.select() == LOCAL
    world.registrations = [registration()]  # operator removes the model from the registration
    assert world.select() == "remote-model"
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    world.catalog = world.catalog.withdraw_capability_profile(PROFILE.artifact.model_ref)
    assert world.select() == "remote-model"  # withdrawn evidence: no longer a candidate at all


def test_same_tag_different_manifest_is_not_authorized(world: World) -> None:
    retagged = make_profile(manifest_seed="9")
    world.catalog = registered(retagged)
    world.registrations = [registration(PROFILE.artifact.model_ref)]  # authorizes the OLD manifest only
    assert world.select() == "remote-model"
    world.registrations = [registration(retagged.artifact.model_ref)]
    assert world.select() == worker_id(retagged)


def test_an_authority_that_raises_authorizes_nothing(world: World) -> None:
    def broken() -> list[NodeRegistration]:
        raise RuntimeError("registry unavailable")

    source = MeasuredLocalCandidates(requirements={"MODEL_SUMMARY": REASONING}, candidates=world.candidates,
                                     authorization=registration_authorizes(broken))
    bridge = WorkerRouterBridge(lambda: {}, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE}, measured_local=source)
    selection = bridge.select(InferenceRequest(project_id="p", stage_id="s", capability_id="MODEL_SUMMARY",
                                               risk_level="LOW", classification=DataClassification.INTERNAL,
                                               network_policy="LOCAL_ONLY", grant_hash="g" * 64))
    assert selection.selected_worker_id is None


# --- capability evidence ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("requirement", [
    CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.CODE_REPAIR),)),  # absent
    CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.SUMMARIZATION,
                                                  min_success_lower_bound=0.3),)),  # floor not met
    CapabilityRequirement(min_context_tokens=65_536),  # beyond measured context
    CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.TIMEOUT_RECOVERY),)),  # invalid
], ids=["absent", "quality-floor", "context", "invalid-measurement"])
def test_unmet_evidence_rejects_local_and_the_router_escalates_where_policy_allows(
        world: World, requirement: CapabilityRequirement) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    world.requirement = requirement
    assert world.select() == "remote-model"
    assert world.select(network_policy="LOCAL_ONLY") is None
    assert world.select(classification=DataClassification.CONFIDENTIAL) is None  # cloud is capped at INTERNAL


def test_privacy_requirement_keeps_a_capable_local_candidate(world: World) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    assert world.select(classification=DataClassification.CONFIDENTIAL) == LOCAL
    assert world.select(network_policy="LOCAL_ONLY") == LOCAL


def test_unmapped_capability_gets_no_measured_candidates(world: World) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    world.requirement = REASONING
    source = MeasuredLocalCandidates(requirements={}, candidates=world.candidates,
                                     authorization=registration_authorizes(lambda: world.registrations))
    bridge = WorkerRouterBridge(lambda: world.cloud, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE}, measured_local=source)
    selection = bridge.select(InferenceRequest(project_id="p", stage_id="s", capability_id="MODEL_SUMMARY",
                                               risk_level="LOW", classification=DataClassification.INTERNAL,
                                               network_policy="WAN_REQUIRED", grant_hash="g" * 64))
    assert selection.selected_worker_id == "remote-model"


# --- runtime, device and resource state ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", [{"runtime": CandidateState.UNAVAILABLE}, {"device": CandidateState.UNAVAILABLE},
                                    {"headroom": None},
                                    {"headroom": ResourceHeadroom(free_gpu_memory_mib=500, available_ram_mib=500)}],
                         ids=["runtime-unavailable", "device-unavailable", "telemetry-unknown", "insufficient-memory"])
def test_unavailable_candidates_are_never_selected(world: World, change: dict[str, object]) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    for key, value in change.items():
        setattr(world, key, value)
    assert world.select() == "remote-model"
    assert world.select(network_policy="LOCAL_ONLY") is None


def test_degraded_candidates_need_an_explicit_requirement_opt_in(world: World) -> None:
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    world.headroom = ResourceHeadroom(free_gpu_memory_mib=2000, available_ram_mib=9000)  # resource pressure
    assert world.select() == "remote-model"
    world.requirement = REASONING.model_copy(update={"allow_degraded": True})
    assert world.select() == LOCAL
    world.headroom, world.runtime = ROOMY, CandidateState.DEGRADED
    assert world.select() == LOCAL
    world.requirement = REASONING
    assert world.select() == "remote-model"


def test_resource_pressure_orders_available_before_degraded_local_candidates() -> None:
    fast, slow = make_profile(manifest_seed="1"), make_profile(manifest_seed="2", gpu_memory_mib=7900)
    world = World(registered(fast, slow), headroom=ResourceHeadroom(free_gpu_memory_mib=7000, available_ram_mib=9000),
                  requirement=REASONING.model_copy(update={"allow_degraded": True}))
    world.registrations = [registration(fast.artifact.model_ref, slow.artifact.model_ref)]
    assert world.select() == worker_id(fast)  # the profile that fits in GPU memory is presented first
    world.registrations = [registration(slow.artifact.model_ref)]
    assert world.select() == worker_id(slow)  # degraded, but allowed and the only local candidate


# --- replaceability and no brand routing --------------------------------------------------------------------------

def test_model_replaceability_requires_no_code_change() -> None:
    replacement = make_profile(artifact_ref="another-vendor-model:3b", manifest_seed="c", layer_seed="d",
                               runtime_family="other-runtime", runtime_version="9.9", model_family="unrelated",
                               obs=observations(summary=None))
    world = World(registered(PROFILE, replacement))
    world.registrations = [registration(replacement.artifact.model_ref)]
    assert world.select() == worker_id(replacement)  # same schema, same requirement, different model
    world.registrations = [registration(PROFILE.artifact.model_ref)]
    assert world.select() == LOCAL
    world.requirement = CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.SUMMARIZATION),))
    world.registrations = [registration(replacement.artifact.model_ref)]
    assert world.select() == "remote-model"  # the replacement never measured summarization


PLANNER_FACING = ("capability/planner.py", "capability/inference.py", "capability/seams.py", "capability/policy.py",
                  "resources/capability_profile.py", "resources/planning.py", "resources/models.py",
                  "resources/inventory.py", "worker/router.py")
FORBIDDEN_TOKENS = ("qwen", "ollama", "registry.ollama.ai", "/api/generate", "/api/chat", "ollama pull",
                    "qwen3:8b", "d:\\ollama", "windows")


@pytest.mark.parametrize("name", PLANNER_FACING)
def test_no_brand_or_runtime_specific_branch_in_planner_facing_code(name: str) -> None:
    tree = ast.parse((SRC / name).read_text(encoding="utf-8-sig"))
    literals = [node.value.lower() for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str) and not _is_docstring(tree, node)]
    hits = {token for token in FORBIDDEN_TOKENS for text in literals if token in text}
    assert not hits, (name, hits)  # the router's ranking never names a model or runtime string either
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if name != "worker/router.py":  # the router's existing worker-class priority list is not capability logic
        assert not {n for n in names if "ollama" in n.lower() or "qwen" in n.lower()}, name
    for attr in ("artifact_ref", "model_family", "runtime_family"):
        if name in {"capability/planner.py", "resources/planning.py", "capability/inference.py"}:
            assert attr not in names, (name, attr)  # selection never reads identity labels or provenance


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and body[0].value is node:
            return True
    return False


def test_outcome_learning_has_no_path_into_profiles_routing_or_authorization() -> None:
    for name in ("resources/capability_profile.py", "resources/models.py", "resources/planning.py",
                 "capability/inference.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not {m for m in modules if m.startswith("pados.learning")}, name
    learning = SRC / "learning"
    for path in learning.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not {m for m in modules if m.startswith(("pados.resources.capability_profile",
                                                        "pados.compute_node.registry"))}, path.name
    profile = make_profile()
    with pytest.raises(Exception):  # noqa: B017 - frozen: no in-place update by any observer
        profile.observations = ()  # type: ignore[misc]


# --- end to end through the existing Capability Planner, Worker Router bridge and executor ---------------------

def test_end_to_end_planner_routes_to_the_authorized_measured_profile(tmp_path: Path) -> None:
    from fabric_support import PROJECT, fabric, with_model

    from pados.capability.executor import ExecutionRefused, ExecutionStatus

    fab = fabric(tmp_path)
    task = with_model(fab)
    world = World(registered(PROFILE), cloud={})
    world.registrations = [registration(PROFILE.artifact.model_ref, project=PROJECT)]
    bridge = world.bridge()
    plan = fab.planner(inference=bridge).plan(task)
    assert plan.feasible
    stage = plan.stages[-1]
    assert stage.router_status == "ROUTED"
    result = fab.executor(inference=bridge).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    assert result.router_evidence[str(stage.router_selection_hash)].selected_worker_id == LOCAL
    revoked = [registration(project=PROJECT)]
    # Revoked DURING execution (after planning, before the model stage): the stage's router re-check refuses it.
    fab.adapters["chunk.fixed"].hook = lambda: world.registrations.__setitem__(slice(None), revoked)
    refused = fab.executor(inference=bridge).execute(plan, task)
    assert refused.failure_code == "summary:WORKER_ROUTER_BLOCKED"  # no other worker exists in this world
    assert len(fab.adapters["llm.bridge"].calls) == 1  # only the first, authorized run reached the model stage
    # Revoked BEFORE execution: the plan can no longer be reproduced from trusted state at all.
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.executor(inference=bridge).execute(plan, task)
    replanned = fab.planner(inference=bridge).plan(task)
    assert not replanned.feasible and replanned.stages[-1].infeasible_code == "WORKER_ROUTER_BLOCKED"
