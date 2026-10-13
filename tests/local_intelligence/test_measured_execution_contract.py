"""Measured execution contract: CAPABILITY = MODEL x RUNTIME x DEVICE x EXECUTION CONFIG x EVIDENCE.

Regression for the first Windows M4.1 dogfood failure (CONTRACT_MISMATCH): the capability was measured with the
reasoning phase disabled, a fixed context window and schema-constrained output, but the live request reproduced
none of that, so a reasoning phase could spend the whole output budget before any visible content. The fake
runtime reproduces that failure deterministically (``reasoning_tokens``) without any model.
"""
from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
from capability_profile_support import measured_config  # noqa: E402
from live_chain_support import rehearsal_profile  # noqa: E402
from m4_1_live_chain import DOGFOOD_MESSAGES, DOGFOOD_SCHEMA, PROJECT, REQUIREMENT, validate_extraction  # noqa: E402
from test_m4_1_live_chain import Rig, make_rig  # noqa: E402

from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.compute_node.backends import (  # noqa: E402
    BackendError,
    DeterministicBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
    ollama_chat_body,
)
from pados.compute_node.jobs import ExecuteRequest  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.provider import LocalInferenceProvider, NodeExecutionError, RouteIntent  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.resources.capability_profile import (  # noqa: E402
    CandidateState,
    CapabilityFloor,
    CapabilityRequirement,
    ExecutionCompatibility,
    MeasuredCapability,
    MeasuredExecutionCondition,
    ModelCapabilityProfile,
    OutputConstraint,
    ReasoningMode,
    ResourceHeadroom,
    assess,
    check_execution,
    profile_id_for,
    required_execution,
)

ROOT = Path(__file__).resolve().parents[2]
ROOMY = ResourceHeadroom(free_gpu_memory_mib=8000, available_ram_mib=16000)
EXTRACTION = MeasuredCapability.INFORMATION_EXTRACTION


def condition(**overrides: Any) -> MeasuredExecutionCondition:
    return MeasuredExecutionCondition(capabilities=(EXTRACTION,), config=measured_config(EXTRACTION, **overrides),
                                      max_observed_output_tokens=70, sample_size=2, evidence_refs=("task",))


def rig_with(tmp_path: Path, **fake: object) -> Iterator[Rig]:
    yield from make_rig(tmp_path, **fake)


def route_and_execute(rig: Rig, inputs: list[dict[str, Any]], **kwargs: Any) -> Any:
    provider = rig.chain.provider()
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    return provider, route, provider.execute(route, run_id="r1", work_package_id="w", inputs=inputs,
                                             output_path="o.txt", allowed_paths=("o.txt",), timeout_seconds=10,
                                             **kwargs)


def chat_bodies(rig: Rig) -> list[dict[str, Any]]:
    return [r["body"] for r in rig.fake.requests if r["path"] == "/api/chat"]


# --- 1-2: the measured reasoning mode and context window are enforced -------------------------------------------

def test_measured_reasoning_mode_is_enforced() -> None:
    measured = condition()
    assert measured.config.reasoning_mode is ReasoningMode.DISABLED
    enabled = measured.config.model_copy(update={"reasoning_mode": ReasoningMode.ENABLED})
    state, reasons = check_execution(measured, enabled)
    assert state is ExecutionCompatibility.INCOMPATIBLE
    assert reasons == ("MEASURED_EXECUTION_CONFIG_MISMATCH:reasoning_mode",)
    assert check_execution(measured, measured.config) == (ExecutionCompatibility.COMPATIBLE, ())


def test_measured_context_window_is_enforced() -> None:
    measured = condition()
    for window in (4096, 16384):
        state, reasons = check_execution(measured, measured.config.model_copy(update={"context_window": window}))
        assert state is ExecutionCompatibility.INCOMPATIBLE
        assert reasons == ("MEASURED_EXECUTION_CONFIG_MISMATCH:context_window",)


def test_sampling_output_constraint_and_budget_floor_are_enforced() -> None:
    measured = condition()
    cases = {"temperature": 0.7, "seed": 7, "output_constraint": OutputConstraint.UNCONSTRAINED,
             "max_output_tokens": 69}
    for name, value in cases.items():
        state, reasons = check_execution(measured, measured.config.model_copy(update={name: value}))
        assert state is ExecutionCompatibility.INCOMPATIBLE and reasons == (
            f"MEASURED_EXECUTION_CONFIG_MISMATCH:{name}",), name
    # a smaller budget than the measured one is fine while it covers the largest measured completion
    assert check_execution(measured, measured.config.model_copy(update={"max_output_tokens": 256}))[0] \
        is ExecutionCompatibility.COMPATIBLE
    assert check_execution(None, measured.config)[0] is ExecutionCompatibility.MISSING
    assert check_execution(measured, None)[0] is ExecutionCompatibility.MISSING


# --- 3: incompatible / missing / inconsistent configuration fails closed ----------------------------------------

def test_profile_without_measured_conditions_keeps_identity_but_supports_nothing() -> None:
    profile = rehearsal_profile()
    legacy_body = profile.model_dump(mode="json", exclude={"profile_id"})
    legacy_body["execution_conditions"] = []
    legacy = ModelCapabilityProfile.model_validate({**legacy_body, "profile_id": profile_id_for(legacy_body)})
    del legacy_body["execution_conditions"]  # bodies written before the field existed hash identically
    assert legacy.profile_id == profile_id_for(legacy_body) != profile.profile_id
    result = assess(legacy, REQUIREMENT, runtime_state=CandidateState.AVAILABLE,
                    device_state=CandidateState.AVAILABLE, headroom=ROOMY)
    assert result.state is CandidateState.UNAVAILABLE
    assert result.reasons == ("MEASURED_EXECUTION_CONFIG_MISSING:STRUCTURED_JSON",)
    assert required_execution(legacy, REQUIREMENT) == (None, "MEASURED_EXECUTION_CONFIG_MISSING:STRUCTURED_JSON")


M3_REPORT = ROOT / "runtime/artifacts/reports/stage-b-local-intelligence-v1/closure/M3_MODEL_CAPABILITY_PROFILE.json"


@pytest.mark.skipif(not M3_REPORT.is_file(), reason="internal evidence: runtime/** is not part of the public export")
def test_the_accepted_m3_profile_still_loads_with_its_id_and_is_refused() -> None:
    report = json.loads(M3_REPORT.read_text(encoding="utf-8"))
    old = ModelCapabilityProfile.model_validate(report["profile"])
    assert old.profile_id == "mcp-cebf3bf408559ba915e9331ec9daf3964d71d725a031500d4442cba50b3e857e"
    assert old.execution_conditions == ()
    assert required_execution(old, REQUIREMENT)[1] == "MEASURED_EXECUTION_CONFIG_MISSING:STRUCTURED_JSON"


def test_capabilities_measured_under_different_conditions_are_inconsistent() -> None:
    requirement = CapabilityRequirement(floors=(
        CapabilityFloor(capability=MeasuredCapability.STRUCTURED_JSON, min_success_lower_bound=0.2),
        CapabilityFloor(capability=MeasuredCapability.TEXT_GENERATION_EN, min_success_lower_bound=0.2)))
    profile = rehearsal_profile()  # STRUCTURED_JSON measured schema-constrained, TEXT_GENERATION_EN unconstrained
    assert required_execution(profile, requirement) == (None, "MEASURED_EXECUTION_CONFIG_INCONSISTENT")
    result = assess(profile, requirement, runtime_state=CandidateState.AVAILABLE,
                    device_state=CandidateState.AVAILABLE, headroom=ROOMY)
    assert result.state is CandidateState.UNAVAILABLE and "MEASURED_EXECUTION_CONFIG_INCONSISTENT" in result.reasons


def test_condition_for_an_unmeasured_capability_is_refused() -> None:
    profile = rehearsal_profile()
    body = profile.model_dump(mode="json", exclude={"profile_id"})
    body["execution_conditions"] = [{**body["execution_conditions"][0], "capabilities": ["CODE_REPAIR"]}]
    with pytest.raises(ValueError, match="EXECUTION_CONDITION_FOR_UNMEASURED_CAPABILITY"):
        ModelCapabilityProfile.model_validate({**body, "profile_id": profile_id_for(body)})


def test_incompatible_execution_fails_closed_before_the_node_is_contacted(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path):
        rig.chain.write_registration()
        with pytest.raises(NodeExecutionError,
                           match="^EXECUTION_GATE_REFUSED:MEASURED_EXECUTION_CONFIG_MISMATCH:output_constraint$"):
            route_and_execute(rig, [{"messages": DOGFOOD_MESSAGES}])  # schema missing: not how it was measured
        with pytest.raises(NodeExecutionError,
                           match="^EXECUTION_GATE_REFUSED:MEASURED_EXECUTION_CONFIG_MISMATCH:max_output_tokens$"):
            route_and_execute(rig, [{"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}],
                              max_output_tokens=16)  # below the largest measured completion
        assert chat_bodies(rig) == []  # nothing reached the runtime


# --- 4-5: compatible generic configuration -> runtime request; no bypass -----------------------------------------

def test_compatible_generic_config_translates_into_the_runtime_request(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path):
        rig.chain.write_registration()
        _, route, execution = route_and_execute(
            rig, [{"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}], max_output_tokens=256)
        (body,) = chat_bodies(rig)
        assert body["think"] is False and body["format"] == DOGFOOD_SCHEMA
        assert body["options"] == {"num_ctx": 8192, "num_predict": 256, "temperature": 0.0, "seed": 42}
        bound = route.measured.execution.config
        assert execution.record["execution"] == bound.model_copy(update={"max_output_tokens": 256}).model_dump(
            mode="json")
        assert execution.receipt["execution"] == execution.record["execution"]
        assert execution.record["runtime"]["execution_requested"] == execution.record["execution"]
        assert len(execution.record["runtime"]["response_schema_sha256"]) == 64


def test_ollama_translation_is_isolated_and_legacy_shape_is_unchanged() -> None:
    config = measured_config(EXTRACTION, max_output_tokens=300)
    body = ollama_chat_body("m", [{"role": "user", "content": "x"}], max_output_tokens=300, execution=config,
                            schema={"type": "object"})
    assert body == {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": False, "think": False,
                    "options": {"num_ctx": 8192, "num_predict": 300, "temperature": 0.0, "seed": 42},
                    "format": {"type": "object"}}
    enabled = config.model_copy(update={"reasoning_mode": ReasoningMode.ENABLED,
                                        "output_constraint": OutputConstraint.UNCONSTRAINED})
    assert ollama_chat_body("m", [], max_output_tokens=300, execution=enabled, schema=None)["think"] is True
    assert ollama_chat_body("m", [], max_output_tokens=256, execution=None, schema=None) == {
        "model": "m", "messages": [], "stream": False, "options": {"num_predict": 256, "temperature": 0}}


def test_provider_has_no_configuration_override_and_requires_the_node_to_echo_it(tmp_path: Path,
                                                                                 monkeypatch: pytest.MonkeyPatch
                                                                                 ) -> None:
    import inspect

    assert "execution" not in inspect.signature(LocalInferenceProvider.execute).parameters
    for rig in rig_with(tmp_path):
        rig.chain.write_registration()
        provider = rig.chain.provider()
        route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
        backend = rig.chain.node_runtime.backends[rig.chain.profile.binding.runtime_family]
        original = backend.run

        def ignores_configuration(*args: Any, **kwargs: Any) -> Any:  # a runtime that silently drops it
            out = original(*args, **kwargs)
            return type(out)(out.content, {k: v for k, v in out.runtime.items() if k != "execution_requested"})

        monkeypatch.setattr(backend, "run", ignores_configuration)
        with pytest.raises(NodeExecutionError, match="^MEASURED_EXECUTION_CONFIG_NOT_APPLIED$") as refused:
            provider.execute(route, run_id="r1", work_package_id="w",
                             inputs=[{"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}],
                             output_path="o.txt", allowed_paths=("o.txt",), timeout_seconds=10)
        assert refused.value.failure is not None
        assert refused.value.failure["node_receipt"] == "NOT_ISSUED_FOR_FAILED_EXECUTION"


def test_runtime_that_reasons_despite_the_switch_is_refused(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path, reasoning_tokens=50, thinking_despite_disabled=True):
        rig.chain.write_registration()
        with pytest.raises(NodeExecutionError,
                           match="^JOB_FAILED:EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode$") as refused:
            route_and_execute(rig, [{"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}])
        assert refused.value.failure["backend_error_detail"]["thinking_chars"] == 50


def test_backends_that_cannot_apply_the_configuration_refuse_it() -> None:
    config = measured_config(EXTRACTION, max_output_tokens=64)
    item = {"messages": [{"role": "user", "content": "x"}], "response_schema": {"type": "object"}}
    with pytest.raises(BackendError, match="EXECUTION_CONFIG_UNVERIFIABLE"):
        OpenAICompatibleBackend("o", "http://127.0.0.1:1", transport=lambda *a: (200, {})).run(
            NodeCapability.LOCAL_INFERENCE, "m", item, max_output_tokens=64, timeout_seconds=1, execution=config)
    with pytest.raises(BackendError, match="EXECUTION_CONFIG_BUDGET_CONFLICT"):
        OllamaBackend("l", "http://127.0.0.1:1", transport=lambda *a: (200, {})).run(
            NodeCapability.LOCAL_INFERENCE, "m", item, max_output_tokens=65, timeout_seconds=1, execution=config)
    with pytest.raises(BackendError, match="INPUT_INVALID"):  # schema-constrained without a schema
        DeterministicBackend().run(NodeCapability.LOCAL_INFERENCE, DeterministicBackend.MODEL,
                                   {"text": "x"}, max_output_tokens=64, timeout_seconds=1, execution=config)
    with pytest.raises(ValueError):  # the node refuses a request whose budget contradicts its configuration
        ExecuteRequest.model_validate({"request_id": "r", "work_package_id": "w", "run_id": "r",
                                       "capability": "LOCAL_INFERENCE", "model_id": "m", "classification": "INTERNAL",
                                       "inputs": [item], "output_path": "o", "execution": {"context_window": 0}})


# --- 6: planner-facing contracts stay model- and runtime-agnostic ------------------------------------------------

PLANNER_FACING = ("resources/capability_profile.py", "capability/inference.py", "capability/planner.py",
                  "packs/local_inference/adapter.py", "providers/execution_binding.py", "graph/cognition.py")


def test_planner_facing_contracts_name_no_model_or_runtime_parameter() -> None:
    root = ROOT / "src" / "pados"
    for name in PLANNER_FACING:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        literals = " ".join(node.value.lower() for node in ast.walk(tree)
                            if isinstance(node, ast.Constant) and isinstance(node.value, str))
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for token in ("qwen", "ollama", "num_ctx", "num_predict", "/api/chat"):
            assert token not in literals, (name, token)
        assert not {"num_ctx", "num_predict", "think"} & identifiers, name


def test_replacement_artifact_binds_through_the_same_generic_condition() -> None:
    a = rehearsal_profile()
    b = rehearsal_profile(artifact_ref="another-model:7b", runtime_family="runtime-y", model_family="family-y",
                          manifest_seed="7")
    ca, cb = required_execution(a, REQUIREMENT)[0], required_execution(b, REQUIREMENT)[0]
    assert ca is not None and cb is not None and ca.config == cb.config and a.profile_id != b.profile_id


# --- 7-11: failure evidence, no receipt, no authority, reproduction, compatible success -------------------------

def test_fake_runtime_reproduces_the_empty_output_failure_of_an_unbound_request(tmp_path: Path) -> None:
    """The pre-fix request shape (budget only, reasoning not disabled, no context window) against a runtime whose
    reasoning phase is longer than the budget: no visible content, a precise backend code and counters."""
    for rig in rig_with(tmp_path, reasoning_tokens=400):
        backend = rig.chain.node_runtime.backends[rig.chain.profile.binding.runtime_family]
        model = rig.chain.node_runtime.models()[0][0].backend_model
        with pytest.raises(BackendError) as failed:
            backend.run(NodeCapability.LOCAL_INFERENCE, model, {"messages": DOGFOOD_MESSAGES},
                        max_output_tokens=256, timeout_seconds=10)
        assert failed.value.code == "EMPTY_COMPLETION"
        assert failed.value.detail == {"done_reason": "length", "eval_count": 256, "thinking_chars": 256,
                                       "content_chars": 0, "prompt_eval_count": failed.value.detail[
                                           "prompt_eval_count"]}
        (body,) = chat_bodies(rig)
        assert "think" not in body and "num_ctx" not in body["options"]  # the measured conditions were absent


def test_compatible_execution_produces_the_structured_extraction_on_the_same_runtime(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path, reasoning_tokens=400):
        rig.chain.write_registration()
        plan, result, adapter, *_ = rig.plan_and_execute()
        assert plan.feasible and result is not None and result.status is ExecutionStatus.COMPLETED, result
        assert validate_extraction(rig.chain.output_bytes(result) or b"")["passed"]
        (body,) = chat_bodies(rig)
        assert body["think"] is False and body["options"]["num_ctx"] == 8192 and body["options"]["num_predict"] == 256
        assert adapter.executions[-1]["receipt"]["execution"]["reasoning_mode"] == "DISABLED"


def test_failed_node_execution_keeps_complete_evidence_and_never_a_receipt(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path, empty_content=True):
        rig.chain.write_registration()
        plan, result, adapter, *_ = rig.plan_and_execute()
        assert plan.feasible and result is not None and result.status is ExecutionStatus.FAILED
        run = adapter.executions[-1]
        assert "receipt" not in run and run["refused"] == "JOB_FAILED:EMPTY_COMPLETION"
        failure = run["failure"]
        assert failure["failure_code"] == "JOB_FAILED:EMPTY_COMPLETION"
        assert failure["backend_error_code"] == "EMPTY_COMPLETION" and failure["job_state"] == "FAILED"
        assert failure["backend_error_detail"]["done_reason"] == "stop"
        assert failure["record_mac_verified"] is True and failure["job_id"].startswith("job-")
        assert failure["request_id"] == failure["signed_record"]["request_id"]
        assert failure["signed_record"]["record_mac"] and failure["signed_record"]["artifacts"] == []
        assert failure["node_receipt"] == "NOT_ISSUED_FOR_FAILED_EXECUTION"
        assert failure["semantic_authority"] == "NONE"
        assert failure["execution"]["reasoning_mode"] == "DISABLED"
        assert result.semantic_authority == "NONE" and result.plan_receipt.semantic_authority == "NONE"
        assert all(stage.semantic_authority == "NONE" for stage in result.plan_receipt.stages)
        assert result.outputs == {} or not any(result.outputs.values())
        text = json.dumps(failure, default=str)
        for leaked in (rig.chain.credential, "Invoice INV-88213", "X-Amz-Signature"):
            assert leaked not in text


def test_independent_verifier_rederives_the_condition_and_rejects_a_mismatched_run(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts" / "stage_b"))
    from m4_1_verify_dogfood import _execution_checks

    from pados.local_intelligence.catalog_store import ModelCatalogStore
    from live_chain_support import persist

    profile = rehearsal_profile()
    persist(tmp_path / "catalog.json", profile)
    files = {"state/model-catalog.json": (tmp_path / "catalog.json").read_bytes()}
    assert ModelCatalogStore(tmp_path / "catalog.json").load().capability_profiles()[0] == profile
    bound = required_execution(profile, REQUIREMENT)[0]
    assert bound is not None
    ran = bound.config.model_copy(update={"max_output_tokens": 256}).model_dump(mode="json")
    good = {"status": "COMPLETED", "node_record_execution": ran, "node_failure": None, "node_refusal": None,
            "node_execution_receipt": {"execution": ran, "runtime": {"execution_requested": ran}}}
    route = {"execution": bound.model_dump(mode="json")}
    assert all(_execution_checks(files, good, route).values())
    thinking = {**ran, "reasoning_mode": "ENABLED"}
    bad = {**good, "node_record_execution": thinking,
           "node_execution_receipt": {"execution": thinking, "runtime": {"execution_requested": thinking}}}
    assert _execution_checks(files, bad, route)["node_ran_measured_execution"] is False
    unbound = {"execution": None}
    assert _execution_checks(files, good, unbound)["route_binds_measured_execution"] is False
    failed = {"status": "FAILED", "node_failure": None, "node_execution_receipt": None}
    assert _execution_checks(files, failed, route)["failure_evidence_consistent"] is False


def test_an_answer_cut_off_by_the_budget_is_refused_not_accepted(tmp_path: Path) -> None:
    for rig in rig_with(tmp_path, answer_tokens=300):
        rig.chain.write_registration()
        with pytest.raises(NodeExecutionError, match="^JOB_FAILED:OUTPUT_TRUNCATED$") as refused:
            route_and_execute(rig, [{"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}],
                              max_output_tokens=256)
        assert refused.value.failure["backend_error_detail"]["done_reason"] == "length"
        assert refused.value.failure["node_receipt"] == "NOT_ISSUED_FOR_FAILED_EXECUTION"


def test_failure_evidence_never_embeds_an_unverified_record_and_bounds_node_codes() -> None:
    from pados.compute_node.provider import _failure

    forged = {"job_id": "job-1", "state": "FAILED", "error_code": "X" * 500 + " <script>leak</script>",
              "error_detail": {"done_reason": "length", "eval_count": 5, "note": "free text the node made up",
                               "content_chars": "not-an-int"}, "artifacts": [], "record_mac": "f" * 64}
    unverified = _failure({"request_id": "r", "execution": None}, forged, "RECORD_MAC_INVALID",
                          "NODE_RECEIPT_INVALID")
    assert unverified["signed_record"] is None and unverified["signed_record_sha256"] is None
    assert unverified["record_mac_verified"] is False
    assert unverified["backend_error_detail"] == {"done_reason": "length", "eval_count": 5}
    assert len(unverified["backend_error_code"]) == 160 and "<" not in unverified["backend_error_code"]
    verified = _failure({"request_id": "r", "execution": None}, {**forged, "error_code": "EMPTY_COMPLETION"},
                        "FAILED", "JOB_FAILED:EMPTY_COMPLETION", mac_verified=True)
    assert verified["signed_record"]["record_mac"] == "f" * 64 and len(verified["signed_record_sha256"]) == 64
    huge = _failure({"request_id": "r"}, {**forged, "artifacts": ["x" * 40_000]}, "FAILED", "JOB_FAILED:X",
                    mac_verified=True)
    assert huge["signed_record"] is None and huge["signed_record_sha256"] is not None  # bounded: hash only
