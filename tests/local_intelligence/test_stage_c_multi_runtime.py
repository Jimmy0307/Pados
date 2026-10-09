"""Stage C: another model AND another runtime enter PADOS with no change to planner, scheduler, Worker Router,
provider routing or semantic authority (CONTRACT ACCEPTANCE on fake runtimes; never model evidence).

    ModelArtifactIdentity -> ModelCapabilityProfile (MeasuredExecutionCondition) -> live RuntimeBinding (by digest)
    -> unchanged planner / WorkerRouterBridge + select_worker / LocalInferenceProvider measured gate -> node adapter

Real components: ModelCatalog store, LiveLocalState over DeviceProfiler and runtime discovery, bind_live, the
WorkerRouterBridge and select_worker, CapabilityPlanner, CapabilityExecutor, LocalInferenceStageAdapter,
LocalInferenceProvider with its MeasuredExecutionGate, the compute-node app and its runtime backends over HTTP.
Fakes: the two runtimes (loopback, ephemeral ports) and the host the device profiler sees.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_llama_server import FakeLlamaServer  # noqa: E402
from fake_ollama import FakeOllama  # noqa: E402
from live_chain_support import Clock, fake_profiler, persist, rehearsal_profile  # noqa: E402
from m4_1_live_chain import CAPABILITY, DOGFOOD_SCHEMA, PROJECT, REQUIREMENT, LiveChain, validate_extraction  # noqa: E402
from stage_c_support import LLAMA_FAMILY, RuntimeSpec, build_multi_runtime_chain, gguf_file, llama_profile  # noqa: E402

from capability_profile_support import counted, observations  # noqa: E402
from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.capability.inference import measured_local_workers, worker_id_for  # noqa: E402
from pados.capability.seams import InferenceRequest  # noqa: E402
from pados.compute_node.receipts import verify_node_receipt  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.resources.capability_profile import (  # noqa: E402
    CapabilityObservation,
    MeasuredCapability,
    ModelCapabilityProfile,
    ObservationStatus,
    required_execution,
)
from pados.worker.schemas import CapabilityId, WorkerAvailability  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class StageC:
    chain: LiveChain
    llama: FakeLlamaServer | None
    ollama: FakeOllama | None

    def llama_chats(self) -> list[dict[str, Any]]:
        return self.llama.chats() if self.llama else []

    def ollama_chats(self) -> int:
        return sum(1 for r in self.ollama.requests if r["path"] == "/api/chat") if self.ollama else 0

    def plan_and_execute(self) -> tuple[Any, Any, Any]:
        planner, executor, _bridge, adapter, task = self.chain.fabric()
        plan = planner.plan(task)
        return plan, (executor.execute(plan, task) if plan.feasible else None), adapter

    def request(self) -> InferenceRequest:
        return InferenceRequest(project_id=PROJECT, stage_id="extract", capability_id=CAPABILITY, risk_level="LOW",
                                classification=DataClassification.INTERNAL, network_policy="LOCAL_ONLY",
                                grant_hash="g" * 64)

    def workers(self) -> dict[str, Any]:
        return measured_local_workers(self.chain.measured(), self.request(), CapabilityId.SUMMARIZE)


@contextmanager
def stage_c(tmp: Path, profiles: tuple[ModelCapabilityProfile, ...], *, serve: tuple[str, Path] | None,
            models_root: Path | None, with_ollama: bool = False, **llama_options: Any) -> Iterator[StageC]:
    """Persist ``profiles`` (the catalog), start the fake runtimes and wire the unchanged governed chain."""
    clock = Clock()
    persist(tmp / "catalog.json", *profiles)
    with ExitStack() as stack:
        specs: list[RuntimeSpec] = []
        llama = ollama = None
        if serve is not None:
            alias, path = serve
            llama = stack.enter_context(FakeLlamaServer(alias=alias, model_path=str(path), **llama_options))
            specs.append(RuntimeSpec(LLAMA_FAMILY, llama.url, str(models_root) if models_root else None))
        if with_ollama:
            ollama = stack.enter_context(FakeOllama(present=True))
            specs.append(RuntimeSpec("ollama", ollama.url))
        chain, stop = build_multi_runtime_chain(tmp / "run", catalog_path=tmp / "catalog.json", runtimes=specs,
                                                profiler=fake_profiler(clock), clock=clock)
        stack.callback(stop)
        yield StageC(chain, llama, ollama)


def one_llama_model(tmp: Path, *, alias: str = "brand-b:4b", model_family: str = "brand-b",
                    **profile_options: Any) -> tuple[ModelCapabilityProfile, Path, Path]:
    root = tmp / "models"
    path = gguf_file(root, alias.replace(":", "-").replace("/", "-") + ".gguf", alias)
    return llama_profile(root, path, alias=alias, model_family=model_family, **profile_options), root, path


# --- 1: same planner, different model, different runtime, same capability contract -------------------------------

def test_another_model_on_another_runtime_completes_through_the_unchanged_chain(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path)
    # reasoning_tokens: a reasoning phase longer than the budget would leave NO visible answer unless the measured
    # reasoning mode (DISABLED) is really applied by the adapter
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root,
                 reasoning_tokens=400) as rig:
        binding = rig.chain.binding(profile)
        assert binding.bindable and binding.runtime_ref == profile.artifact.artifact_ref and not binding.reasons
        rig.chain.write_registration()
        plan, result, adapter = rig.plan_and_execute()
        assert plan.feasible and plan.stages[-1].router_status == "ROUTED"
        assert result is not None and result.status is ExecutionStatus.COMPLETED, result
        selection = result.router_evidence[str(plan.stages[-1].router_selection_hash)]
        assert selection.selected_worker_id == worker_id_for(profile)
        assert validate_extraction(rig.chain.output_bytes(result) or b"")["passed"]
        run = adapter.executions[-1]
        assert run["measured"].profile_id == profile.profile_id
        assert run["measured"].manifest_digest == profile.artifact.manifest_digest
        receipt = run["receipt"]
        assert verify_node_receipt(receipt) and receipt["verification"]["record_mac"] == "VERIFIED"
        assert receipt["semantic_authority"] is False and result.semantic_authority == "NONE"
        bound = required_execution(profile, REQUIREMENT)[0]
        assert bound is not None
        ran = bound.config.model_copy(update={"max_output_tokens": 256}).model_dump(mode="json")
        assert receipt["execution"] == ran and receipt["runtime"]["execution_requested"] == ran
        assert receipt["runtime"]["context_window_verified"] == bound.config.context_window == 8192
        assert receipt["runtime"]["backend"] == profile.binding.runtime_family
        (chat,) = rig.llama_chats()
        assert chat["model"] == profile.artifact.artifact_ref
        assert chat["chat_template_kwargs"] == {"enable_thinking": False}
        assert chat["response_format"]["type"] == "json_schema"
        assert chat["response_format"]["json_schema"]["schema"] == DOGFOOD_SCHEMA
        assert (chat["max_tokens"], chat["temperature"], chat["seed"]) == (256, 0.0, 42)
        assert not {"n_ctx", "num_ctx", "options", "think"} & set(chat)  # the window is verified, not requested


# --- 2: two measured profiles on two runtimes; the unchanged Worker Router selects --------------------------------

def test_two_profiles_on_two_runtimes_router_selects_deterministically_and_revocation_moves_on(tmp_path: Path) -> None:
    first = rehearsal_profile()  # runtime family #1 (as in Stage B)
    second, root, path = one_llama_model(tmp_path)  # another model on runtime family #2
    with stage_c(tmp_path, (first, second), serve=(second.artifact.artifact_ref, path), models_root=root,
                 with_ollama=True) as rig:
        refs = {first.profile_id: first.artifact.model_ref, second.profile_id: second.artifact.model_ref}
        rig.chain.write_registration(models=tuple(refs.values()))
        workers = rig.workers()
        assert {spec.availability for spec in workers.values()} == {WorkerAvailability.AVAILABLE}
        bridge = rig.chain.fabric()[2]
        choices = {bridge.select(rig.request()).selected_worker_id for _ in range(3)}
        assert len(choices) == 1  # deterministic: same inputs, same router decision
        (chosen,) = choices
        by_worker = {worker_id_for(p): p for p in (first, second)}
        assert chosen in by_worker
        selected = by_worker[chosen]
        plan, result, adapter = rig.plan_and_execute()
        assert result is not None and result.status is ExecutionStatus.COMPLETED, result
        assert adapter.executions[-1]["measured"].profile_id == selected.profile_id
        ran_on = (rig.ollama_chats(), len(rig.llama_chats()))
        assert ran_on == ((1, 0) if selected is first else (0, 1))

        other = second if selected is first else first  # revoke the selected model's explicit authorization
        rig.chain.write_registration(models=(refs[other.profile_id],))
        plan, result, adapter = rig.plan_and_execute()
        assert result is not None and result.status is ExecutionStatus.COMPLETED, result
        assert adapter.executions[-1]["measured"].profile_id == other.profile_id
        assert (rig.ollama_chats(), len(rig.llama_chats())) == (1, 1)  # the other runtime ran it this time

        rig.chain.write_registration(models=())  # empty allowed_models: DENY (never a wildcard)
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and plan.stages[-1].infeasible_code == "WORKER_ROUTER_BLOCKED" and result is None
        assert {spec.availability for spec in rig.workers().values()} == {WorkerAvailability.DISABLED}
        assert all("NOT_AUTHORIZED" in str(spec.reason) for spec in rig.workers().values())
        assert (rig.ollama_chats(), len(rig.llama_chats())) == (1, 1)


def test_empty_allowed_models_denies_the_second_runtime_too(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path)
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root) as rig:
        rig.chain.write_registration(models=())
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.llama_chats() == []


# --- 3: never routable without conditions or with unknown / invalid evidence --------------------------------------

def test_profile_without_execution_conditions_is_never_routable(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path, execution_conditions=())
    assert profile.execution_conditions == ()
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root) as rig:
        rig.chain.write_registration()  # explicitly authorized, live, bindable ...
        assert rig.chain.binding(profile).bindable
        (spec,) = rig.workers().values()
        assert spec.availability is WorkerAvailability.UNAVAILABLE  # ... and still never routable
        assert "MEASURED_EXECUTION_CONFIG_MISSING:STRUCTURED_JSON" in str(spec.reason)
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.llama_chats() == []


@pytest.mark.parametrize(("extract", "reason"), [
    (CapabilityObservation(capability=MeasuredCapability.INFORMATION_EXTRACTION,
                           status=ObservationStatus.INVALID_MEASUREMENT, unit="none"),
     "CAPABILITY_MEASUREMENT_INVALID:INFORMATION_EXTRACTION"),
    (None, "CAPABILITY_UNMEASURED:INFORMATION_EXTRACTION"),  # never measured at all
], ids=["invalid-measurement", "not-measured"])
def test_unknown_or_invalid_floor_is_never_routable_on_the_second_runtime(tmp_path: Path,
                                                                         extract: CapabilityObservation | None,
                                                                         reason: str) -> None:
    profile, root, path = one_llama_model(tmp_path, obs=observations() if extract is None
                                          else observations(extract=extract))
    assert profile.observation(MeasuredCapability.INFORMATION_EXTRACTION).status is not ObservationStatus.MEASURED
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root) as rig:
        rig.chain.write_registration()
        (spec,) = rig.workers().values()
        assert spec.availability is WorkerAvailability.UNAVAILABLE and reason in str(spec.reason)
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.llama_chats() == []


# --- 4: a third "model brand" is data only -----------------------------------------------------------------------

@pytest.mark.parametrize(("alias", "family"), [("zeta-instruct:2b", "brand-z"),
                                               ("vendor-q/coder-14b:q5_k_m", "another-vendor")])
def test_a_new_model_brand_label_needs_zero_code_change(tmp_path: Path, alias: str, family: str) -> None:
    profile, root, path = one_llama_model(tmp_path, alias=alias, model_family=family,
                                          obs=observations(extract=counted(
                                              MeasuredCapability.INFORMATION_EXTRACTION, 3, 3, lower=0.4385)))
    with stage_c(tmp_path, (profile,), serve=(alias, path), models_root=root) as rig:
        rig.chain.write_registration()
        plan, result, _ = rig.plan_and_execute()
        assert result is not None and result.status is ExecutionStatus.COMPLETED, result
        assert validate_extraction(rig.chain.output_bytes(result) or b"")["passed"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "pados").rglob("*.py")).lower()
    assert alias.lower() not in source and family.lower() not in source  # the label exists only as data


# --- 5: the context window on a runtime that fixes it at start ---------------------------------------------------

def test_mismatched_context_window_fails_closed_at_the_node_with_no_output_and_no_receipt(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path)
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root,
                 n_ctx=4096) as rig:  # measured under 8192; this server was started with 4096
        rig.chain.write_registration()
        plan, result, adapter = rig.plan_and_execute()
        assert plan.feasible and result is not None and result.status is ExecutionStatus.FAILED
        run = adapter.executions[-1]
        assert "receipt" not in run and run["refused"] == "JOB_FAILED:EXECUTION_CONFIG_MISMATCH:context_window"
        failure = run["failure"]
        assert failure["node_receipt"] == "NOT_ISSUED_FOR_FAILED_EXECUTION"
        assert failure["semantic_authority"] == "NONE" and failure["record_mac_verified"] is True
        assert failure["backend_error_detail"] == {"runtime_context_window": 4096, "measured_context_window": 8192}
        assert not any(result.outputs.values())
        assert rig.llama_chats() == []  # refused before any inference


def test_unknown_context_window_is_never_routed(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path)
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=root,
                 props_available=False) as rig:
        rig.chain.write_registration()
        binding = rig.chain.binding(profile)
        # without the server's properties neither the window nor the served file can be verified
        assert not binding.bindable and "MODEL_ARTIFACT_NOT_PRESENT" in binding.reasons
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.llama_chats() == []


def test_without_a_verifiable_digest_source_the_second_runtime_never_binds(tmp_path: Path) -> None:
    profile, root, path = one_llama_model(tmp_path)
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, path), models_root=None) as rig:
        binding = rig.chain.binding(profile)  # same alias, but no operator-configured root: no digest at all
        assert binding.runtime_ref is None and "MODEL_ARTIFACT_NOT_PRESENT" in binding.reasons
        rig.chain.write_registration()
        plan, result, _ = rig.plan_and_execute()
        assert not plan.feasible and result is None and rig.llama_chats() == []


def test_same_alias_serving_another_file_is_another_artifact(tmp_path: Path) -> None:
    profile, root, _ = one_llama_model(tmp_path)
    other = gguf_file(root, "swapped.gguf", "different-bytes")
    with stage_c(tmp_path, (profile,), serve=(profile.artifact.artifact_ref, other), models_root=root) as rig:
        binding = rig.chain.binding(profile)  # bound by DIGEST, never by name
        assert binding.runtime_ref is None and "MODEL_ARTIFACT_NOT_PRESENT" in binding.reasons
        rig.chain.write_registration()
        assert not rig.plan_and_execute()[0].feasible and rig.llama_chats() == []


# --- 6: static: protected authorities hold no model, vendor or runtime vocabulary -------------------------------

PROTECTED = ("capability/planner.py", "capability/executor.py", "capability/registry.py", "capability/inference.py",
             "worker/router.py", "core/scheduler.py", "loop/engine.py", "resources/models.py",
             "resources/capability_profile.py", "compute_node/provider.py", "packs/local_inference/adapter.py",
             "local_intelligence/live.py")
BRAND_AND_RUNTIME_TOKENS = ("qwen", "gemma", "gpt-oss", "llama", "ollama", "mlx", "vllm", "num_ctx", "n_ctx",
                            "num_predict", "enable_thinking", "chat_template", "reasoning_content", "/api/chat",
                            "/v1/chat/completions", "/props", ".gguf")
RUNTIME_PARAMETER_IDENTIFIERS = {"num_ctx", "n_ctx", "num_predict", "enable_thinking", "chat_template_kwargs",
                                 "think", "reasoning_content"}
# The frozen Worker Router's internal local-model worker CLASS (``WorkerType.OLLAMA``) predates this program; it is
# an enum member referenced by name, never a string the planner or router reads (see compute_node/public.py).
LEGACY_INTERNAL_WORKER_CLASS = "workertype.ollama"


@pytest.mark.parametrize("name", PROTECTED)
def test_protected_authorities_name_no_model_vendor_or_runtime(name: str) -> None:
    path = ROOT / "src" / "pados" / name
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    literals = " ".join(node.value.lower() for node in ast.walk(tree)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str))
    literals = literals.replace(f"``{LEGACY_INTERNAL_WORKER_CLASS}``", "")
    for token in BRAND_AND_RUNTIME_TOKENS:
        assert token not in literals, (name, token)
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not RUNTIME_PARAMETER_IDENTIFIERS & identifiers, name
    assert not {i for i in identifiers if i != "OLLAMA" and any(t in i.lower() for t in ("qwen", "gemma", "llama",
                                                                                          "mlx", "vllm"))}, name
