"""Requirement 6: no authority. Outputs are candidates, nothing writes CAS / ScopeVersion / semantic state,
the final result is not accepted truth, and a gated task records only a verified human-decision reference."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pipeline_support import pipeline

from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.decision_ledger import FileConsumedDecisionLedger
from pados.capability.human_decision import HmacHumanDecisionVerifier, HumanDecisionRefused
from pados.capability import operator_decisions
from pados.capability.operator_decisions import (
    OperatorBinding,
    OperatorDecisionIssuer,
    current_os_account,
    open_operator_issuer,
)
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.packs.algorithms.contracts import load_result
from pados.packs.intelligence.report import load_report

AUTHORITY_MODULES = ("pados.state_proof.service", "pados.state_proof.repository", "pados.state_proof.audit_chain",
                     "pados.product_loop.state", "pados.product_loop.service", "pados.memory", "pados.db",
                     "pados.worker.router", "pados.core.scheduler", "pados.execution.semantic_context")
ACCEPTANCE_WORDS = {"accepted", "acceptance", "human_acceptance", "accepted_by", "is_truth"}
KEY_NAME = "PADOS_TEST_PIPELINE_DECISION_KEY"
APPROVER = "jimmy.operator"
ISSUED = datetime(2026, 10, 3, 8, 30, tzinfo=UTC)  # the executor clock starts at 2026-10-03T09:00Z


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_every_output_is_a_candidate_and_nothing_is_accepted(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built, _, report_result = fab.report(task, plan, result)
    for execution in (result, report_result):
        assert execution.semantic_authority == "NONE" and execution.plan_receipt.semantic_authority == "NONE"
        assert all(stage.semantic_authority == "NONE" for stage in execution.plan_receipt.stages)
        assert not ACCEPTANCE_WORDS & _keys(execution.plan_receipt.model_dump(mode="json"))
    for stage in result.plan_receipt.stages:
        for ref in stage.outputs:
            document = json.loads(fab.store.get(ref))
            assert document.get("semantic_authority", "NONE") == "NONE", ref.kind
            assert not ACCEPTANCE_WORDS & _keys(document), ref.kind
    produced = fab.outputs(result)
    assert load_result(fab.store.get(produced["analyze"][0]), produced["analyze"][0]).semantic_authority == "NONE"
    explanation = json.loads(fab.store.get(produced["explain"][0]))
    assert explanation["label"] == "EXPLANATION" and explanation["authoritative"] is False
    report = load_report(fab.store.get(report_result.plan_receipt.stages[-1].outputs[0]))
    assert report.label == "PIPELINE_REPORT_CANDIDATE" and report.authoritative is False
    assert report.semantic_authority == "NONE" and report.human_decision_ref is None
    assert not ACCEPTANCE_WORDS & _keys(report.canonical())


def test_the_pipeline_writes_only_content_addressed_artifacts(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    fab.report(task, plan, result)
    ledger = fab.ledger.path.parent.resolve()  # the executor's trusted, content-free execution ledger (R1)
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert sorted(p.name for p in files if p.resolve().is_relative_to(ledger)) == [
        "executions.jsonl", "executions.jsonl.lock"]
    assert [entry.task_id for entry in fab.ledger.entries()] == [task.task_id, f"{task.task_id}.report"]
    written = [path for path in files if not path.resolve().is_relative_to(ledger)]
    objects = (tmp_path / "objects").resolve()
    assert written and all(path.resolve().is_relative_to(objects) for path in written)
    assert all(path.name == path.parent.parent.name + path.parent.name + path.name[4:] for path in written)


def test_a_fresh_process_running_the_pipeline_loads_no_authority_writer(tmp_path: Path) -> None:
    script = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        from types import SimpleNamespace
        sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
        from pados.packs.algorithms import adapters
        adapters.time = SimpleNamespace(perf_counter_ns=lambda: 0)
        from pipeline_support import pipeline
        fab = pipeline(Path({str(tmp_path)!r}))
        task, plan, result = fab.run()
        fab.report(task, plan, result)
        loaded = sorted(name for name in sys.modules if name.startswith({AUTHORITY_MODULES!r}))
        print(json.dumps({{"status": result.status.value, "loaded": loaded}}))
    """)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PADOS_")}
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                               env=env, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    assert outcome == {"status": "COMPLETED", "loaded": []}


def _boundary() -> SecretBoundary:
    secrets = {KEY_NAME: "operator-held-" + "p" * 40}  # assembled at runtime; >= MIN_KEY_BYTES
    return SecretBoundary(EnvironmentSecretResolver(secrets))


def _verifier(tmp_path: Path) -> HmacHumanDecisionVerifier:
    """Execution side: verify-only, per-operator key, durable single-use ledger (CR13b-01/02)."""
    return HmacHumanDecisionVerifier(operator_keys={APPROVER: f"secretref://env/{KEY_NAME}"}, boundary=_boundary(),
                                     ledger=FileConsumedDecisionLedger(tmp_path / "decisions" / "consumed.jsonl"),
                                     environ={})


class _Terminal:
    """Injected interactive terminal (CR13b-01 seam): the human types the lines queued in ``typed``."""

    def __init__(self) -> None:
        self.typed: list[str] = []

    def interactive(self) -> bool:
        return True

    def read_confirmation(self, prompt: str) -> str:
        return self.typed.pop(0) if self.typed else ""


def _issuer(terminal: _Terminal) -> OperatorDecisionIssuer:
    """Operator side only: the execution path never imports or holds the issuer. The approver is bound to the
    OS account this test process runs as (read from the OS, never injected); only the terminal is a seam."""
    operators = {APPROVER: OperatorBinding(key_ref=f"secretref://env/{KEY_NAME}", account=current_os_account())}
    previous = operator_decisions._session_factory
    operator_decisions._session_factory = lambda: terminal  # the private terminal seam (no session parameter)
    try:
        return open_operator_issuer(approver=APPROVER, operators=operators, boundary=_boundary(), environ={})
    finally:
        operator_decisions._session_factory = previous


def _approve(issuer: OperatorDecisionIssuer, terminal: _Terminal, task_id: str, plan_hash: str) -> dict[str, str]:
    terminal.typed += [plan_hash[:12], task_id]  # the human confirms what they reviewed
    return issuer.approve(task_id=task_id, plan_hash=plan_hash, issued_at=ISSUED, lifetime=timedelta(hours=2))


def test_gated_task_runs_only_with_a_verified_decision_recorded_as_a_reference(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task(human_gate=True)
    plan = fab.planner().plan(task)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_VERIFIER_MISSING"):
        fab.execute(plan, task)
    authority = _verifier(tmp_path)
    terminal = _Terminal()
    issuer = _issuer(terminal)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_REQUIRED"):
        fab.execute(plan, task, human_decisions=authority)
    other = _approve(issuer, terminal, task.task_id, "0" * 64)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_PLAN_MISMATCH"):
        fab.execute(plan, task, human_decision=other, human_decisions=authority)
    terminal.typed += ["0" * 12, task.task_id]  # confirming a plan hash the human did not review: nothing signed
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_CONFIRMATION_MISMATCH$"):
        issuer.approve(task_id=task.task_id, plan_hash=plan.plan_hash, issued_at=ISSUED, lifetime=timedelta(hours=2))
    record = _approve(issuer, terminal, task.task_id, plan.plan_hash)
    result = fab.execute(plan, task, human_decision=record, human_decisions=authority)
    assert result.status is ExecutionStatus.COMPLETED
    receipt = result.plan_receipt
    assert receipt.human_decision_ref == f"human-decision:{record['decision_id']}"
    assert receipt.semantic_authority == "NONE" and not ACCEPTANCE_WORDS & _keys(receipt.model_dump(mode="json"))
    assert "p" * 40 not in json.dumps(receipt.model_dump(mode="json"))
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_ALREADY_CONSUMED"):
        fab.execute(plan, task, human_decision=record, human_decisions=authority)
    _, _, report_result = fab.report(task, plan, result)
    report = load_report(fab.store.get(report_result.plan_receipt.stages[-1].outputs[0]))
    assert report.human_decision_ref == receipt.human_decision_ref and report.authoritative is False
