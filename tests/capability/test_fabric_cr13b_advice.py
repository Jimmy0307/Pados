"""CF-01 (CR-13b): the advice a plan carries must be exactly the advice the advisor issued for that binding."""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest
from fabric_support import Advisor, Fabric, TransformAdapter, adapter_descriptor, fabric, forge

from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.planner import AdviceRecord, ExecutionPlan, PlannerConfig
from pados.capability.seams import ADVICE_BOUND, Advice
from pados.execution.context import canonical_hash

ADVICE_ON = PlannerConfig(advice_enabled=True)


def three_ocr(tmp_path: Path) -> Fabric:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    fab.add(TransformAdapter(adapter_descriptor("ocr.gamma", "DOCUMENT_OCR"), kind="ocr.text"))
    return fab


def no_adapter_ran(fab: Fabric) -> bool:
    return all(not impl.calls for impl in fab.adapters.values())


def fabricate(plan: ExecutionPlan, favourite: str) -> ExecutionPlan:
    """A forger: bounded advice for ``favourite`` with well-formed hashes, consistent ordering, rehashed plan."""
    stage = plan.stages[0]
    fake = AdviceRecord(favourite, ADVICE_BOUND, 500, canonical_hash({"profile": "made-up"}),
                        canonical_hash({"explanation": "looks plausible"}), False)
    others = tuple(aid for aid in stage.ordering if aid != favourite)
    candidate = next(c for c in stage.candidates if c.adapter_id == favourite)
    return forge(plan, 0, advice=(fake,), ordering=(favourite, *others), selected_adapter_id=favourite,
                 adapter_digest=candidate.adapter_digest, decision_hash=candidate.decision_hash)


def test_cf01_fabricated_bounded_advice_with_recomputed_hashes_is_refused(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()
    advisor = Advisor({"ocr.gamma": 0.2})
    plan = fab.planner(advisor=advisor, config=ADVICE_ON).plan(task)
    assert plan.stages[0].selected_adapter_id == "ocr.gamma"
    forged = fabricate(plan, "ocr.beta")
    assert forged.plan_hash != plan.plan_hash and forged.stages[0].advice_context_hash == (
        plan.stages[0].advice_context_hash)  # the forger even kept the genuine context binding
    with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
        fab.executor(advisor=advisor, config=ADVICE_ON).execute(forged, task)
    assert no_adapter_ran(fab)
    result = fab.executor(advisor=advisor, config=ADVICE_ON).execute(plan, task)  # genuine advice reproduces
    assert result.status is ExecutionStatus.COMPLETED and result.plan_receipt.stages[0].adapter_id == "ocr.gamma"


def test_cf01_adjusted_or_rehashed_advice_records_are_refused(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()
    advisor = Advisor({"ocr.gamma": 0.1, "ocr.beta": -0.1})
    plan = fab.planner(advisor=advisor, config=ADVICE_ON).plan(task)
    stage = plan.stages[0]
    by_id = {record.adapter_id: record for record in stage.advice}
    edits = {
        "adjustment": tuple(dataclasses.replace(r, adjustment=0.2) if r.adapter_id == "ocr.gamma" else r
                            for r in stage.advice),
        "profile_hash": tuple(dataclasses.replace(r, profile_hash=canonical_hash({"other": r.adapter_id}))
                              for r in stage.advice),
        "explanation_hash": tuple(dataclasses.replace(r, explanation_hash=canonical_hash({"x": 1}))
                                  for r in stage.advice),
        "sample_size": tuple(dataclasses.replace(r, sample_size=999) for r in stage.advice),
        "dropped": (by_id["ocr.gamma"],),
    }
    for name, advice in edits.items():
        with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
            fab.executor(advisor=advisor, config=ADVICE_ON).execute(forge(plan, 0, advice=advice), task)
        assert no_adapter_ran(fab), name


def test_cf01_without_an_advisor_the_recorded_advice_must_be_empty(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()
    unadvised = fab.planner().plan(task)
    forged = fabricate(unadvised, "ocr.gamma")
    for executor in (fab.executor(), fab.executor(config=ADVICE_ON)):  # no advisor configured at all
        with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
            executor.execute(forged, task)
    assert no_adapter_ran(fab)


def test_cf01_advice_returned_but_never_issued_never_enters_a_plan(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()

    class Unlogged(Advisor):
        def issued(self, plan_binding_hash: str, *, candidates: Sequence[tuple[str, str]] = ()) -> Sequence[Advice] | None:
            return None  # it returned advice but never recorded issuing it

    plan = fab.planner(advisor=Unlogged({"ocr.gamma": 0.25}), config=ADVICE_ON).plan(task)
    stage = plan.stages[0]
    assert stage.advice == () and stage.advice_status == "ADVICE_NOT_ISSUED"
    assert stage.ordering == ("ocr.alpha", "ocr.beta", "ocr.gamma")
    assert fab.executor(advisor=Unlogged({}), config=ADVICE_ON).execute(plan, task).status is ExecutionStatus.COMPLETED


def test_cf01_reproduction_reads_the_issued_set_and_never_re_asks(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()
    advisor = Advisor({"ocr.beta": 0.2})
    plan = fab.planner(advisor=advisor, config=ADVICE_ON).plan(task)
    asked = advisor.calls
    stranger = Advisor({"ocr.beta": 0.2})  # same scores, but it never issued anything for this plan
    with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
        fab.executor(advisor=stranger, config=ADVICE_ON).execute(plan, task)
    assert stranger.calls == 0 and stranger.issued_calls >= 1
    result = fab.executor(advisor=advisor, config=ADVICE_ON).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED and advisor.calls == asked
    assert result.plan_receipt.stages[0].adapter_id == "ocr.beta"


def test_cf01_reissued_advice_supersedes_and_stale_plans_are_refused(tmp_path: Path) -> None:
    fab = three_ocr(tmp_path)
    task = fab.task()
    advisor = Advisor({"ocr.beta": 0.2})
    first = fab.planner(advisor=advisor, config=ADVICE_ON).plan(task)
    advisor.scores = {"ocr.gamma": 0.2}
    second = fab.planner(advisor=advisor, config=ADVICE_ON).plan(task)  # same binding, new issuance
    assert first.plan_hash != second.plan_hash
    with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
        fab.executor(advisor=advisor, config=ADVICE_ON).execute(first, task)
    result = fab.executor(advisor=advisor, config=ADVICE_ON).execute(second, task)
    assert result.plan_receipt.stages[0].adapter_id == "ocr.gamma"
