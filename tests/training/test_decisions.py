"""Signed training decisions: schema, signature, binding, window, automation refusal, single use."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pados.enterprise.secrets import SecretBoundary
from pados.execution.context import canonical_hash
from pados.training.decisions import (
    DecisionBinding,
    DecisionCode,
    DecisionError,
    DecisionOperation,
    DecisionVerifier,
    TrainingOperatorRegistry,
)
from pados.training.operator import DecisionSigner, open_operator_signer

from .support import APPROVER, START, FixedResolver, make_pair, operator_entry, operator_registry

SUBJECT = canonical_hash({"subject": 1})
BINDING = DecisionBinding(DecisionOperation.PROMOTE_MODEL, SUBJECT)
LATER = START + timedelta(minutes=5)


def _issue(tmp: Path, **overrides: object) -> tuple[DecisionSigner, DecisionVerifier, dict]:
    signer, verifier = make_pair(tmp)
    values: dict = dict(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=SUBJECT, issued_at=START,
                        lifetime=timedelta(hours=1))
    values.update(overrides)
    return signer, verifier, signer.issue(**values)


def test_valid_decision_is_authorized_then_consumed_once(tmp_path: Path) -> None:
    _, verifier, record = _issue(tmp_path)
    assert verifier.check(record, BINDING, now=LATER).code is DecisionCode.AUTHORIZED
    consumed = verifier.consume(record, BINDING, now=LATER)
    assert consumed.code is DecisionCode.CONSUMED and consumed.approver == APPROVER
    assert verifier.consume(record, BINDING, now=LATER).code is DecisionCode.DECISION_ALREADY_CONSUMED
    ledger_line = json.loads((tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert ledger_line["subject_hash"] == SUBJECT
    assert ledger_line["decision"] == record  # the full signed record is retained (CR-12 TP-01)
    assert verifier.consumed_matches(record, BINDING)


@pytest.mark.parametrize("now, code", [
    (START - timedelta(seconds=1), DecisionCode.DECISION_NOT_YET_VALID),
    (START + timedelta(hours=1), DecisionCode.DECISION_EXPIRED),
])
def test_time_window(tmp_path: Path, now: datetime, code: DecisionCode) -> None:
    _, verifier, record = _issue(tmp_path)
    assert verifier.check(record, BINDING, now=now).code is code
    assert verifier.verify(record, BINDING, at=now).code is code


def test_lifetime_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(DecisionError, match="DECISION_LIFETIME_INVALID"):
        _issue(tmp_path, lifetime=timedelta(hours=25))
    _, verifier, record = _issue(tmp_path)
    stretched = dict(record, expires_at=(START + timedelta(days=3)).isoformat())
    assert verifier.check(stretched, BINDING, now=LATER).code is DecisionCode.DECISION_SIGNATURE_INVALID


def test_unregistered_or_disabled_approver_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DecisionError, match="APPROVER_NOT_REGISTERED"):
        open_operator_signer(approver="eve.outsider", boundary=SecretBoundary(FixedResolver({})),
                             registry=operator_registry())
    assert not operator_registry(()).allows(APPROVER)


def test_automation_context_cannot_sign_or_consume(tmp_path: Path,
                                                   hermetic_decision_environment: dict[str, str]) -> None:
    signer, verifier, record = _issue(tmp_path)
    for marker in ("GITHUB_ACTIONS", "CI", "RUNNER_TEMP"):
        hermetic_decision_environment.clear()
        hermetic_decision_environment[marker] = "1"
        assert verifier.check(record, BINDING, now=LATER).code is DecisionCode.AUTOMATION_CANNOT_DECIDE
        with pytest.raises(DecisionError, match="AUTOMATION_CANNOT_DECIDE"):
            signer.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=SUBJECT, issued_at=START,
                         lifetime=timedelta(hours=1))
        with pytest.raises(DecisionError, match="AUTOMATION_CANNOT_DECIDE"):
            open_operator_signer(approver=APPROVER, boundary=SecretBoundary(FixedResolver({})),
                                 registry=operator_registry())
    hermetic_decision_environment.clear()
    marked = dict(record, job_origin="x")
    assert verifier.check(marked, BINDING, now=LATER).code is DecisionCode.DECISION_MALFORMED


def test_key_must_be_resolvable_and_long_enough(tmp_path: Path) -> None:
    with pytest.raises(DecisionError, match="DECISION_KEY_UNAVAILABLE"):
        make_pair(tmp_path, key="k" * 8)  # the approver's own key is resolved when the signer is opened
    with pytest.raises(DecisionError, match="DECISION_KEY_UNAVAILABLE"):
        open_operator_signer(approver=APPROVER, boundary=SecretBoundary(FixedResolver({})),
                             registry=operator_registry())
    with pytest.raises(ValueError, match="OPERATOR_REGISTRY_KEY_REF_INVALID"):
        TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
            dict(operator_entry(APPROVER), key_ref="plain-key")]})
    with pytest.raises(TypeError, match="DECISION_VERIFIER_CONFIGURATION_INVALID"):
        DecisionVerifier(boundary=None, registry=operator_registry(), ledger=None)  # type: ignore[arg-type]


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    _, verifier, record = _issue(tmp_path)
    verifier.consume(record, BINDING, now=LATER)
    path = tmp_path / "decisions.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace(SUBJECT, "c" * 64), encoding="utf-8")
    assert verifier.check(record, BINDING, now=LATER).code is DecisionCode.DECISION_LEDGER_CORRUPT
    assert verifier.consume(record, BINDING, now=LATER).code is DecisionCode.DECISION_LEDGER_CORRUPT
    assert not verifier.consumed_matches(record, BINDING)
