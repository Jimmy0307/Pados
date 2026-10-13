"""Planner: policy-feasible selection, explicit ordering, bounded opt-in advice, router delegation."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fabric_support import (
    OPERATOR,
    Advisor,
    Delegate,
    Inventory,
    TransformAdapter,
    adapter_descriptor,
    fabric,
    with_model,
)

from pados.capability.model import LifecycleState
from pados.capability.planner import PlannerConfig, PlanStatus, plan_hash_of
from pados.capability.seams import ADVICE_BOUND
from pados.capability.task import QualityRequirement
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification


def test_feasible_plan_is_content_addressed_and_deterministic(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    assert plan.status is PlanStatus.FEASIBLE and plan.stage_ids == ("ocr", "chunk")
    assert [s.selected_adapter_id for s in plan.stages] == ["ocr.alpha", "chunk.fixed"]
    assert plan.plan_hash == plan_hash_of(plan) and plan.plan_id == f"plan-{plan.plan_hash[:24]}"
    assert fab.planner().plan(fab.task()).plan_hash == plan.plan_hash
    assert fab.planner().plan(fab.task(task_id="task-2")).plan_hash != plan.plan_hash
    # Planning never executes adapter code (metadata cannot trigger execution).
    assert all(not impl.calls for impl in fab.adapters.values())


def test_unauthorized_capability_makes_the_plan_infeasible_with_reasons(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.grants.grant("DOCUMENT_OCR")  # TEXT_CHUNK no longer granted
    probe = fab.adapters["chunk.fixed"]
    probe.ready = True
    plan = fab.planner().plan(fab.task())
    assert plan.status is PlanStatus.INFEASIBLE
    assert "chunk:NO_FEASIBLE_CANDIDATE" in plan.infeasible_codes
    chunk = plan.stages[1]
    assert chunk.selected_adapter_id is None
    assert [(c.adapter_id, c.code) for c in chunk.candidates] == [("chunk.fixed", "NOT_AUTHORIZED")]
    assert chunk.candidates[0].decision_hash and "NOT_AUTHORIZED" in chunk.candidates[0].failing_checks


def test_missing_grant_and_foreign_project_grant_fail_closed(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.grants.grants.clear()
    plan = fab.planner().plan(fab.task())
    assert plan.status is PlanStatus.INFEASIBLE and "ocr:GRANT_NOT_FOUND" in plan.infeasible_codes
    fab.grants.grants[("proj-a", "wc-1")] = {"work_contract_id": "wc-1", "project_id": "proj-b",
                                             "capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
    plan = fab.planner().plan(fab.task())
    assert "ocr:GRANT_SCOPE_MISMATCH" in plan.infeasible_codes


def test_no_implicit_fallback_from_local_to_cloud(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.adapters["ocr.alpha"].ready = False
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    plan = fab.planner().plan(fab.task())
    assert plan.status is PlanStatus.INFEASIBLE
    codes = {c.adapter_id: c.code for c in plan.stages[0].candidates}
    assert codes == {"ocr.alpha": "ADAPTER_UNAVAILABLE", "ocr.cloud": "NETWORK_NOT_PERMITTED"}
    assert not fab.adapters["ocr.cloud"].calls


def test_classification_and_lifecycle_filter_candidates(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.public", "DOCUMENT_OCR",
                                                max_classification=DataClassification.PUBLIC)))
    plan = fab.planner().plan(fab.task())
    codes = {c.adapter_id: c.code for c in plan.stages[0].candidates}
    assert codes["ocr.public"] == "CLASSIFICATION_EXCEEDS_ADAPTER" and plan.stages[0].ordering == ("ocr.alpha",)
    fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.REVOKED, operator=OPERATOR,
                                       reason_code="SECURITY_ADVISORY")
    plan = fab.planner().plan(fab.task())
    assert {c.adapter_id: c.code for c in plan.stages[0].candidates}["ocr.alpha"] == "LIFECYCLE_NOT_PLANNABLE"
    assert plan.status is PlanStatus.INFEASIBLE


def test_explicit_operator_preference_then_adapter_id(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")))
    assert fab.planner().plan(fab.task()).stages[0].ordering == ("ocr.alpha", "ocr.beta")
    preferred = fab.planner(config=PlannerConfig(preferences={"DOCUMENT_OCR": ("ocr.beta", "ocr.unknown")}))
    stage = preferred.plan(fab.task()).stages[0]
    assert stage.ordering == ("ocr.beta", "ocr.alpha") and stage.selected_adapter_id == "ocr.beta"
    assert stage.alternatives == ()  # capability did not opt into explicit fallback


def test_advice_is_opt_in_and_disabling_restores_the_identical_plan(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")))
    advisor = Advisor({"ocr.beta": 9.0, "ocr.alpha": -9.0})
    baseline = fab.planner().plan(fab.task())
    disabled = fab.planner(advisor=advisor, config=PlannerConfig(advice_enabled=False)).plan(fab.task())
    assert advisor.calls == 0 and disabled.plan_hash == baseline.plan_hash
    enabled = fab.planner(advisor=advisor, config=PlannerConfig(advice_enabled=True)).plan(fab.task())
    stage = enabled.stages[0]
    assert stage.ordering == ("ocr.beta", "ocr.alpha") and stage.advice_status == "APPLIED"
    assert {a.adapter_id: a.adjustment for a in stage.advice} == {"ocr.alpha": -ADVICE_BOUND, "ocr.beta": ADVICE_BOUND}
    assert all(len(a.profile_hash) == 64 and a.sample_size == 12 for a in stage.advice)
    assert enabled.plan_hash != baseline.plan_hash and enabled.advice_enabled


def test_advice_cannot_make_infeasible_feasible_or_beat_explicit_preference(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")))
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    advisor = Advisor({"ocr.cloud": 100.0, "ocr.alpha": 9.0})
    config = PlannerConfig(advice_enabled=True, preferences={"DOCUMENT_OCR": ("ocr.beta",)})
    stage = fab.planner(advisor=advisor, config=config).plan(fab.task()).stages[0]
    assert stage.ordering == ("ocr.beta", "ocr.alpha")  # cloud stays infeasible, preference wins
    assert "ocr.cloud" not in {a.adapter_id for a in stage.advice}
    stale = fab.planner(advisor=Advisor({"ocr.beta": 1.0}, wrong_context=True),
                        config=PlannerConfig(advice_enabled=True)).plan(fab.task()).stages[0]
    assert stale.advice == () and stale.ordering == ("ocr.alpha", "ocr.beta")
    invalid = Advisor({"ocr.beta": 1.0}, invalid_features=True)
    stage = fab.planner(advisor=invalid, config=PlannerConfig(advice_enabled=True)).plan(fab.task()).stages[0]
    assert stage.advice_status == "ADVICE_CONTEXT_INVALID" and stage.advice == () and invalid.calls == 0
    assert stage.ordering == ("ocr.alpha", "ocr.beta")


def test_failing_or_duplicate_advice_never_changes_feasibility(tmp_path: Path) -> None:
    fab = fabric(tmp_path)

    class Broken(Advisor):
        def advise(self, **kwargs: object) -> list[object]:  # type: ignore[override]
            raise RuntimeError("advisor down")

    plan = fab.planner(advisor=Broken({}), config=PlannerConfig(advice_enabled=True)).plan(fab.task())
    assert plan.status is PlanStatus.FEASIBLE and plan.stages[0].advice_status == "ADVICE_ERROR"

    class Duplicating(Advisor):
        def advise(self, **kwargs: object) -> list[object]:  # type: ignore[override]
            return [*super().advise(**kwargs), *super().advise(**kwargs)]  # type: ignore[arg-type]

        def issued(self, plan_binding_hash: str, *, candidates: Sequence[tuple[str, str]] = ()) -> list[object] | None:  # type: ignore[override]
            single = super().issued(plan_binding_hash)
            return None if single is None else [*single, *single]  # it reports issuing the duplicates

    plan = fab.planner(advisor=Duplicating({"ocr.alpha": 0.1}),
                       config=PlannerConfig(advice_enabled=True)).plan(fab.task())
    assert plan.status is PlanStatus.FEASIBLE
    assert plan.stages[0].advice_status == "ADVICE_DUPLICATE_CANDIDATE" and plan.stages[0].advice == ()


def test_resource_inventory_fit_is_recorded_and_required(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    inventory = Inventory()
    plan = fab.planner(inventory=inventory).plan(fab.task())
    assert plan.stages[0].resource_id == "local-cpu"
    inventory.fits = {"gpu-node": False}
    plan = fab.planner(inventory=inventory).plan(fab.task())
    candidate = plan.stages[0].candidates[0]
    assert candidate.code == "RESOURCE_UNAVAILABLE" and candidate.resource_reasons == ("RAM_INSUFFICIENT",)


def test_model_stage_is_resolved_only_through_the_inference_delegate(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    plan = fab.planner().plan(task)
    assert "summary:MODEL_INFERENCE_DELEGATE_MISSING" in plan.infeasible_codes
    delegate = Delegate()
    plan = fab.planner(inference=delegate).plan(task)
    assert plan.status is PlanStatus.FEASIBLE
    summary = plan.stages[2]
    assert summary.router_status == "ROUTED" and summary.router_selection_hash and len(delegate.calls) == 1
    assert delegate.calls[0].classification is DataClassification.INTERNAL
    assert delegate.calls[0].network_policy == "WAN_REQUIRED"
    blocked = fab.planner(inference=Delegate(worker=None)).plan(task)
    assert "summary:WORKER_ROUTER_BLOCKED" in blocked.infeasible_codes


def test_model_adapter_that_does_not_delegate_is_refused(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab, delegates=False)
    plan = fab.planner(inference=Delegate()).plan(task)
    assert {c.adapter_id: c.code for c in plan.stages[2].candidates} == {
        "llm.bridge": "MODEL_INFERENCE_MUST_DELEGATE"}


def test_plan_level_infeasibility_codes(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task(task_type="unknown.type"))
    assert plan.status is PlanStatus.INFEASIBLE and plan.infeasible_codes == ("TEMPLATE_NOT_FOUND",)
    plan = fab.planner().plan(fab.task(required_outputs=("text.chunks/v1", "model.summary/v1")))
    assert plan.infeasible_codes == ("REQUIRED_OUTPUT_NOT_PRODUCED:model.summary/v1",)
    task = with_model(fab)
    plan = fab.planner(inference=Delegate()).plan(task.model_copy(update={"quality": QualityRequirement.VERIFIED}))
    assert "summary:QUALITY_REQUIREMENT_UNMET" in plan.infeasible_codes


@pytest.mark.parametrize("bad", ["", "operator; rm"])
def test_lifecycle_calls_need_an_operator_identity(tmp_path: Path, bad: str) -> None:
    fab = fabric(tmp_path)
    with pytest.raises(ValueError, match="OPERATOR_IDENTITY_INVALID"):
        fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.REVOKED, operator=bad, reason_code="X_Y_Z")


def test_seam01_planner_supplies_the_feasible_set_classification_and_an_advice_independent_binding(
        tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")))
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    advisor = Advisor({"ocr.beta": 0.2})
    config = PlannerConfig(advice_enabled=True)
    stage = fab.planner(advisor=advisor, config=config).plan(fab.task()).stages[0]
    assert stage.ordering == ("ocr.beta", "ocr.alpha") and stage.advice_status == "APPLIED"
    first = advisor.seen[0]
    assert first["candidates"] == (("ocr.alpha", "1"), ("ocr.beta", "1"))  # feasible only: no ocr.cloud
    assert first["classification"] == "CONFIDENTIAL" and len(first["plan_binding_hash"]) == 64
    other = Advisor({"ocr.beta": -0.2})  # different advice, same decision -> same binding
    fab.planner(advisor=other, config=config).plan(fab.task())
    assert other.seen[0]["plan_binding_hash"] == first["plan_binding_hash"]
    fab.planner(advisor=other, config=config).plan(fab.task(task_id="task-2"))
    assert other.seen[1]["plan_binding_hash"] != first["plan_binding_hash"]


@pytest.mark.parametrize("bind_to", [
    {"candidates": (("ocr.alpha", "1"), ("ocr.beta", "1"), ("ocr.cloud", "1"))},
    {"candidates": (("ocr.beta", "1"),)},
    {"classification": "PUBLIC"},
    {"plan_binding_hash": "f" * 64},
])
def test_seam01_advice_bound_to_another_decision_is_ignored(tmp_path: Path, bind_to: dict[str, object]) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")))
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    advisor = Advisor({"ocr.beta": 0.25, "ocr.cloud": 100.0}, bind_to=bind_to)
    plan = fab.planner(advisor=advisor, config=PlannerConfig(advice_enabled=True)).plan(fab.task())
    stage = plan.stages[0]
    assert advisor.calls == 2 and stage.advice == ()  # one request per stage (ocr, chunk)
    assert stage.ordering == ("ocr.alpha", "ocr.beta")  # neutral: operator preference then adapter_id
    assert "ocr.cloud" not in stage.ordering and plan.status is PlanStatus.FEASIBLE


def test_seam01_correctly_bound_advice_still_cannot_widen_feasibility(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    stage = fab.planner(advisor=Advisor({"ocr.cloud": 100.0}),
                        config=PlannerConfig(advice_enabled=True)).plan(fab.task()).stages[0]
    assert stage.ordering == ("ocr.alpha",) and stage.advice == ()
    assert {c.adapter_id: c.code for c in stage.candidates}["ocr.cloud"] == "NETWORK_NOT_PERMITTED"
