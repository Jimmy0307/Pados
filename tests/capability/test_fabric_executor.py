"""Executor: TOCTOU re-checks, scoped inputs, fail-closed semantics, explicit fallback, chained receipts."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from fabric_support import (
    OPERATOR,
    Delegate,
    Inventory,
    NondeterministicAdapter,
    TransformAdapter,
    adapter_descriptor,
    fabric,
    with_model,
)

import pados.capability.executor as executor_module
from pados.capability.adapter import StageContext, StageOutput, StageRequest
from pados.capability.executor import NOT_EXECUTED, ExecutionRefused, ExecutionStatus
from pados.capability.model import FailureSemantics, LifecycleState, VerificationMethod
from pados.capability.receipts import VerificationStatus
from pados.capability.task import QualityRequirement


def test_happy_path_produces_a_verified_chained_receipt_but_no_acceptance(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    result = fab.executor().execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED and result.failure_code is None
    receipt = result.plan_receipt
    assert receipt.verify_structure() and receipt.expected_stage_ids == ("ocr", "chunk")
    assert receipt.semantic_authority == "NONE" and receipt.human_decision_ref is None
    assert result.semantic_authority == "NONE"
    assert not {"accepted", "acceptance", "human_acceptance"} & set(receipt.model_dump())
    ocr, chunk = receipt.stages
    assert ocr.verification is VerificationStatus.PASSED and ocr.verification_method is VerificationMethod.SCHEMA
    assert ocr.verifier_id and ocr.verification_evidence_hash
    assert chunk.verification is VerificationStatus.PASSED
    assert chunk.verification_method is VerificationMethod.DETERMINISTIC_REPLAY
    assert chunk.inputs == ocr.outputs  # downstream reads exactly the upstream outputs of THIS run
    assert ocr.inputs == (fab.scan,) and ocr.resource_id == "unassigned"
    assert ocr.policy_decision_hash == plan.stages[0].decision_hash
    assert ocr.attempt_id == "run00001.ocr.1" and ocr.metrics == {"items": 1.0}
    assert fab.store.get(chunk.outputs[0]) == b"SCANNED PAGE|"
    assert result.outputs["chunk"] == chunk.outputs



def test_state_changed_between_plan_and_execute_refuses_before_any_adapter_runs(tmp_path: Path) -> None:
    """A plan is re-derived from CURRENT trusted state; revocation since planning makes it irreproducible."""
    changes = {
        "grant": lambda fab: fab.grants.grant("TEXT_CHUNK"),
        "grant_document": lambda fab: fab.grants.grants[("proj-a", "wc-1")].update(approval_ref="another"),
        "adapter_lifecycle": lambda fab: fab.registry.set_adapter_lifecycle(
            "chunk.fixed", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
        "capability_lifecycle": lambda fab: fab.registry.set_capability_lifecycle(
            "DOCUMENT_OCR", LifecycleState.DEPRECATED, operator=OPERATOR, reason_code="SUPERSEDED"),
    }
    for name, change in changes.items():
        fab = fabric(tmp_path / name)
        task = fab.task()
        plan = fab.planner().plan(task)
        change(fab)
        with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
            fab.executor().execute(plan, task)
        assert not fab.adapters["ocr.alpha"].calls and not fab.adapters["chunk.fixed"].calls, name


def test_grant_revoked_during_the_run_is_refused_at_the_next_stage(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    fab.adapters["ocr.alpha"].hook = lambda: fab.grants.grant("DOCUMENT_OCR")  # TEXT_CHUNK revoked meanwhile
    result = fab.executor().execute(plan, task)
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_code == "chunk:AUTHORIZATION_REVOKED_BEFORE_EXECUTION"
    ocr, chunk = result.plan_receipt.stages
    assert ocr.outputs and chunk.outputs == () and result.plan_receipt.verify_structure()
    assert not fab.adapters["chunk.fixed"].calls


def test_lifecycle_or_grant_document_changed_during_the_run_is_refused(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    original = executor_module._Run.stage

    def stage(self: Any, planned: Any) -> Any:  # between stages: outside any dispatch
        if planned.stage_id == "chunk":
            fab.registry.set_adapter_lifecycle("chunk.fixed", LifecycleState.REVOKED, operator=OPERATOR,
                                               reason_code="SECURITY_ADVISORY")
        return original(self, planned)

    monkeypatch.setattr(executor_module._Run, "stage", stage)
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "chunk:POLICY_LIFECYCLE_NOT_PLANNABLE"
    assert fab.adapters["ocr.alpha"].calls and not fab.adapters["chunk.fixed"].calls
    monkeypatch.setattr(executor_module._Run, "stage", original)
    # The same grant-document change made WHILE the OCR call runs withholds that call's outputs (CF-03).
    fab2 = fabric(tmp_path / "second")
    plan2 = fab2.planner().plan(fab2.task())
    fab2.adapters["ocr.alpha"].hook = lambda: fab2.grants.grants[("proj-a", "wc-1")].update(approval_ref="x2")
    result = fab2.executor().execute(plan2, fab2.task())
    assert result.failure_code == "ocr:REVOKED_DURING_EXECUTION"
    assert result.plan_receipt.stages[0].outputs == () and not fab2.adapters["chunk.fixed"].calls


def test_tampered_infeasible_or_mismatched_plans_are_refused(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    forged_stage = dataclasses.replace(plan.stages[0], selected_adapter_id="ocr.other")
    with pytest.raises(ExecutionRefused, match="PLAN_HASH_MISMATCH"):
        fab.executor().execute(dataclasses.replace(plan, stages=(forged_stage, plan.stages[1])), task)
    with pytest.raises(ExecutionRefused, match="PLAN_TASK_MISMATCH"):
        fab.executor().execute(plan, fab.task(parameters={"lang": "fr"}))
    fab.grants.grants.clear()
    infeasible = fab.planner().plan(task)
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_FEASIBLE"):
        fab.executor().execute(infeasible, task)


class Snooping(TransformAdapter):
    target: Any = None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        context.artifacts.get(self.target)
        return super().execute(request, context=context)


def test_adapter_reads_only_permitted_inputs(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    other = fab.store.put(b"another project's document", media_type="text/plain", kind="doc.scan")
    fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.DEPRECATED, operator=OPERATOR,
                                       reason_code="SUPERSEDED")
    snoop = Snooping(adapter_descriptor("ocr.snoop", "DOCUMENT_OCR"))
    snoop.target = other
    fab.add(snoop)
    task = fab.task()
    plan = fab.planner(config=_prefer("ocr.snoop")).plan(task)
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "ocr:ADAPTER_ARTIFACT_ACCESS_DENIED"
    assert result.plan_receipt.stages[0].outputs == ()


def _prefer(adapter_id: str) -> Any:
    from pados.capability.planner import PlannerConfig
    return PlannerConfig(preferences={"DOCUMENT_OCR": (adapter_id,)})


def test_adapter_failure_fails_closed_without_implicit_fallback(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.adapters["ocr.alpha"].fail_with = "ENGINE_CRASHED"
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    task = fab.task()
    result = fab.executor().execute(fab.planner().plan(task), task)
    assert result.failure_code == "ocr:ENGINE_CRASHED"
    assert not fab.adapters["ocr.beta"].calls  # capability did not opt in: no fallback
    assert result.plan_receipt.stages[0].outputs == () and result.plan_receipt.verify_structure()


def test_explicit_same_policy_fallback_is_recorded(tmp_path: Path) -> None:
    fab = fabric(tmp_path, ocr_fallback=True)
    fab.adapters["ocr.alpha"].fail_with = "ENGINE_CRASHED"
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    task = fab.task()
    plan = fab.planner().plan(task)
    assert plan.stages[0].alternatives == ("ocr.beta",)
    result = fab.executor().execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    ocr = result.plan_receipt.stages[0]
    assert ocr.adapter_id == "ocr.beta" and ocr.fallback_transition_hash and ocr.attempt_id.endswith(".2")
    assert [(a.adapter_id, a.failure_code) for a in result.attempts if a.stage_id == "ocr"] == [
        ("ocr.alpha", "ENGINE_CRASHED"), ("ocr.beta", None)]


def test_fallback_alternative_is_revalidated_at_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path, ocr_fallback=True)
    fab.adapters["ocr.alpha"].fail_with = "ENGINE_CRASHED"
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    task = fab.task()
    plan = fab.planner().plan(task)
    original = executor_module._Run.fallback

    def fallback(self: Any, stage: Any, primary: Any, alternative: str) -> Any:  # after the primary failed
        fab.registry.set_adapter_lifecycle("ocr.beta", LifecycleState.REVOKED, operator=OPERATOR,
                                           reason_code="SECURITY_ADVISORY")
        return original(self, stage, primary, alternative)

    monkeypatch.setattr(executor_module._Run, "fallback", fallback)
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "ocr:ENGINE_CRASHED" and not fab.adapters["ocr.beta"].calls
    assert ("ocr.beta", "POLICY_LIFECYCLE_NOT_PLANNABLE") in [(a.adapter_id, a.failure_code)
                                                               for a in result.attempts]


def test_registry_mutation_from_inside_the_primary_is_refused_and_never_falls_back(tmp_path: Path) -> None:
    fab = fabric(tmp_path, ocr_fallback=True)
    fab.adapters["ocr.alpha"].fail_with = "ENGINE_CRASHED"
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    task = fab.task()
    plan = fab.planner().plan(task)
    fab.adapters["ocr.alpha"].hook = lambda: fab.registry.set_adapter_lifecycle(
        "ocr.beta", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY")
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "ocr:REGISTRY_MUTATION_DURING_DISPATCH" and not fab.adapters["ocr.beta"].calls
    assert fab.registry.adapter("ocr.beta").recorded_descriptor().lifecycle is LifecycleState.ACTIVE


def test_partial_output_semantics(tmp_path: Path) -> None:
    fab = fabric(tmp_path, ocr_failure=FailureSemantics.PARTIAL_WITH_EVIDENCE)
    fab.adapters["ocr.alpha"].partial = True
    task = fab.task()
    result = fab.executor().execute(fab.planner().plan(task), task)
    ocr, chunk = result.plan_receipt.stages
    assert ocr.partial and ocr.failure_code == "PARTIAL_OUTPUT" and len(ocr.outputs) == 1
    assert ocr.verification is VerificationStatus.NOT_VERIFIED and chunk.failure_code == NOT_EXECUTED
    closed = fabric(tmp_path / "closed")
    closed.adapters["ocr.alpha"].partial = True
    task = closed.task()
    ocr = closed.executor().execute(closed.planner().plan(task), task).plan_receipt.stages[0]
    assert ocr.failure_code == "PARTIAL_OUTPUT_NOT_PERMITTED" and ocr.outputs == () and not ocr.partial


def test_failed_verification_withholds_outputs(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.adapters["ocr.alpha"].suffix = b" lower"
    task = fab.task()
    result = fab.executor().execute(fab.planner().plan(task), task)
    ocr = result.plan_receipt.stages[0]
    assert ocr.failure_code == "VERIFICATION_FAILED" and ocr.verification is VerificationStatus.FAILED
    assert ocr.outputs == () and result.failure_code == "ocr:VERIFICATION_FAILED"


def test_verified_quality_requires_passing_replay(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.registry.set_adapter_lifecycle("chunk.fixed", LifecycleState.DEPRECATED, operator=OPERATOR,
                                       reason_code="SUPERSEDED")
    fab.add(NondeterministicAdapter(adapter_descriptor("chunk.flaky", "TEXT_CHUNK"), kind="text.chunk"))
    task = fab.task(quality=QualityRequirement.VERIFIED)
    from pados.capability.planner import PlannerConfig
    plan = fab.planner(config=PlannerConfig(preferences={"TEXT_CHUNK": ("chunk.flaky",)})).plan(task)
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "chunk:VERIFICATION_FAILED"  # replay produced different bytes
    ok = fabric(tmp_path / "ok")
    task = ok.task(quality=QualityRequirement.VERIFIED)
    assert ok.executor().execute(ok.planner().plan(task), task).status is ExecutionStatus.COMPLETED



def test_resource_reservation_is_recorded_and_rechecked(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    inventory = Inventory()
    task = fab.task()
    plan = fab.planner(inventory=inventory).plan(task)
    result = fab.executor(inventory=inventory).execute(plan, task)
    assert {s.resource_id for s in result.plan_receipt.stages} == {"local-cpu"}
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):  # inventory removed
        fab.executor().execute(plan, task)
    fab.adapters["ocr.alpha"].hook = lambda: inventory.fits.update({"local-cpu": False, "gpu-node": True})
    chunk_calls = len(fab.adapters["chunk.fixed"].calls)
    result = fab.executor(inventory=inventory).execute(plan, task)
    assert result.failure_code == "ocr:REVOKED_DURING_EXECUTION"  # the reservation vanished during the call
    assert result.plan_receipt.stages[0].outputs == () and len(fab.adapters["chunk.fixed"].calls) == chunk_calls
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):  # no longer fits at all
        fab.executor(inventory=inventory).execute(plan, task)


def test_model_stage_records_resolved_worker_router_evidence(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    delegate = Delegate()
    plan = fab.planner(inference=delegate).plan(task)
    result = fab.executor(inference=delegate).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED  # execution complete != semantically accepted
    summary = result.plan_receipt.stages[2]
    assert summary.router_selection_hash == plan.stages[2].router_selection_hash
    assert result.router_evidence[summary.router_selection_hash].selected_worker_id == "ollama-local"
    assert summary.verification is VerificationStatus.NOT_VERIFIED  # HUMAN_REVIEW happens downstream
    assert fab.adapters["llm.bridge"].calls[0].router_selection_hash == summary.router_selection_hash


def test_model_stage_refuses_when_router_selection_changes(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    delegate = Delegate()
    plan = fab.planner(inference=delegate).plan(task)
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.executor(inference=Delegate(worker="claude-remote")).execute(plan, task)
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.executor().execute(plan, task)  # no delegate: the model stage cannot be re-derived
    fab.adapters["chunk.fixed"].hook = lambda: setattr(delegate, "worker", "claude-remote")
    result = fab.executor(inference=delegate).execute(plan, task)
    assert result.failure_code == "summary:WORKER_ROUTER_SELECTION_CHANGED"
    assert not fab.adapters["llm.bridge"].calls
