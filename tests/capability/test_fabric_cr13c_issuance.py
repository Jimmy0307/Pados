"""CR13b-01 / CF-04: authenticated human issuance under the HMAC model.

The OS account is read from the OS (never injected): approvers are bound to the account this test process
really runs as, or to a different one. Only the terminal (TTY check, typed confirmation) is a seam."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fabric_support import fabric
from operator_support import (
    TypingHuman,
    bindings,
    open_approver,
    open_operator_issuer_at,
    other_account,
    run_cli_at,
    this_account,
)

import pados.capability.operator_decisions as operator_module
from pados.capability import operator_cli
from pados.capability.decision_ledger import FileConsumedDecisionLedger
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.human_decision import HmacHumanDecisionVerifier, HumanDecisionRefused
from pados.capability.operator_decisions import (
    WINDOWS_NATIVE_VALIDATION,
    OperatorAccount,
    OperatorBinding,
    TerminalSession,
    current_os_account,
    open_operator_issuer,
)
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary, SecretValue

ALICE, BOB = "alice.operator", "bob.operator"
ALICE_KEY_NAME, BOB_KEY_NAME = "PADOS_TEST_CR13C_KEY_ALICE", "PADOS_TEST_CR13C_KEY_BOB"
ALICE_REF, BOB_REF = f"secretref://env/{ALICE_KEY_NAME}", f"secretref://env/{BOB_KEY_NAME}"
ISSUED = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
PLAN_HASH = "ab" * 32


def _alice_key() -> str:
    return "alice-held-" + "a" * 40  # assembled at runtime


def _secrets() -> dict[str, str]:
    return {ALICE_KEY_NAME: _alice_key(), BOB_KEY_NAME: "bob-held-" + "b" * 40}


def boundary() -> SecretBoundary:
    return SecretBoundary(EnvironmentSecretResolver(_secrets()))


def open_alice(human: TypingHuman, *, account: OperatorAccount | None = None,
               environ: dict[str, str] | None = None) -> Any:
    return open_operator_issuer_at(human, approver=ALICE, operators=bindings(ALICE, ALICE_REF, account), boundary=boundary(),
                                environ={} if environ is None else environ)


def approve(issuer: Any, task_id: str = "task-1", plan_hash: str = PLAN_HASH) -> dict[str, str]:
    return dict(issuer.approve(task_id=task_id, plan_hash=plan_hash, issued_at=ISSUED, lifetime=timedelta(hours=2)))


# --- OS account binding ------------------------------------------------------------------------------


def test_the_bound_account_is_the_os_account_of_this_process() -> None:
    mine = current_os_account()
    if sys.platform == "win32":
        assert mine.login and WINDOWS_NATIVE_VALIDATION == "PENDING"
    else:
        import pwd

        assert mine.uid == os.getuid() and mine.login == pwd.getpwuid(os.getuid()).pw_name
    assert WINDOWS_NATIVE_VALIDATION == "PENDING"


def test_wrong_os_account_is_refused_at_open_and_nothing_is_signed() -> None:
    human = TypingHuman().confirms("task-1", PLAN_HASH)
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_MISMATCH$"):
        open_alice(human, account=other_account())
    wrong_login = dataclasses.replace(this_account(), login=this_account().login + "x")
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_MISMATCH$"):
        open_alice(human, account=wrong_login)
    assert human.prompts == []  # refused before any confirmation was even asked


def test_account_is_rechecked_at_the_approval_action(monkeypatch: pytest.MonkeyPatch) -> None:
    human = TypingHuman()
    issuer = open_alice(human)
    monkeypatch.setattr(operator_module, "current_os_account", other_account)  # e.g. a handed-over object
    human.confirms("task-1", PLAN_HASH)
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_MISMATCH$"):
        approve(issuer)
    monkeypatch.setattr(operator_module, "current_os_account", lambda: (_ for _ in ()).throw(KeyError("uid")))
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_MISMATCH$"):
        approve(issuer)


def test_operator_mapping_must_bind_every_approver_to_its_own_os_account() -> None:
    human = TypingHuman()
    mine = this_account()
    cases: dict[str, tuple[dict[str, Any], str]] = {
        "no_uid": ({ALICE: OperatorBinding(ALICE_REF, OperatorAccount(login=mine.login))},
                   "OPERATOR_ACCOUNT_BINDING_INCOMPLETE" if sys.platform != "win32" else "OPERATOR_SESSION_MISMATCH"),
        "shared_account": ({ALICE: OperatorBinding(ALICE_REF, mine), BOB: OperatorBinding(BOB_REF, mine)},
                           "OPERATOR_ACCOUNT_SHARED"),
        "shared_key": ({ALICE: OperatorBinding(ALICE_REF, mine), BOB: OperatorBinding(ALICE_REF, other_account())},
                       "HUMAN_DECISION_OPERATOR_KEY_SHARED"),
        "not_a_binding": ({ALICE: (ALICE_REF, mine)}, "OPERATOR_BINDINGS_INVALID"),
        "bad_name": ({"Not A Name": OperatorBinding(ALICE_REF, mine)}, "OPERATOR_BINDINGS_INVALID"),
        "empty": ({}, "OPERATOR_BINDINGS_INVALID"),
    }
    for name, (operators, code) in cases.items():
        with pytest.raises((ValueError, HumanDecisionRefused), match=f"^{code}$"):
            open_operator_issuer_at(human, approver=ALICE, operators=operators, boundary=boundary(), environ={})
        assert human.prompts == [], name
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_OPERATOR_NOT_REGISTERED$"):
        open_operator_issuer_at(human, approver=ALICE, operators=bindings(BOB, BOB_REF), boundary=boundary(), environ={})
    with pytest.raises(ValueError, match="^OPERATOR_ACCOUNT_INVALID$"):
        OperatorAccount(login="bad login")
    with pytest.raises(ValueError, match="^OPERATOR_ACCOUNT_INVALID$"):
        OperatorAccount(login="alice", uid=-1)


# --- interactive session ---------------------------------------------------------------------------


def test_non_interactive_sessions_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_NOT_INTERACTIVE$"):
        open_alice(TypingHuman(interactive=False))

    class Exploding(TypingHuman):
        def interactive(self) -> bool:
            raise OSError("no terminal")

    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_NOT_INTERACTIVE$"):
        open_alice(Exploding())
    human = TypingHuman()
    issuer = open_alice(human)
    human.is_interactive = False  # the terminal went away before the approval action
    human.confirms("task-1", PLAN_HASH)
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_NOT_INTERACTIVE$"):
        approve(issuer)
    # The real terminal session: piped (non-TTY) stdin/stdout is not interactive, and it is the default.
    monkeypatch.setattr(sys, "stdin", io.StringIO(PLAN_HASH[:12] + "\ntask-1\n"))
    assert TerminalSession().interactive() is False
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_NOT_INTERACTIVE$"):
        open_operator_issuer(approver=ALICE, operators=bindings(ALICE, ALICE_REF), boundary=boundary(), environ={})


# --- explicit confirmation -------------------------------------------------------------------------


def test_wrong_confirmation_signs_nothing() -> None:
    wrong: dict[str, tuple[str, ...]] = {
        "wrong_hash_prefix": ("cd" * 6, "task-1"),
        "short_hash_prefix": (PLAN_HASH[:11], "task-1"),
        "full_hash_instead": (PLAN_HASH, "task-1"),
        "wrong_task": (PLAN_HASH[:12], "task-2"),
        "swapped": ("task-1", PLAN_HASH[:12]),
        "nothing_typed": (),
        "too_long": (PLAN_HASH[:12] + " " * 200, "task-1"),
    }
    for name, typed in wrong.items():
        human = TypingHuman()
        issuer = open_alice(human)
        human.will_type(*typed)
        with pytest.raises(HumanDecisionRefused, match="^OPERATOR_CONFIRMATION_MISMATCH$"):
            approve(issuer)
        assert len(human.prompts) == 2, name

    class BrokenReader(TypingHuman):
        def read_confirmation(self, prompt: str) -> str:
            raise EOFError

    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_CONFIRMATION_MISMATCH$"):
        approve(open_alice(BrokenReader()))


def test_correct_confirmation_signs_a_decision_that_runs_the_gated_task_once(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task(human_gate=True)
    plan = fab.planner().plan(task)
    human = TypingHuman()
    issuer = open_alice(human)
    human.confirms(task.task_id, plan.plan_hash)
    record = approve(issuer, task.task_id, plan.plan_hash)
    assert record["operator"] == ALICE and record["origin"] == "HUMAN_OPERATOR"
    assert all(plan.plan_hash not in prompt and plan.plan_hash[:12] not in prompt for prompt in human.prompts)
    assert len(human.prompts) == 2 and not human.typed
    verifier = HmacHumanDecisionVerifier(operator_keys={ALICE: ALICE_REF}, boundary=boundary(), environ={},
                                         ledger=FileConsumedDecisionLedger(tmp_path / "decisions" / "consumed.jsonl"))
    result = fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    assert result.status is ExecutionStatus.COMPLETED
    assert result.plan_receipt.human_decision_ref == f"human-decision:{record['decision_id']}"
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)


# --- no automation -----------------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "JENKINS_URL", "TF_BUILD"])
def test_ci_markers_refuse_at_open_and_at_approval(marker: str) -> None:
    human = TypingHuman().confirms("task-1", PLAN_HASH)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_AUTOMATION_CONTEXT$"):
        open_alice(human, environ={marker: "true"})
    environ: dict[str, str] = {}
    issuer = open_alice(human, environ=environ)
    environ[marker] = "true"  # automation detected by the time of the approval action
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_AUTOMATION_CONTEXT$"):
        approve(issuer)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_AUTOMATION_CONTEXT$"):
        open_operator_issuer_at(human, approver="github-actions", operators=bindings(ALICE, ALICE_REF), boundary=boundary(),
                             environ={})


# --- verifier secret exposure: key references only, resolved at compare time, nothing retained ---------


class CountingResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.resolutions = 0

    def resolve(self, ref: str) -> SecretValue | None:
        self.resolutions += 1
        value = self.values.get(ref.removeprefix("secretref://env/"))
        return SecretValue(value) if value else None


def test_verifier_holds_only_key_references_and_resolves_per_comparison(tmp_path: Path) -> None:
    resolver = CountingResolver(_secrets())
    verifier = HmacHumanDecisionVerifier(operator_keys={ALICE: ALICE_REF, BOB: BOB_REF},
                                         boundary=SecretBoundary(resolver), environ={},
                                         ledger=FileConsumedDecisionLedger(tmp_path / "decisions" / "consumed.jsonl"))
    held = {slot: getattr(verifier, slot) for slot in type(verifier).__slots__}
    assert dict(held["_operator_keys"]) == {ALICE: ALICE_REF, BOB: BOB_REF}  # references only
    for round_ in range(3):
        human = TypingHuman()
        issuer = open_alice(human)
        human.confirms("task-1", PLAN_HASH)
        record = approve(issuer)
        verifier.verify_and_consume(record, task_id="task-1", plan_hash=PLAN_HASH, now=NOW)
        assert resolver.resolutions == round_ + 1  # resolved at compare time, every time (no cache)
    after = {slot: getattr(verifier, slot) for slot in type(verifier).__slots__}
    assert after == held  # nothing new retained
    text = json.dumps({slot: repr(value) for slot, value in after.items()})
    assert _alice_key() not in text and "SecretValue" not in text
    assert not any(isinstance(value, (SecretValue, bytes)) for value in after.values())


# --- the operator entry module -----------------------------------------------------------------------


def _bindings_file(tmp_path: Path, *, mode: int = 0o600, account: OperatorAccount | None = None) -> Path:
    mine = account or this_account()
    entry: dict[str, Any] = {"key_ref": ALICE_REF, "login": mine.login}
    entry.update({"uid": mine.uid} if mine.uid is not None else {"domain": mine.domain})
    path = tmp_path / "operators.json"
    path.write_text(json.dumps({"schema": operator_cli.BINDINGS_SCHEMA, "operators": {ALICE: entry}}),
                    encoding="utf-8")
    os.chmod(path, mode)
    return path


def _cli(tmp_path: Path, human: TypingHuman | None, *, environ: dict[str, str] | None = None,
         operators: Path | None = None, task_id: str = "task-1", plan_hash: str = PLAN_HASH) -> int:
    argv = ["--operators", str(operators or _bindings_file(tmp_path)), "--approver", ALICE, "--task-id", task_id,
            "--plan-hash", plan_hash, "--out", str(tmp_path / "decision.json"), "--lifetime-minutes", "90"]
    return run_cli_at(human, argv, environ=_secrets() if environ is None else environ,
                             clock=lambda: ISSUED)


def test_operator_cli_writes_one_owner_only_verifiable_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]
                                                              ) -> None:
    fab = fabric(tmp_path / "fabric")
    task = fab.task(human_gate=True)
    plan = fab.planner().plan(task)
    human = TypingHuman().confirms(task.task_id, plan.plan_hash)
    assert _cli(tmp_path, human, task_id=task.task_id, plan_hash=plan.plan_hash) == 0
    out = tmp_path / "decision.json"
    if sys.platform != "win32":
        assert out.stat().st_mode & 0o777 == 0o600
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["operator"] == ALICE and record["task_id"] == task.task_id and record["plan_hash"] == plan.plan_hash
    assert _alice_key() not in out.read_text(encoding="utf-8") and _alice_key() not in capsys.readouterr().out
    verifier = HmacHumanDecisionVerifier(operator_keys={ALICE: ALICE_REF}, boundary=boundary(), environ={},
                                         ledger=FileConsumedDecisionLedger(tmp_path / "decisions" / "consumed.jsonl"))
    result = fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    assert result.status is ExecutionStatus.COMPLETED
    human.confirms(task.task_id, plan.plan_hash)
    assert _cli(tmp_path, human, task_id=task.task_id, plan_hash=plan.plan_hash) == 1  # never overwrites
    assert "REFUSED OPERATOR_DECISION_OUTPUT_UNAVAILABLE" in capsys.readouterr().err


def test_operator_cli_refusals_sign_and_write_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "decision.json"
    cases: dict[str, tuple[Any, str]] = {
        "wrong_confirmation": (lambda: _cli(tmp_path, TypingHuman().will_type("0" * 12, "task-1")),
                               "OPERATOR_CONFIRMATION_MISMATCH"),
        "ci": (lambda: _cli(tmp_path, TypingHuman().confirms("task-1", PLAN_HASH),
                            environ={**_secrets(), "GITHUB_ACTIONS": "true"}), "HUMAN_DECISION_AUTOMATION_CONTEXT"),
        "wrong_account": (lambda: _cli(tmp_path, TypingHuman().confirms("task-1", PLAN_HASH),
                                       operators=_bindings_file(tmp_path, account=other_account())),
                          "OPERATOR_SESSION_MISMATCH"),
        "no_key": (lambda: _cli(tmp_path, TypingHuman().confirms("task-1", PLAN_HASH), environ={}),
                   "HUMAN_DECISION_KEY_UNAVAILABLE"),
        "bad_bindings": (lambda: _cli(tmp_path, TypingHuman(), operators=tmp_path / "missing.json"),
                         "OPERATOR_BINDINGS_UNAVAILABLE"),
    }
    if sys.platform != "win32":
        cases["group_readable_bindings"] = (lambda: _cli(tmp_path, TypingHuman().confirms("task-1", PLAN_HASH),
                                                         operators=_bindings_file(tmp_path, mode=0o640)),
                                            "OPERATOR_BINDINGS_UNSAFE")
    for name, (run, code) in cases.items():
        assert run() == 1, name
        assert f"REFUSED {code}" in capsys.readouterr().err, name
        assert not out.exists(), name
    monkeypatch.setattr(sys, "stdin", io.StringIO(PLAN_HASH[:12] + "\ntask-1\n"))  # piped: not a terminal
    assert _cli(tmp_path, None) == 1
    assert "REFUSED OPERATOR_SESSION_NOT_INTERACTIVE" in capsys.readouterr().err and not out.exists()


def test_open_approver_helper_uses_the_real_os_account() -> None:
    approver = open_approver(ALICE, ALICE_REF, boundary())
    assert approver.issuer.approver == ALICE


def test_cr13d_no_caller_can_supply_the_operator_session() -> None:
    """CR13d (integrator mitigation): production APIs take no session; only the private seam exists."""
    import inspect

    assert "session" not in inspect.signature(open_operator_issuer).parameters
    assert "session" not in inspect.signature(operator_cli.main).parameters
    assert operator_module._session_factory is TerminalSession


def test_cr13d_confirmation_is_read_from_the_controlling_terminal_never_stdin(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import io

    class Untouchable(io.StringIO):
        def readline(self, *args: Any) -> str:
            raise AssertionError("stdin must never be read for a confirmation")

    real_open = builtins.open

    def no_tty(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == "/dev/tty":
            raise OSError("no controlling terminal")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sys, "stdin", Untouchable("abcdef012345\n"))
    monkeypatch.setattr(builtins, "open", no_tty)
    with pytest.raises(HumanDecisionRefused, match="^OPERATOR_SESSION_NOT_INTERACTIVE$"):
        TerminalSession().read_confirmation("type: ")


def test_cr13d_windows_issuance_fails_closed() -> None:
    """CR13d-01: on win32 the environment-derived account is never trusted; issuance refuses."""
    import subprocess

    code = ("import sys; sys.platform = 'win32'\n"
            "from pados.capability import operator_decisions as m\n"
            "try:\n    m.current_os_account()\nexcept m.HumanDecisionRefused as exc:\n    print(exc.code)\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={"PATH": "", "USERNAME": "alice", "USERDOMAIN": "CORP"}, check=False)
    assert done.stdout.strip() == "OPERATOR_PLATFORM_UNSUPPORTED", done.stderr[-2000:]
