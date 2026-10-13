"""CR-12c TP-02: signing needs the approver's own OS login, an interactive terminal and typed human confirmation;
the signer lives behind an operator-side entry module that no training service module imports."""
from __future__ import annotations

import ast
import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.execution.context import canonical_hash
from pados.training import operator, operator_cli
from pados.training.decisions import (
    ConsumedDecisionLedger,
    DecisionBinding,
    DecisionCode,
    DecisionError,
    DecisionOperation,
    DecisionVerifier,
    OsAccount,
    TrainingOperatorRegistry,
    parse_time,
)
from pados.training.operator import ConfirmationRequest, DecisionSigner, OperatorSession, open_operator_signer

from .conftest import REAL_CURRENT_SESSION
from .support import APPROVER, SECOND_APPROVER, START, Terminal, make_keys, operator_entry, os_account, session_for

SRC = Path(__file__).resolve().parents[2] / "src" / "pados"
SUBJECT = canonical_hash({"subject": "cr12c-tp02"})
BINDING = DecisionBinding(DecisionOperation.PROMOTE_MODEL, SUBJECT)
LATER = START + timedelta(minutes=5)
SERVICE_MODULES = ("jobs.py", "registry.py", "evaluation.py", "backends.py", "contracts.py", "decisions.py",
                   "_chain.py")


def _issue(signer: DecisionSigner, operation: DecisionOperation = DecisionOperation.PROMOTE_MODEL) -> dict[str, Any]:
    return signer.issue(operation=operation, subject_hash=SUBJECT, issued_at=START, lifetime=timedelta(hours=1))


def _verifier(tmp: Path, keys: Any) -> DecisionVerifier:
    return DecisionVerifier(boundary=keys.boundary, registry=keys.registry,
                            ledger=ConsumedDecisionLedger(tmp / "decisions.jsonl"))


# --- OS account binding -------------------------------------------------------------------------------------


def test_wrong_os_account_cannot_open_or_use_a_signer(operator_terminal: Terminal) -> None:
    keys = make_keys()
    alice = os_account(APPROVER)
    for session in (OperatorSession("posix", alice["login"], alice["uid"] + 1, None, True),  # wrong uid
                    OperatorSession("posix", "mallory", alice["uid"], None, True),  # right uid, other login
                    session_for(SECOND_APPROVER),  # another registered approver's login
                    OperatorSession("windows", alice["login"], None, "CORP", True)):  # another platform
        operator_terminal.session = session
        with pytest.raises(DecisionError, match="OPERATOR_SESSION_MISMATCH"):
            open_operator_signer(approver=APPROVER, boundary=keys.boundary, registry=keys.registry)
    signer = keys.signer(APPROVER)  # opened from alice's own login...
    operator_terminal.login(SECOND_APPROVER)  # ...then used from carol's
    with pytest.raises(DecisionError, match="OPERATOR_SESSION_MISMATCH"):
        _issue(signer)
    bypass = DecisionSigner(approver=APPROVER, registry=keys.registry, boundary=keys.boundary)
    with pytest.raises(DecisionError, match="OPERATOR_SESSION_MISMATCH"):  # direct construction checks too
        _issue(bypass)
    assert operator_terminal.prompts == []  # nothing was ever put in front of a human


def test_non_interactive_session_is_refused(operator_terminal: Terminal) -> None:
    keys = make_keys()
    operator_terminal.session = session_for(APPROVER, interactive=False)
    with pytest.raises(DecisionError, match="OPERATOR_SESSION_NOT_INTERACTIVE"):
        open_operator_signer(approver=APPROVER, boundary=keys.boundary, registry=keys.registry)
    operator_terminal.session = session_for(APPROVER)
    signer = open_operator_signer(approver=APPROVER, boundary=keys.boundary, registry=keys.registry)
    operator_terminal.session = session_for(APPROVER, interactive=False)  # stdin/stdout redirected later
    with pytest.raises(DecisionError, match="OPERATOR_SESSION_NOT_INTERACTIVE"):
        _issue(signer)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account lookup")
def test_real_session_reports_this_process_and_is_not_interactive_under_pytest() -> None:
    import pwd

    session = REAL_CURRENT_SESSION()
    assert session.platform == "posix" and session.uid == os.getuid()
    assert session.login == pwd.getpwuid(os.getuid()).pw_name
    assert session.interactive is False  # pytest captures stdout: automation never looks interactive


def test_windows_signing_fails_closed_until_token_identity_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-12d-02 (integrator mitigation): environment-derived Windows identity is spoofable, so the real session
    refuses on win32 (WINDOWS_NATIVE_VALIDATION = PENDING) instead of trusting USERNAME/USERDOMAIN."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    with pytest.raises(DecisionError, match="^OPERATOR_PLATFORM_UNSUPPORTED$"):
        REAL_CURRENT_SESSION()  # the real seam (the autouse fixture replaces ``operator._current_session``)


def test_windows_account_binding_is_case_insensitive_and_domain_bound() -> None:
    account = OsAccount.from_document({"platform": "windows", "domain": "CORP", "login": "Alice"})
    assert OperatorSession("windows", "alice", None, "corp", True).is_account(account)
    assert not OperatorSession("windows", "alice", None, "OTHER", True).is_account(account)
    assert not OperatorSession("windows", "alice", None, None, True).is_account(account)


@pytest.mark.parametrize("account, code", [
    ({"platform": "posix", "uid": 0, "login": "alice"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "posix", "uid": 1001, "login": "root"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "posix", "uid": True, "login": "alice"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "posix", "login": "alice"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "windows", "domain": "NT AUTHORITY", "login": "alice"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "windows", "domain": "CORP", "login": "SYSTEM"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    ({"platform": "vms", "login": "alice"}, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
    (None, "OPERATOR_REGISTRY_ACCOUNT_INVALID"),
])
def test_operator_registry_binds_each_approver_to_one_unprivileged_os_account(account: Any, code: str) -> None:
    with pytest.raises(ValueError, match=code):
        TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
            dict(operator_entry(APPROVER), os_account=account)]})


def test_two_approvers_cannot_share_an_os_account() -> None:
    shared = os_account(APPROVER)
    with pytest.raises(ValueError, match="OPERATOR_REGISTRY_ACCOUNT_SHARED"):
        TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
            operator_entry(APPROVER), dict(operator_entry(SECOND_APPROVER), os_account=shared)]})
    with pytest.raises(ValueError, match="OPERATOR_REGISTRY_INVALID"):  # the binding is mandatory
        TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
            {k: v for k, v in operator_entry(APPROVER).items() if k != "os_account"}]})
    registry = TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
        operator_entry(APPROVER)]})
    assert registry.os_account(APPROVER) == OsAccount("posix", shared["login"], uid=shared["uid"])
    assert registry.os_account(SECOND_APPROVER) is None


# --- typed human confirmation -------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", [
    lambda r: "",
    lambda r: "yes",
    lambda r: r.subject_hash[:12],  # no operation
    lambda r: f"{r.operation.value}",  # no subject
    lambda r: f"REJECT_MODEL {r.subject_hash[:12]}",  # another operation
    lambda r: f"{r.operation.value} {r.subject_hash[:11]}",  # too short
    lambda r: f"{r.operation.value} {r.subject_hash[:13]}",  # too long
    lambda r: f"{r.operation.value} {r.subject_hash}",  # the whole hash pasted
    lambda r: f"{r.operation.value} {r.subject_hash[1:13]}",  # not the prefix
    lambda r: f"{r.operation.value.lower()} {r.subject_hash[:12]}",
])
def test_wrong_confirmation_is_refused_and_signs_nothing(tmp_path: Path, operator_terminal: Terminal,
                                                         reply: Any) -> None:
    keys = make_keys()
    signer = keys.signer(APPROVER)
    operator_terminal.reply = reply
    with pytest.raises(DecisionError, match="OPERATOR_CONFIRMATION_MISMATCH"):
        _issue(signer)
    assert len(operator_terminal.prompts) == 1


def test_correct_confirmation_signs_a_verifiable_decision(tmp_path: Path, operator_terminal: Terminal) -> None:
    keys = make_keys()
    signer = keys.signer(APPROVER)
    operator_terminal.reply = lambda r: f"  {r.operation.value}\t {r.subject_hash[:12]} \n"  # spacing is forgiven
    record = _issue(signer)
    [request] = operator_terminal.prompts
    assert request == ConfirmationRequest(APPROVER, DecisionOperation.PROMOTE_MODEL, SUBJECT, record["expires_at"])
    assert request.expected == f"PROMOTE_MODEL {SUBJECT[:12]}"
    outcome = _verifier(tmp_path, keys).check(record, BINDING, now=LATER)
    assert outcome.code is DecisionCode.AUTHORIZED and outcome.approver == APPROVER


def test_ci_or_automation_markers_are_still_refused(operator_terminal: Terminal,
                                                   hermetic_decision_environment: dict[str, str]) -> None:
    keys = make_keys()
    signer = keys.signer(APPROVER)
    for marker in ("CI", "GITHUB_ACTIONS", "RUNNER_TEMP"):
        hermetic_decision_environment.clear()
        hermetic_decision_environment[marker] = "1"
        with pytest.raises(DecisionError, match="AUTOMATION_CANNOT_DECIDE"):
            open_operator_signer(approver=APPROVER, boundary=keys.boundary, registry=keys.registry)
        with pytest.raises(DecisionError, match="AUTOMATION_CANNOT_DECIDE"):
            _issue(signer)
    assert operator_terminal.prompts == []


# --- the operator entry module ------------------------------------------------------------------------------


def _cli_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], TrainingOperatorRegistry]:
    variable = "TRAINING_OPERATOR_KEY_" + secrets.token_hex(4).upper()
    monkeypatch.setenv(variable, secrets.token_hex(32))
    document = {"schema": "pados.training-operators/v3",
                "operators": [dict(operator_entry(APPROVER), key_ref=f"secretref://env/{variable}")]}
    (tmp_path / "operators.json").write_text(json.dumps(document), encoding="utf-8")
    argv = ["--operators", str(tmp_path / "operators.json"), "--approver", APPROVER, "--operation", "PROMOTE_MODEL",
            "--subject", SUBJECT, "--lifetime-minutes", "30", "--out", str(tmp_path / "decision.json")]
    return argv, TrainingOperatorRegistry.from_document(document)


def test_operator_cli_signs_after_confirmation_and_writes_a_new_record(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    argv, registry = _cli_setup(tmp_path, monkeypatch)
    assert operator_cli.main(argv) == 0
    record = json.loads((tmp_path / "decision.json").read_text(encoding="utf-8"))
    assert record["approver"] == APPROVER and record["subject_hash"] == SUBJECT
    assert f"DECISION_ISSUED {record['decision_id']}" in capsys.readouterr().out
    verifier = DecisionVerifier(boundary=SecretBoundary(EnvironmentSecretResolver()), registry=registry,
                                ledger=ConsumedDecisionLedger(tmp_path / "decisions.jsonl"))
    outcome = verifier.verify(record, BINDING, at=parse_time(record["issued_at"]) + timedelta(seconds=1))
    assert outcome.code is DecisionCode.VERIFIED and outcome.approver == APPROVER
    assert parse_time(record["expires_at"]) - parse_time(record["issued_at"]) == timedelta(minutes=30)
    assert operator_cli.main(argv) == 2  # never overwrites an existing decision file
    assert "DECISION_OUTPUT_EXISTS" in capsys.readouterr().err


@pytest.mark.parametrize("arrange, code", [
    (lambda t: setattr(t, "reply", lambda r: "PROMOTE_MODEL 000000000000"), "OPERATOR_CONFIRMATION_MISMATCH"),
    (lambda t: setattr(t, "session", session_for(APPROVER, interactive=False)), "OPERATOR_SESSION_NOT_INTERACTIVE"),
    (lambda t: setattr(t, "session", session_for(SECOND_APPROVER)), "OPERATOR_SESSION_MISMATCH"),
])
def test_operator_cli_refuses_without_session_or_confirmation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        operator_terminal: Terminal, arrange: Any, code: str) -> None:
    argv, _ = _cli_setup(tmp_path, monkeypatch)
    arrange(operator_terminal)
    assert operator_cli.main(argv) == 2
    assert code in capsys.readouterr().err
    assert not (tmp_path / "decision.json").exists()


def test_operator_cli_refuses_in_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str],
                                   hermetic_decision_environment: dict[str, str]) -> None:
    argv, _ = _cli_setup(tmp_path, monkeypatch)
    hermetic_decision_environment["GITHUB_ACTIONS"] = "true"
    assert operator_cli.main(argv) == 2
    assert "AUTOMATION_CANNOT_DECIDE" in capsys.readouterr().err


# --- the operator-process boundary (static) -----------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    return _imports_of(path.read_text(encoding="utf-8-sig"), ".".join(path.relative_to(SRC.parent).parts[:-1]))


def _imports_of(source: str, package: str) -> set[str]:
    """Absolute module names imported by ``source`` living in ``package`` (relative imports resolved;
    ``from pados.training import operator`` counts as importing ``pados.training.operator``)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parent, *([base] if base else [])])
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def test_operator_module_is_imported_only_by_the_operator_entry_module() -> None:
    importers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                 if "pados.training.operator" in _imported_modules(path)}
    assert importers == {"training/operator_cli.py"}
    cli_importers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                     if "pados.training.operator_cli" in _imported_modules(path)}
    assert cli_importers <= {path.relative_to(SRC).as_posix() for path in (SRC / "cli").rglob("*.py")}
    assert "pados.training._chain" in _imported_modules(SRC / "training" / "jobs.py")
    for name in SERVICE_MODULES:
        imported = _imported_modules(SRC / "training" / name)
        assert not imported & {"pados.training.operator", "pados.training.operator_cli"}, name
    assert callable(operator_cli.main) and operator_cli.open_operator_signer is operator.open_operator_signer


@pytest.mark.parametrize("source", [
    "from . import operator as op", "from .operator import DecisionSigner", "import pados.training.operator as op",
    "from pados.training import operator", "from .. import training\nfrom ..training.operator import x",
])
def test_the_boundary_check_sees_relative_and_aliased_imports(source: str) -> None:
    assert "pados.training.operator" in _imports_of(source, "pados.training")
    assert "pados.training.jobs" not in _imports_of(source, "pados.training")
