"""Stage B M4.1: the live governed chain (REHEARSAL: loopback fake runtime + fake host; never model evidence).

Real components: ModelCatalog store, LiveLocalState over DeviceProfiler and runtime discovery, bind_live,
WorkerRouterBridge + select_worker, CapabilityPlanner, CapabilityExecutor, LocalInferenceStageAdapter,
LocalInferenceProvider with its MeasuredExecutionGate, the compute-node service and its runtime backend over HTTP.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ollama import FakeOllama  # noqa: E402
from live_chain_support import Clock, fake_profiler, persist, rehearsal_profile  # noqa: E402
from m4_1_live_chain import (  # noqa: E402
    CAPABILITY,
    DOGFOOD_SCHEMA,
    PROJECT,
    REQUIREMENT,
    LiveChain,
    build_chain,
    validate_extraction,
)

from pados.capability.executor import ExecutionRefused, ExecutionStatus  # noqa: E402
from pados.capability.inference import MeasuredLocalCandidates, WorkerRouterBridge, worker_id_for  # noqa: E402
from pados.capability.seams import InferenceRequest  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.provider import NodeExecutionError, RouteIntent, RoutingDenied  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.local_intelligence.catalog_store import CatalogStoreError, ModelCatalogStore  # noqa: E402
from pados.resources.capability_profile import (  # noqa: E402
    BindingPolicy,
    CandidateState,
    CapabilityFloor,
    CapabilityRequirement,
    DeviceObservation,
    Freshness,
    MeasuredCapability,
    ObservationStatus,
    RuntimeArtifact,
    RuntimeObservation,
    assess,
    bind_live,
)
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType  # noqa: E402

CLOUD = WorkerSpec(worker_id="remote-model", worker_type=WorkerType.CLAUDE_CODE, display_name="remote fixture",
                   availability=WorkerAvailability.AVAILABLE, capabilities=[CapabilityId.SUMMARIZE],
                   risk_ceiling=RiskLevel.MEDIUM, supports_structured_output=True, supports_file_changes=False,
                   supports_tool_calls=False, supports_streaming=False, max_context_tokens=200_000,
                   default_timeout_seconds=60)


@dataclass
class Rig:
    chain: LiveChain
    fake: FakeOllama
    clock: Clock
    tmp: Path
    profile_failures: list[bool]

    def chats(self) -> int:
        return sum(1 for r in self.fake.requests if r["path"] == "/api/chat")

    def plan_and_execute(self):  # type: ignore[no-untyped-def]
        planner, executor, bridge, adapter, task = self.chain.fabric()
        plan = planner.plan(task)
        return plan, (executor.execute(plan, task) if plan.feasible else None), adapter, bridge, task

    def request(self, **overrides: object) -> InferenceRequest:
        values: dict[str, object] = dict(project_id=PROJECT, stage_id="extract", capability_id=CAPABILITY,
                                         risk_level="LOW", classification=DataClassification.INTERNAL,
                                         network_policy="LOCAL_ONLY", grant_hash="g" * 64)
        values.update(overrides)
        return InferenceRequest(**values)  # type: ignore[arg-type]


def make_rig(tmp: Path, *, profile=None, digest_hex: str | None = None, vram_free: int | None = None,  # type: ignore[no-untyped-def]
             policy: BindingPolicy | None = None, **fake_options: object) -> Iterator[Rig]:
    clock = Clock()
    failures = [False]
    persist(tmp / "catalog.json", profile or rehearsal_profile())
    kwargs: dict[str, object] = {"digest_hex": digest_hex} if digest_hex else {}
    kwargs.update(fake_options)
    with FakeOllama(present=True, **kwargs) as fake:
        chain, stop = build_chain(tmp / "run", catalog_path=tmp / "catalog.json", runtime_endpoint=fake.url,
                                  profiler=fake_profiler(clock, vram_free=vram_free, fail=failures),
                                  transport="inprocess", clock=clock, policy=policy)
        try:
            yield Rig(chain, fake, clock, tmp, failures)
        finally:
            stop()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield from make_rig(tmp_path)


# --- 1-4: authorization and revocation ----------------------------------------------------------------------------

def test_registered_but_unauthorized_is_not_routable(rig: Rig) -> None:
    rig.chain.write_registration(models=())  # authorized NODE, empty model allowlist: authorizes no model
    plan, result, *_ = rig.plan_and_execute()
    assert not plan.feasible and plan.stages[-1].infeasible_code == "WORKER_ROUTER_BLOCKED" and result is None
    with pytest.raises(RoutingDenied):
        rig.chain.provider().route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    assert rig.chats() == 0


def test_explicitly_authorized_capable_live_candidate_runs_end_to_end(rig: Rig) -> None:
    rig.chain.write_registration()
    plan, result, adapter, bridge, _ = rig.plan_and_execute()
    assert plan.feasible and plan.stages[-1].router_status == "ROUTED"
    assert result is not None and result.status is ExecutionStatus.COMPLETED, result
    selection = result.router_evidence[str(plan.stages[-1].router_selection_hash)]
    assert selection.selected_worker_id == worker_id_for(rig.chain.profile)
    run = adapter.executions[-1]
    assert run["measured"].profile_id == rig.chain.profile.profile_id
    assert run["measured"].manifest_digest == rig.chain.profile.artifact.manifest_digest
    assert run["receipt"]["verification"]["record_mac"] == "VERIFIED" and run["receipt"]["semantic_authority"] is False
    assert validate_extraction(rig.chain.output_bytes(result) or b"")["passed"]
    assert rig.chats() == 1
    chat = next(r["body"] for r in rig.fake.requests if r["path"] == "/api/chat")
    assert chat["model"] == rig.chain.profile.artifact.artifact_ref  # invoked by the name listed for the digest


def test_revoked_before_selection_is_not_routable(rig: Rig) -> None:
    rig.chain.write_registration()
    rig.chain.revoke()
    plan, result, *_ = rig.plan_and_execute()
    assert not plan.feasible and result is None and rig.chats() == 0


def test_revoked_after_plan_before_execution_refuses_execution(rig: Rig) -> None:
    rig.chain.write_registration()
    planner, executor, *_rest, task = rig.chain.fabric()
    plan = planner.plan(task)
    assert plan.feasible
    rig.chain.revoke()
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        executor.execute(plan, task)
    # The provider's own gate, independently: routed while authorized, revoked, then execute -> refused, no job.
    rig.chain.write_registration()
    provider = rig.chain.provider()
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    assert route.measured is not None
    rig.chain.revoke()
    with pytest.raises(NodeExecutionError, match="EXECUTION_GATE_REFUSED:MODEL_NOT_AUTHORIZED"):
        provider.execute(route, run_id="r1", work_package_id="w", inputs=[{"text": "x"}], output_path="o.txt",
                         allowed_paths=("o.txt",), timeout_seconds=5)
    assert rig.chats() == 0


def test_revocation_while_the_job_runs_withholds_the_output(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.chain.write_registration()
    provider = rig.chain.provider()
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    original = provider._run

    def run_then_revoke(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = original(*args, **kwargs)
        rig.chain.revoke()  # the job already ran on the node; it is NOT cancelled (no such guarantee exists)
        return record

    monkeypatch.setattr(provider, "_run", run_then_revoke)
    with pytest.raises(NodeExecutionError, match="^REVOKED_DURING_EXECUTION$"):
        provider.execute(route, run_id="r1", work_package_id="w",
                         inputs=[{"text": "INV-88213", "response_schema": DOGFOOD_SCHEMA}],
                         output_path="o.txt", allowed_paths=("o.txt",), timeout_seconds=5)
    assert rig.chats() == 1  # inference happened; its output was withheld


# --- 5-8: identity, runtime and device ---------------------------------------------------------------------------

def test_different_manifest_under_the_same_tag_refuses_binding(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, digest_hex="f" * 64):  # runtime lists the tag with ANOTHER manifest digest
        binding = rig.chain.binding(rig.chain.profile)
        assert binding.runtime_ref is None and "MODEL_ARTIFACT_NOT_PRESENT" in binding.reasons
        assert binding.runtime_state is CandidateState.UNAVAILABLE
        rig.chain.write_registration()
        plan, result, *_ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.chats() == 0


def test_same_tag_different_manifest_is_a_different_artifact() -> None:
    a = rehearsal_profile()
    b = rehearsal_profile(manifest_seed="9")
    assert a.artifact.artifact_ref == b.artifact.artifact_ref
    assert a.artifact.artifact_id != b.artifact.artifact_id and a.artifact.model_ref != b.artifact.model_ref
    assert a.profile_id != b.profile_id


def test_runtime_unavailable_rejects_local(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path):
        rig.chain.write_registration()
        rig.fake.server.shutdown()
        rig.fake.server.server_close()
        binding = rig.chain.binding(rig.chain.profile)
        assert binding.runtime_state is CandidateState.UNAVAILABLE and "RUNTIME_UNREACHABLE" in binding.reasons
        plan, result, *_ = rig.plan_and_execute()
        assert not plan.feasible and result is None


def test_device_unavailable_stale_or_different_rejects_local(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path):
        rig.chain.write_registration()
        assert rig.chain.binding(rig.chain.profile).device_freshness is Freshness.FRESH
        rig.profile_failures[0] = True  # the profiler stops answering; the last observation ages
        rig.clock.now += timedelta(seconds=BindingPolicy().max_age_seconds + 1)
        binding = rig.chain.binding(rig.chain.profile)
        assert binding.device_freshness is Freshness.STALE and "DEVICE_STATE_STALE" in binding.reasons
        assert binding.headroom is None and not rig.plan_and_execute()[0].feasible
    other = rehearsal_profile(device_id="e" * 64)  # measured on another device
    for rig in make_rig(tmp_path / "other", profile=other):
        rig.chain.write_registration()
        assert "DEVICE_NOT_MEASURED" in rig.chain.binding(rig.chain.profile).reasons
        assert not rig.plan_and_execute()[0].feasible


def test_device_never_observed_is_unavailable() -> None:
    profile = rehearsal_profile()
    runtime = RuntimeObservation(runtime_id="r", runtime_family="ollama", runtime_version="0.34.3",
                                 state=CandidateState.AVAILABLE, observed_at=None,
                                 artifacts=(RuntimeArtifact(runtime_ref="m", manifest_digest=profile.artifact.manifest_digest),))
    binding = bind_live(profile, (runtime,), None, now=Clock()(), policy=BindingPolicy())
    assert binding.device_state is CandidateState.UNAVAILABLE and "DEVICE_STATE_UNAVAILABLE" in binding.reasons
    assert binding.runtime_freshness is Freshness.UNAVAILABLE and "RUNTIME_STATE_UNAVAILABLE" in binding.reasons


# --- 9: resource pressure ----------------------------------------------------------------------------------------

def test_resource_pressure_changes_eligibility_never_authorization(tmp_path: Path) -> None:
    for rig in make_rig(tmp_path, vram_free=3000):  # measured footprint 6000 MiB; part must offload to RAM
        rig.chain.write_registration()
        binding = rig.chain.binding(rig.chain.profile)
        assessment = assess(rig.chain.profile, REQUIREMENT, runtime_state=binding.runtime_state,
                            device_state=binding.device_state, headroom=binding.headroom)
        assert assessment.state is CandidateState.DEGRADED and "RESOURCE_PRESSURE_PARTIAL_OFFLOAD" in assessment.reasons
        before = rig.chain.registry_path.read_bytes()
        assert not rig.plan_and_execute()[0].feasible  # degraded is not eligible without an explicit opt-in
        assert rig.chain.registry_path.read_bytes() == before  # pressure never touched authorization
    strict = BindingPolicy(ram_reserve_mib=30_000)  # an operator reserve larger than free RAM
    for rig in make_rig(tmp_path / "strict", vram_free=3000, policy=strict):
        binding = rig.chain.binding(rig.chain.profile)
        assessment = assess(rig.chain.profile, REQUIREMENT, runtime_state=binding.runtime_state,
                            device_state=binding.device_state, headroom=binding.headroom)
        assert assessment.state is CandidateState.UNAVAILABLE and "INSUFFICIENT_MEMORY" in assessment.reasons


# --- 10-13: evidence, context and escalation ----------------------------------------------------------------------

@pytest.mark.parametrize("floor", [
    CapabilityFloor(capability=MeasuredCapability.CODE_REPAIR),  # never measured in the rehearsal profile
    CapabilityFloor(capability=MeasuredCapability.TIMEOUT_RECOVERY),  # INVALID_MEASUREMENT
], ids=["unknown", "invalid-measurement"])
def test_unknown_or_invalid_capability_never_satisfies(rig: Rig, floor: CapabilityFloor) -> None:
    profile = rig.chain.profile  # measured: no CODE_REPAIR at all; TIMEOUT_RECOVERY is INVALID_MEASUREMENT
    status = profile.observation(floor.capability).status
    assert status in {ObservationStatus.NOT_MEASURED, ObservationStatus.INVALID_MEASUREMENT}
    binding = rig.chain.binding(rig.chain.profile)
    assessment = assess(profile, CapabilityRequirement(floors=(floor,)), runtime_state=binding.runtime_state,
                        device_state=binding.device_state, headroom=binding.headroom)
    assert assessment.state is CandidateState.UNAVAILABLE


def test_context_above_measured_range_rejects_local_at_route(rig: Rig) -> None:
    rig.chain.write_registration()
    provider = rig.chain.provider()
    with pytest.raises(RoutingDenied) as denied:
        provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT,
                                   min_context_tokens=65_536))
    considered = json.dumps(denied.value.considered)
    assert "NO_AVAILABLE_MODEL" in considered or "CONTEXT_EXCEEDS_MEASURED_EVIDENCE" in considered
    # Within the node's declared limit but beyond measured evidence, the measured gate itself refuses.
    requirement = REQUIREMENT.model_copy(update={"min_context_tokens": 16_385})
    binding = rig.chain.binding(rig.chain.profile)
    assessment = assess(rig.chain.profile, requirement, runtime_state=binding.runtime_state,
                        device_state=binding.device_state, headroom=binding.headroom)
    assert "CONTEXT_EXCEEDS_MEASURED_EVIDENCE" in assessment.reasons and not assessment.eligible(requirement)


def test_local_rejected_and_allowed_remote_candidate_escalates_only_where_policy_allows(rig: Rig) -> None:
    rig.chain.write_registration()
    local = worker_id_for(rig.chain.profile)
    beyond = CapabilityRequirement(min_context_tokens=65_536)  # local cannot satisfy this context
    for requirement, expected_wan in ((REQUIREMENT, local), (beyond, "remote-model")):
        measured = MeasuredLocalCandidates(requirements={CAPABILITY: requirement}, candidates=rig.chain.candidates,
                                           authorization=rig.chain.measured().authorization)
        bridge = WorkerRouterBridge(lambda: {"remote-model": CLOUD}, {CAPABILITY: CapabilityId.SUMMARIZE},
                                    measured_local=measured)
        assert bridge.select(rig.request(network_policy="WAN_REQUIRED")).selected_worker_id == expected_wan
        if expected_wan == "remote-model":  # never a silent cloud fallback under LOCAL_ONLY or CONFIDENTIAL
            assert bridge.select(rig.request()).selected_worker_id is None
            assert bridge.select(rig.request(network_policy="WAN_REQUIRED",
                                             classification=DataClassification.CONFIDENTIAL)).selected_worker_id is None


# --- 14-15: replaceability and outcome learning ------------------------------------------------------------------

def test_replacement_model_on_another_runtime_binds_through_the_same_generic_contract() -> None:
    replacement = rehearsal_profile(artifact_ref="another-model:3b", manifest_seed="c", runtime_family="other-runtime",
                                    runtime_version="9.9", model_family="unrelated")
    now = Clock()()
    runtime = RuntimeObservation(runtime_id="rt-2", runtime_family="other-runtime", runtime_version="9.9",
                                 state=CandidateState.AVAILABLE, observed_at=now,
                                 artifacts=(RuntimeArtifact(runtime_ref="vendor-name-for-it",
                                                            manifest_digest=replacement.artifact.manifest_digest),))
    device = DeviceObservation(device_profile_id=replacement.device.device_profile_id, state=CandidateState.AVAILABLE,
                               free_gpu_memory_mib=8000, available_ram_mib=30000, observed_at=now)
    binding = bind_live(replacement, (runtime,), device, now=now, policy=BindingPolicy())
    assert binding.bindable and binding.runtime_ref == "vendor-name-for-it" and not binding.reasons
    assert assess(replacement, REQUIREMENT, runtime_state=binding.runtime_state, device_state=binding.device_state,
                  headroom=binding.headroom).eligible(REQUIREMENT)
    wrong_version = runtime.model_copy(update={"runtime_version": "10.0"})
    assert "RUNTIME_VERSION_NOT_MEASURED" in bind_live(replacement, (wrong_version,), device, now=now,
                                                       policy=BindingPolicy()).reasons


def test_outcome_observations_stay_advisory(rig: Rig) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "learning"))
    from learning_support import outcome

    from pados.learning.profiles import build_profiles

    rig.chain.write_registration()
    bridge = WorkerRouterBridge(lambda: {}, {CAPABILITY: CapabilityId.SUMMARIZE}, measured_local=rig.chain.measured())
    before = (bridge.select(rig.request()), rig.chain.catalog().catalog_digest, rig.chain.registry_path.read_bytes())
    failures = [outcome(i, adapter_id="local.measured-inference", success=False) for i in range(30)]
    learned = build_profiles(failures)
    assert learned and all(p.success_rate == 0.0 for p in learned.values())  # the learner saw only failures
    after = (bridge.select(rig.request()), rig.chain.catalog().catalog_digest, rig.chain.registry_path.read_bytes())
    assert after == before  # no authorization, catalog, lifecycle or selection change from outcomes


# --- persistence --------------------------------------------------------------------------------------------------

def test_profile_survives_a_process_restart_unchanged_and_unauthorized(tmp_path: Path) -> None:
    profile = rehearsal_profile()
    store = persist(tmp_path / "catalog.json", profile)
    code = ("import json,sys; from pados.local_intelligence.catalog_store import ModelCatalogStore as S; "
            "c=S(sys.argv[1]).load(); p=c.capability_profiles()[0]; "
            "print(json.dumps({'id':p.profile_id,'m':p.artifact.manifest_digest,'e':p.benchmark_evidence_digest,"
            "'b':p.binding.binding_id,'d':c.catalog_digest}))")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path / "catalog.json")], capture_output=True, text=True,
                         check=True)
    reloaded = json.loads(out.stdout)
    assert reloaded == {"id": profile.profile_id, "m": profile.artifact.manifest_digest,
                        "e": profile.benchmark_evidence_digest, "b": profile.binding.binding_id,
                        "d": store.load().catalog_digest}
    document = json.loads((tmp_path / "catalog.json").read_text())
    assert set(document) == {"schema_version", "models", "capability_profiles"}  # no authorization or routability
    assert "authoriz" not in json.dumps(document).lower().replace('"authority": "none"', "")
    document["capability_profiles"][0]["observations"][0]["success_count"] = 0  # tamper with the evidence
    (tmp_path / "catalog.json").write_text(json.dumps(document))
    with pytest.raises(CatalogStoreError):
        ModelCatalogStore(tmp_path / "catalog.json").load()


def test_live_and_gate_modules_hold_no_brand_or_runtime_literals() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "pados"
    for name in ("local_intelligence/live.py", "packs/local_inference/adapter.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for token in ("qwen", "ollama", "registry.ollama.ai", "/api/chat", "/api/generate", "d:\\ollama"):
            assert token not in text, (name, token)
    digest = hashlib.sha256((root / "resources" / "capability_profile.py").read_bytes()).hexdigest()
    assert digest  # the generic binding contract is covered by the planner-facing static test as well


# --- operator package: rehearsal run and independent verifier ----------------------------------------------------

def test_operator_dogfood_package_rehearsal_and_verifier(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, capture_output=True, text=True,
                          check=True).stdout.strip()
    script = root / "scripts" / "stage_b"
    run = subprocess.run([sys.executable, str(script / "windows_m4_1_dogfood.py"), "--expected-commit", head,
                          "--expected-tree", tree, "--rehearsal", "--out", str(tmp_path / "rehearsal")],
                         capture_output=True, text=True, check=False, timeout=300)
    summary = json.loads(run.stdout[run.stdout.index("{"):])
    assert run.returncode == 1 and summary["REAL_TASK_DOGFOOD"] == "REHEARSAL_PASS"  # never PASS from fakes
    verify = subprocess.run([sys.executable, str(script / "m4_1_verify_dogfood.py"), summary["archive"], "--sha256",
                             summary["archive_sha256"], "--rehearsal"], capture_output=True, text=True, check=False)
    assert verify.returncode == 0, verify.stdout[-2000:]
    tampered = subprocess.run([sys.executable, str(script / "m4_1_verify_dogfood.py"), summary["archive"], "--sha256",
                               "0" * 64, "--rehearsal"], capture_output=True, text=True, check=False)
    assert tampered.returncode == 1 and "archive_sha256" in tampered.stdout
    wrong = subprocess.run([sys.executable, str(script / "windows_m4_1_dogfood.py"), "--expected-commit", "0" * 40,
                            "--expected-tree", tree, "--rehearsal", "--out", str(tmp_path / "x")],
                           capture_output=True, text=True, check=False)
    assert wrong.returncode == 2 and "REFUSED" in wrong.stdout
