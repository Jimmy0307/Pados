"""Requirements 4 and 5: resource-aware planning on a CPU-only node (an accelerator-only stage is
INFEASIBLE with RESOURCE_UNAVAILABLE, never silently replaced) and optional, disabled-by-default Outcome
Learning advice that cannot change the feasible set."""
from __future__ import annotations

import dataclasses
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pipeline_support import CPU_NODE, OBSERVED, STAGE_IDS, cpu_node, full_grant, inventory, pipeline, spy_adapters

from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.model import AdapterDescriptor
from pados.capability.planner import ExecutionPlan, PlanStatus
from pados.capability.policy import PolicyCode
from pados.capability.seams import Advice
from pados.execution.context import canonical_hash
from pados.learning.advice_log import TEST_ONLY_ACKNOWLEDGEMENT, AdviceLog
from pados.learning.advisor import AdvisorConfig, OutcomeAdvisor
from pados.learning.records import OutcomeRecord
from pados.packs.intelligence.template import analysis_template
from pados.packs.retrieval.engines import EMBEDDING_ADAPTER_ID, PASSAGE_RETRIEVAL_EMBEDDING
from pados.resources.hardware import AcceleratorRuntime, GpuInfo, GpuVendor

DENSE = "document_decision_analysis_dense"


def _dense(fab_root: Path, *profiles: object) -> tuple[object, ExecutionPlan]:
    fab = pipeline(fab_root, resources=inventory(*profiles) if profiles else None)  # type: ignore[arg-type]
    fab.wiring.templates.register(analysis_template({"retrieve": PASSAGE_RETRIEVAL_EMBEDDING}, task_type=DENSE))
    fab.grants.grant(*full_grant(), f"CAPABILITY:{PASSAGE_RETRIEVAL_EMBEDDING}")
    return fab, fab.planner().plan(fab.task(task_type=DENSE))


def test_cpu_only_inventory_fits_every_stage_on_the_cpu_node(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    if sys.platform == "win32":  # Preserve planner coverage while live OCR containment remains pending.
        assert plan.status is PlanStatus.INFEASIBLE
        assert plan.infeasible_codes == ("preprocess:NO_FEASIBLE_CANDIDATE", "recognize:NO_FEASIBLE_CANDIDATE")
        stages = [stage for stage in plan.stages if stage.stage_id not in {"preprocess", "recognize"}]
        assert all(stage.feasible and stage.resource_id == CPU_NODE for stage in stages)
        for stage in stages:
            selected = next(c for c in stage.candidates if c.adapter_id == stage.selected_adapter_id)
            assert any(reason.startswith("CPU_CORES_OK") for reason in selected.resource_reasons)
            assert any(reason.startswith("GPU_NOT_REQUIRED") for reason in selected.resource_reasons)
        return
    result = fab.execute(plan, task)
    assert plan.feasible and all(stage.resource_id == CPU_NODE for stage in plan.stages)
    for stage in plan.stages:
        selected = next(c for c in stage.candidates if c.adapter_id == stage.selected_adapter_id)
        assert any(reason.startswith("CPU_CORES_OK") for reason in selected.resource_reasons)
        assert any(reason.startswith("GPU_NOT_REQUIRED") for reason in selected.resource_reasons)
    assert result.status is ExecutionStatus.COMPLETED
    assert all(receipt.resource_id == CPU_NODE for receipt in result.plan_receipt.stages)


def test_accelerator_stage_is_infeasible_on_cpu_only_node_without_fallback(tmp_path: Path) -> None:
    fab, plan = _dense(tmp_path)
    calls = spy_adapters(fab.wiring)  # type: ignore[attr-defined]
    expected_infeasible = ("retrieve:NO_FEASIBLE_CANDIDATE",)
    if sys.platform == "win32":  # OCR containment is intentionally unavailable pending Windows UAT.
        expected_infeasible = ("preprocess:NO_FEASIBLE_CANDIDATE", "recognize:NO_FEASIBLE_CANDIDATE",
                               *expected_infeasible)
    assert plan.status is PlanStatus.INFEASIBLE and plan.infeasible_codes == expected_infeasible
    retrieve = next(stage for stage in plan.stages if stage.stage_id == "retrieve")
    assert retrieve.capability_id == PASSAGE_RETRIEVAL_EMBEDDING and retrieve.selected_adapter_id is None
    assert [(c.adapter_id, c.code) for c in retrieve.candidates] == [
        (EMBEDDING_ADAPTER_ID, PolicyCode.RESOURCE_UNAVAILABLE.value)]
    reasons = retrieve.candidates[0].resource_reasons
    assert any(r.startswith("GPU_MISSING") for r in reasons) and any(r.startswith("ACCELERATOR_UNAVAILABLE")
                                                                     for r in reasons)
    unavailable_ocr_stages = {"preprocess", "recognize"} if sys.platform == "win32" else set()
    others = [stage for stage in plan.stages if stage.stage_id != "retrieve"
              and stage.stage_id not in unavailable_ocr_stages]
    assert all(stage.feasible and stage.resource_id == CPU_NODE for stage in others)
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_FEASIBLE"):
        fab.execute(plan, fab.task(task_type=DENSE))  # type: ignore[attr-defined]
    assert calls == []


def test_accelerator_present_still_needs_an_installed_engine(tmp_path: Path) -> None:
    gpu = cpu_node(resource_id="gpu-node", gpu=GpuInfo(vendor=GpuVendor.NVIDIA, label="RTX-class 16 GB"),
                   vram_mb=16_384, runtimes=(AcceleratorRuntime.CPU, AcceleratorRuntime.CUDA))
    _, plan = _dense(tmp_path, cpu_node(), gpu)
    retrieve = next(stage for stage in plan.stages if stage.stage_id == "retrieve")
    assert plan.status is PlanStatus.INFEASIBLE
    assert [(c.adapter_id, c.code, c.resource_id) for c in retrieve.candidates] == [
        (EMBEDDING_ADAPTER_ID, PolicyCode.ADAPTER_UNAVAILABLE.value, "gpu-node")]


def test_stale_telemetry_makes_every_stage_infeasible(tmp_path: Path) -> None:
    fab = pipeline(tmp_path, resources=inventory(cpu_node(observed_at=OBSERVED.replace(hour=1))))
    plan = fab.planner().plan(fab.task())
    assert plan.status is PlanStatus.INFEASIBLE
    assert plan.infeasible_codes == tuple(f"{sid}:NO_FEASIBLE_CANDIDATE" for sid in STAGE_IDS)


# --- Outcome advice (optional, disabled by default) -------------------------------------------------

class NoOutcomes:
    def read(self) -> Sequence[OutcomeRecord]:
        return ()


@dataclasses.dataclass
class FavouringAdvisor:
    """ScoreAdvisor double that strongly favours every candidate it is shown (bound to the request)."""

    calls: int = 0
    log: dict[str, tuple[Advice, ...]] = dataclasses.field(default_factory=dict)

    def context_hash(self, *, task_type: str, capability_id: str, context_features: Mapping[str, str],
                     candidates: tuple[tuple[str, str], ...], classification: str,
                     plan_binding_hash: str | None = None) -> str:
        return canonical_hash({"task_type": task_type, "capability_id": capability_id,
                               "features": dict(context_features), "candidates": [list(c) for c in candidates],
                               "classification": classification, "binding": plan_binding_hash})

    def advise(self, *, task_type: str, capability_id: str, candidates: Sequence[AdapterDescriptor],
               context_features: Mapping[str, str], classification: str,
               plan_binding_hash: str | None = None) -> Sequence[Advice]:
        self.calls += 1
        pairs = tuple(sorted((c.adapter_id, c.version) for c in candidates))
        context = self.context_hash(task_type=task_type, capability_id=capability_id,
                                    context_features=context_features, candidates=pairs,
                                    classification=classification, plan_binding_hash=plan_binding_hash)
        advice = tuple(Advice(c.adapter_id, 9.0, 50, "favoured", canonical_hash({"p": c.adapter_id}), context)
                       for c in candidates)
        if plan_binding_hash is not None:
            self.log[plan_binding_hash] = advice  # what this advisor issued and "recorded" for the binding
        return list(advice)

    def issued(self, plan_binding_hash: str, *,
               candidates: Sequence[tuple[str, str]] = ()) -> Sequence[Advice] | None:
        return self.log.get(plan_binding_hash)


def _shape(plan: ExecutionPlan) -> list[tuple[object, ...]]:
    return [(s.stage_id, s.capability_id, s.selected_adapter_id, s.ordering, s.alternatives, s.decision_hash,
             s.resource_id, tuple((c.adapter_id, c.code, c.decision_hash) for c in s.candidates))
            for s in plan.stages]


def test_advice_is_disabled_by_default(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    assert fab.wiring.planner_config.advice_enabled is False
    plan = fab.planner(advisor=FavouringAdvisor()).plan(fab.task())
    assert plan.advice_enabled is False and all(not s.advice and s.advice_status is None for s in plan.stages)
    assert plan.plan_hash == fab.planner().plan(fab.task()).plan_hash


def test_enabled_outcome_advice_keeps_the_feasible_set_and_changes_nothing_without_alternatives(
        tmp_path: Path) -> None:
    baseline_fab = pipeline(tmp_path / "baseline")
    _, baseline_plan, baseline = baseline_fab.run()
    fab = pipeline(tmp_path / "advised")
    advisor = OutcomeAdvisor(NoOutcomes(), config=AdvisorConfig(enabled=True), log=AdviceLog.ephemeral_for_tests(
        acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT))
    config = dataclasses.replace(fab.wiring.planner_config, advice_enabled=True)
    _, plan, result = fab.run(advisor=advisor, config=config)
    assert plan.advice_enabled and plan.feasible and _shape(plan) == _shape(baseline_plan)
    assert all(stage.advice == () for stage in plan.stages)  # no qualifying outcome profile: no adjustment
    assert advisor.log is not None
    entries = advisor.log.entries()
    assert len(entries) == len(plan.stages) and all(entry.advice == () for entry in entries)
    # The planner now passes only the advisor's closed vocabulary (no features in V1), so the real advisor
    # computes a bound context and reports that no candidate has >= min_samples outcomes (no adjustment).
    assert {entry.status.value for entry in entries} == {"NO_QUALIFYING_PROFILE"}
    assert all(stage.advice_status != "ADVICE_CONTEXT_INVALID" for stage in plan.stages)
    assert result.status is ExecutionStatus.COMPLETED
    assert fab.outputs(result) == baseline_fab.outputs(baseline)


def test_bounded_advice_for_the_only_candidate_reorders_nothing(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    advisor = FavouringAdvisor()
    config = dataclasses.replace(fab.wiring.planner_config, advice_enabled=True)
    _, plan, result = fab.run(advisor=advisor, config=config)
    baseline = fab.planner().plan(fab.task())
    assert advisor.calls == len(STAGE_IDS) and _shape(plan) == _shape(baseline)
    for stage in plan.stages:
        assert stage.advice_status == "APPLIED" and len(stage.ordering) == 1
        assert all(abs(item.adjustment) <= 0.25 for item in stage.advice)  # clamped to ADVICE_BOUND
    assert result.status is ExecutionStatus.COMPLETED
