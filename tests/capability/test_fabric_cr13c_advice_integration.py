"""CR13c-01 (confirmed false positive): the REAL planner -> ``OutcomeAdvisor.issued(binding, candidates=...)`` path
applies logged advice, and the executor reproduces the advised plan from the same durable issuance log.

Integration test only: it imports ``pados.learning`` here; no fabric module imports it."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from fabric_support import Fabric, TransformAdapter, adapter_descriptor, fabric

from pados.capability.executor import ExecutionStatus
from pados.capability.planner import PlannerConfig
from pados.capability.seams import ScoreAdvisor
from pados.learning.advisor import AdvisorConfig, OutcomeAdvisor
from tests.learning.learning_support import NOW, ListSource, file_log, history

ADVISED = PlannerConfig(advice_enabled=True)


def _fabric(tmp_path: Path) -> Fabric:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    return fab


def _advisor(tmp_path: Path) -> OutcomeAdvisor:
    """Authenticated outcome history (V1: no context features): ocr.beta clearly outperforms ocr.alpha."""
    records = [*history("ocr.alpha", 10, 30, features={}, task_type="doc.extract"),
               *history("ocr.beta", 38, 2, offset=100, features={}, task_type="doc.extract")]
    return OutcomeAdvisor(ListSource(records), config=AdvisorConfig(enabled=True), log=file_log(tmp_path),
                          clock=lambda: NOW)


def test_real_planner_applies_the_advice_outcome_advisor_issued_and_logged(tmp_path: Path) -> None:
    fab = _fabric(tmp_path)
    task = fab.task()
    advisor = _advisor(tmp_path)
    seam: ScoreAdvisor = advisor  # the concrete advisor satisfies the fabric seam, ``issued(candidates=)`` included
    baseline = fab.planner().plan(task)
    assert baseline.stages[0].ordering == ("ocr.alpha", "ocr.beta")  # adapter_id order without advice
    plan = fab.planner(advisor=seam, config=ADVISED).plan(task)
    ocr = plan.stages[0]
    assert ocr.advice_status == "APPLIED" and ocr.selected_adapter_id == "ocr.beta"
    assert ocr.ordering == ("ocr.beta", "ocr.alpha")
    pairs = (("ocr.alpha", "1"), ("ocr.beta", "1"))
    assert advisor.log is not None and advisor.log.entries()
    binding = advisor.log.entries()[0].plan_binding_hash
    assert binding is not None
    issued = advisor.issued(binding, candidates=pairs)  # what the planner read back
    assert issued is not None and {a.adapter_id for a in issued} == {"ocr.alpha", "ocr.beta"}
    recorded = {item.adapter_id: item for item in ocr.advice}
    for advice in issued:
        assert recorded[advice.adapter_id].profile_hash == advice.profile_hash
        assert recorded[advice.adapter_id].sample_size == advice.sample_size
    assert recorded["ocr.beta"].adjustment > 0 > recorded["ocr.alpha"].adjustment
    assert advisor.issued(binding, candidates=pairs[:1]) is None  # a different candidate set issues nothing


def test_executor_reproduces_the_advised_plan_from_the_durable_log_and_runs_it(tmp_path: Path) -> None:
    fab = _fabric(tmp_path)
    task = fab.task()
    plan = fab.planner(advisor=_advisor(tmp_path), config=ADVISED).plan(task)
    restarted = _advisor(tmp_path)  # a fresh advisor over the same durable log: issued() still answers
    result = fab.executor(advisor=restarted, config=ADVISED).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    assert result.plan_receipt.stages[0].adapter_id == "ocr.beta"
    assert fab.adapters["ocr.beta"].calls and not fab.adapters["ocr.alpha"].calls


def test_disabling_advice_gives_the_byte_identical_no_advisor_plan(tmp_path: Path) -> None:
    fab = _fabric(tmp_path)
    task = fab.task()
    disabled = fab.planner(advisor=_advisor(tmp_path), config=dataclasses.replace(ADVISED, advice_enabled=False))
    assert disabled.plan(task).plan_hash == fab.planner().plan(task).plan_hash
