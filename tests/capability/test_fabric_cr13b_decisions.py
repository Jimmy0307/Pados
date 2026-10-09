"""CR-13b human decisions: snapshot once (CR13b-03), issuance separated from verification with per-operator
keys (CR13b-01), durable shared single-use ledger (CR13b-02)."""
from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import os
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fabric_support import Fabric, fabric
from operator_support import Approver, TypingHuman, bindings, open_approver, open_operator_issuer_at, this_account

import pados.capability.decision_ledger as ledger_module
from pados.capability.decision_ledger import (
    CAPABILITY_DECISION_LEDGER,
    CAPABILITY_DECISION_LEDGER_RETENTION,
    LEDGER_KEYS,
    FileConsumedDecisionLedger,
    in_memory_ledger_for_tests,
)
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.human_decision import (
    SIGNING_ATTRIBUTES,
    HmacHumanDecisionVerifier,
    HumanDecisionRefused,
    VerifiedHumanDecision,
    signed_payload,
    signing_capable,
)
from pados.capability.operator_decisions import OperatorBinding, OperatorDecisionIssuer
from pados.capability.planner import ExecutionPlan
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.lifecycle.minimization import DataMinimizationViolation
from pados.lifecycle.retention import DataKind, PurgeRule, RetentionClass, policy_for

ALICE, BOB = "alice.operator", "bob.operator"
ALICE_KEY_NAME, BOB_KEY_NAME = "PADOS_TEST_DECISION_KEY_ALICE", "PADOS_TEST_DECISION_KEY_BOB"
ALICE_REF, BOB_REF = f"secretref://env/{ALICE_KEY_NAME}", f"secretref://env/{BOB_KEY_NAME}"
ISSUED = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)


def _alice_key() -> str:
    return "alice-held-" + "a" * 40  # assembled at runtime


def _bob_key() -> str:
    return "bob-held-" + "b" * 40


def boundary() -> SecretBoundary:
    return SecretBoundary(EnvironmentSecretResolver({ALICE_KEY_NAME: _alice_key(), BOB_KEY_NAME: _bob_key()}))


def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "decisions" / "consumed.jsonl"


def verifier(tmp_path: Path, *, ledger: Any = None) -> HmacHumanDecisionVerifier:
    return HmacHumanDecisionVerifier(operator_keys={ALICE: ALICE_REF, BOB: BOB_REF}, boundary=boundary(),
                                     ledger=ledger if ledger is not None else FileConsumedDecisionLedger(
                                         ledger_path(tmp_path)), environ={})


def issuer(approver: str = ALICE, key_ref: str = ALICE_REF) -> Approver:
    """Opened from the test process's own OS account with an injected interactive terminal (CR13b-01)."""
    return open_approver(approver, key_ref, boundary())


def approve(task_id: str, plan_hash: str, *, by: Approver | None = None) -> dict[str, str]:
    return (by or issuer()).approve(task_id, plan_hash, issued_at=ISSUED, lifetime=timedelta(hours=2))


def gated(tmp_path: Path) -> tuple[Fabric, Any, ExecutionPlan]:
    fab = fabric(tmp_path)
    task = fab.task(human_gate=True)
    return fab, task, fab.planner().plan(task)


def no_adapter_ran(fab: Fabric) -> bool:
    return all(not impl.calls for impl in fab.adapters.values())


# --- CR13b-03: the caller's record is read exactly once --------------------------------------------


class ShiftingDecision(Mapping[str, str]):
    """A caller-controlled mapping: the signed id on the first reads of ``decision_id``, a fresh one after."""

    def __init__(self, signed: dict[str, str], fresh_id: str, honest_reads: int = 1) -> None:
        self.signed = signed
        self.fresh_id = fresh_id
        self.honest_reads = honest_reads
        self.reads = 0

    def __getitem__(self, key: str) -> str:
        if key == "decision_id":
            self.reads += 1
            return self.signed[key] if self.reads <= self.honest_reads else self.fresh_id
        return self.signed[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.signed)

    def __len__(self) -> int:
        return len(self.signed)


class StrSubclass(str):
    pass


def test_cr13b03_shifting_mapping_cannot_bypass_consumption_or_yield_an_unsigned_id(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    signed = approve(task.task_id, plan.plan_hash)
    fresh = "e" * 32
    authority = verifier(tmp_path)
    for honest_reads in (1, 2, 3):
        shifty = ShiftingDecision(signed, fresh, honest_reads)
        with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_MALFORMED$"):
            authority.verify_and_consume(shifty, task_id=task.task_id, plan_hash=plan.plan_hash, now=NOW)
        with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_MALFORMED$"):
            fab.executor(human_decisions=authority).execute(plan, task, human_decision=shifty)
    assert no_adapter_ran(fab)
    ledger = FileConsumedDecisionLedger(ledger_path(tmp_path))
    assert not ledger.is_consumed(fresh) and not ledger.is_consumed(signed["decision_id"])
    result = fab.executor(human_decisions=authority).execute(plan, task, human_decision=dict(signed))
    assert result.plan_receipt.human_decision_ref == f"human-decision:{signed['decision_id']}"  # the SIGNED id
    assert ledger.is_consumed(signed["decision_id"]) and not ledger.is_consumed(fresh)


def test_cr13b03_only_plain_dicts_of_plain_strings_are_accepted(tmp_path: Path) -> None:
    _, task, plan = gated(tmp_path)
    signed = approve(task.task_id, plan.plan_hash)
    authority = verifier(tmp_path)

    class DictSubclass(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            return "e" * 32 if key == "decision_id" else super().__getitem__(key)

    non_plain: dict[str, Any] = {
        "dict_subclass": DictSubclass(signed),
        "str_subclass_value": {**signed, "decision_id": StrSubclass(signed["decision_id"])},
        "non_str_value": {**signed, "issued_at": ISSUED},
        "missing_field": {k: v for k, v in signed.items() if k != "origin"},
        "oversized_value": {**signed, "task_id": "t" * 500},
    }
    for name, record in non_plain.items():
        assert authority.check(record, task_id=task.task_id, plan_hash=plan.plan_hash, now=NOW).value == (
            "HUMAN_DECISION_MALFORMED"), name
        with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_MALFORMED$"):
            authority.verify_and_consume(record, task_id=task.task_id, plan_hash=plan.plan_hash, now=NOW)
    verified = authority.verify_and_consume(signed, task_id=task.task_id, plan_hash=plan.plan_hash, now=NOW)
    assert verified == VerifiedHumanDecision(f"human-decision:{signed['decision_id']}", task.task_id,
                                             plan.plan_hash, ALICE, signed["expires_at"])


# --- CR13b-01: issuance separated from verification; per-operator keys -----------------------------


def test_cr13b01_verifier_has_no_signing_path_and_retains_no_key(tmp_path: Path) -> None:
    authority = verifier(tmp_path)
    assert not signing_capable(authority)
    assert not any(hasattr(authority, name) for name in SIGNING_ATTRIBUTES)
    assert not hasattr(authority, "__dict__")  # slots only: nothing can be attached later
    _, task, plan = gated(tmp_path)
    authority.verify_and_consume(approve(task.task_id, plan.plan_hash), task_id=task.task_id,
                                 plan_hash=plan.plan_hash, now=NOW)
    held = json.dumps({slot: repr(getattr(authority, slot)) for slot in type(authority).__slots__})
    assert _alice_key() not in held and _bob_key() not in held
    with pytest.raises(TypeError, match="OPERATOR_ISSUER_REQUIRES_open_operator_issuer"):
        OperatorDecisionIssuer(object(), approver=ALICE, binding=OperatorBinding(ALICE_REF, this_account()),
                               boundary=boundary(), session=TypingHuman(), environ={})


def test_cr13b01_a_named_operator_verifies_only_under_that_operators_own_key(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    authority = verifier(tmp_path)
    # Bob's tool can only sign as Bob.
    assert approve(task.task_id, plan.plan_hash, by=issuer(BOB, BOB_REF))["operator"] == BOB
    # A caller naming Alice while holding only Bob's key cannot produce a verifiable decision.
    impersonated = approve(task.task_id, plan.plan_hash, by=issuer(ALICE, BOB_REF))
    assert impersonated["operator"] == ALICE
    forged = {**approve(task.task_id, plan.plan_hash, by=issuer(BOB, BOB_REF)), "operator": ALICE}
    forged["signature"] = hmac.new(_bob_key().encode(), signed_payload(forged), hashlib.sha256).hexdigest()
    for record in (impersonated, forged):
        with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_SIGNATURE_INVALID$"):
            fab.executor(human_decisions=authority).execute(plan, task, human_decision=record)
    assert no_adapter_ran(fab)
    genuine = approve(task.task_id, plan.plan_hash, by=issuer(BOB, BOB_REF))
    assert fab.executor(human_decisions=authority).execute(plan, task,
                                                           human_decision=genuine).status is ExecutionStatus.COMPLETED


def test_cr13b01_operators_cannot_share_a_key_and_issuers_refuse_automation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HUMAN_DECISION_OPERATOR_KEY_SHARED"):
        HmacHumanDecisionVerifier(operator_keys={ALICE: ALICE_REF, BOB: ALICE_REF}, boundary=boundary(),
                                  ledger=FileConsumedDecisionLedger(ledger_path(tmp_path)), environ={})
    with pytest.raises(ValueError, match="HUMAN_DECISION_KEY_REF_INVALID"):
        HmacHumanDecisionVerifier(operator_keys={ALICE: "plain-text"}, boundary=boundary(),
                                  ledger=FileConsumedDecisionLedger(ledger_path(tmp_path)), environ={})
    with pytest.raises(HumanDecisionRefused, match="HUMAN_DECISION_AUTOMATION_CONTEXT"):
        open_operator_issuer_at(TypingHuman(), approver="dependabot[bot]", operators=bindings(ALICE, ALICE_REF), boundary=boundary(),
                             environ={})
    with pytest.raises(HumanDecisionRefused, match="HUMAN_DECISION_AUTOMATION_CONTEXT"):
        open_operator_issuer_at(TypingHuman(), approver=ALICE, operators=bindings(ALICE, ALICE_REF), boundary=boundary(),
                             environ={"CI": "true"})
    with pytest.raises(ValueError, match="HUMAN_DECISION_KEY_REF_INVALID"):
        open_operator_issuer_at(TypingHuman(), approver=ALICE, operators=bindings(ALICE, "plain-text"), boundary=boundary(),
                             environ={})


def test_cr13b01_executor_refuses_signing_capable_verifiers(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    record = approve(task.task_id, plan.plan_hash)

    class SigningVerifier:
        durable_single_use = True

        def issue(self, **kwargs: Any) -> dict[str, str]:
            return record

        def verify_and_consume(self, record: Any, **kwargs: Any) -> VerifiedHumanDecision:
            return VerifiedHumanDecision(f"human-decision:{record['decision_id']}", task.task_id, plan.plan_hash,
                                         ALICE, record["expires_at"])

    with pytest.raises(ValueError, match="HUMAN_DECISION_VERIFIER_SIGNING_CAPABLE"):
        fab.executor(human_decisions=SigningVerifier())

    class LaterSigning:
        durable_single_use = True

        def verify_and_consume(self, record: Any, **kwargs: Any) -> VerifiedHumanDecision:
            raise AssertionError("never reached")

    late = LaterSigning()
    executor = fab.executor(human_decisions=late)
    late.sign = lambda payload: "0" * 64  # type: ignore[attr-defined]  # a signing path appears after wiring
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_VERIFIER_SIGNING_CAPABLE$"):
        executor.execute(plan, task, human_decision=record)
    assert no_adapter_ran(fab)


def test_cr13b01_residual_is_documented_with_the_exact_user_decision_marker() -> None:
    doc = Path(__file__).resolve().parents[2] / "docs" / "capability" / "CAPABILITY_FABRIC_V1.md"
    assert "ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED" in doc.read_text(encoding="utf-8")


# --- CR13b-02: durable, shared, atomic single-use ledger ------------------------------------------


def test_cr13b02_restart_with_new_verifier_and_ledger_refuses_reuse(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    record = approve(task.task_id, plan.plan_hash)
    first = fab.executor(human_decisions=verifier(tmp_path)).execute(plan, task, human_decision=record)
    assert first.status is ExecutionStatus.COMPLETED
    calls = len(fab.adapters["ocr.alpha"].calls)
    restarted = verifier(tmp_path, ledger=FileConsumedDecisionLedger(ledger_path(tmp_path)))  # "new process"
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        fab.executor(human_decisions=restarted).execute(plan, task, human_decision=record)
    assert len(fab.adapters["ocr.alpha"].calls) == calls
    lines = ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and set(json.loads(lines[0])) == LEDGER_KEYS
    assert json.loads(lines[0])["decision_id"] == record["decision_id"]
    assert task.task_id not in lines[0] and record["signature"] not in lines[0]  # decision ids only


def test_cr13b02_executor_requires_a_durable_ledger(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    record = approve(task.task_id, plan.plan_hash)
    ephemeral = verifier(tmp_path, ledger=in_memory_ledger_for_tests())
    assert not ephemeral.durable_single_use
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_LEDGER_NOT_DURABLE$"):
        fab.executor(human_decisions=ephemeral).execute(plan, task, human_decision=record)

    class Undeclared:
        def verify_and_consume(self, record: Any, **kwargs: Any) -> VerifiedHumanDecision:
            raise AssertionError("never reached")

    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_LEDGER_NOT_DURABLE$"):
        fab.executor(human_decisions=Undeclared()).execute(plan, task, human_decision=record)  # type: ignore[arg-type]
    assert no_adapter_ran(fab)
    assert verifier(tmp_path).durable_single_use


def test_cr13b02_two_processes_racing_on_one_id_yield_exactly_one_success(tmp_path: Path) -> None:
    _, task, plan = gated(tmp_path)
    record = approve(task.task_id, plan.plan_hash)
    path = ledger_path(tmp_path)
    FileConsumedDecisionLedger(path)  # creates the owner-only directory
    ctx = multiprocessing.get_context("spawn")
    contenders = 4
    barrier, results = ctx.Barrier(contenders), ctx.Queue()
    from decision_race_support import race_consume
    processes = [ctx.Process(target=race_consume, args=(
        str(path), ALICE_KEY_NAME, _alice_key(), ALICE, record, task.task_id, plan.plan_hash, NOW.isoformat(),
        barrier, results)) for _ in range(contenders)]
    for process in processes:
        process.start()
    outcomes = sorted(results.get(timeout=120) for _ in processes)
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0
    assert outcomes.count(f"human-decision:{record['decision_id']}") == 1
    assert outcomes.count("HUMAN_DECISION_ALREADY_CONSUMED") == contenders - 1, outcomes
    ledger = FileConsumedDecisionLedger(path)
    assert ledger.verify() and len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_cr13b02_ledger_is_hash_chained_and_fails_closed_on_tampering(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    ledger = FileConsumedDecisionLedger(path)
    for digit in "123":
        ledger.consume(digit * 32)
    assert ledger.verify() and ledger.is_consumed("2" * 32) and not ledger.is_consumed("4" * 32)
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert [entry["seq"] for entry in entries] == [1, 2, 3]
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"] and entries[0]["prev_hash"] == "0" * 64
    original = path.read_bytes()
    tampered = {
        "edited_id": "\n".join([lines[0], lines[1].replace("2" * 32, "9" * 32), lines[2]]) + "\n",
        "dropped_middle": "\n".join([lines[0], lines[2]]) + "\n",
        "reordered": "\n".join([lines[1], lines[0], lines[2]]) + "\n",
        "extra_field": "\n".join([lines[0], json.dumps({**entries[1], "task": "x"}), lines[2]]) + "\n",
        "torn_tail": "\n".join(lines) + "\n" + lines[0][:20],
    }
    for name, content in tampered.items():
        path.write_text(content, encoding="utf-8")
        assert not ledger.verify(), name
        code = "HUMAN_DECISION_LEDGER_TORN_ENTRY" if name == "torn_tail" else "HUMAN_DECISION_LEDGER_CORRUPT"
        with pytest.raises(HumanDecisionRefused, match=f"^{code}$"):
            ledger.consume("5" * 32)
        path.write_bytes(original)
    assert ledger.verify()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner/mode bits")
def test_cr13b02_ledger_is_owner_only(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    ledger = FileConsumedDecisionLedger(path)
    ledger.consume("1" * 32)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o077 == 0
    os.chmod(path, 0o644)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_UNSAFE$"):
        ledger.consume("2" * 32)
    os.chmod(path, 0o600)
    os.symlink(path, tmp_path / "decisions" / "link.jsonl")
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_UNSAFE$"):
        FileConsumedDecisionLedger(tmp_path / "decisions" / "link.jsonl").consume("3" * 32)


def test_cr13b02_entries_are_minimized_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = ledger_path(tmp_path)
    ledger = FileConsumedDecisionLedger(path)
    seen: list[tuple[frozenset[str], dict[str, Any]]] = []
    original = ledger_module.assert_minimized

    def recording(record: Any, **kwargs: Any) -> None:
        seen.append((frozenset(kwargs["allowed_keys"]), dict(record)))
        original(record, **kwargs)

    monkeypatch.setattr(ledger_module, "assert_minimized", recording)
    ledger.consume("1" * 32)
    assert seen and all(keys == LEDGER_KEYS and set(record) == LEDGER_KEYS for keys, record in seen)

    def refuse(record: Any, **kwargs: Any) -> None:
        raise DataMinimizationViolation("PROHIBITED_FIELD", "decision_id")

    monkeypatch.setattr(ledger_module, "assert_minimized", refuse)
    with pytest.raises(HumanDecisionRefused):
        ledger.consume("2" * 32)
    monkeypatch.setattr(ledger_module, "assert_minimized", original)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1 and not ledger.is_consumed("2" * 32)


def test_cr13b02_capacity_and_provisional_data_kind(tmp_path: Path) -> None:
    ledger = FileConsumedDecisionLedger(ledger_path(tmp_path), capacity=2)
    ledger.consume("1" * 32)
    ledger.consume("2" * 32)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_FULL$"):
        ledger.consume("3" * 32)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        ledger.consume("1" * 32)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_MALFORMED$"):
        ledger.consume("not-an-id")
    assert CAPABILITY_DECISION_LEDGER == "CAPABILITY_DECISION_LEDGER"
    assert CAPABILITY_DECISION_LEDGER_RETENTION is RetentionClass.R3_AUTHORITY
    catalogued = policy_for(DataKind.CAPABILITY_DECISION_LEDGER)
    assert catalogued.retention is CAPABILITY_DECISION_LEDGER_RETENTION and catalogued.authoritative
    assert catalogued.purge_rule is PurgeRule.NEVER
    memory = in_memory_ledger_for_tests(capacity=1)
    memory.consume("1" * 32)
    assert not memory.durable and memory.is_consumed("1" * 32)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        memory.consume("1" * 32)
