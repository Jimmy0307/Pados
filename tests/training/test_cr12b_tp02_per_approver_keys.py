"""CR-12b TP-02: per-approver keys, and a verifier that retains no key bytes and is no MAC oracle."""
from __future__ import annotations

import ast
import hashlib
import hmac
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.enterprise.secrets import SecretBoundary, SecretValue
from pados.execution.context import canonical_hash
from pados.training import decisions
from pados.training.decisions import (
    ConsumedDecisionLedger,
    DecisionBinding,
    DecisionCode,
    DecisionError,
    DecisionOperation,
    DecisionVerifier,
    TrainingOperatorRegistry,
    signed_payload,
)
from pados.training.operator import open_operator_signer

from .support import APPROVER, SECOND_APPROVER, START, FixedResolver, key_ref, make_keys, os_account, terminal

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "pados" / "training"
SUBJECT = canonical_hash({"subject": "tp02"})
BINDING = DecisionBinding(DecisionOperation.PROMOTE_MODEL, SUBJECT)
LATER = START + timedelta(minutes=5)
MAC_PRODUCERS = {("hmac", "new"), ("hmac", "digest"), ("hmac", "HMAC")}


def _issue(signer: Any) -> dict[str, Any]:
    return signer.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=SUBJECT, issued_at=START,
                        lifetime=timedelta(hours=1))


def _verifier(tmp: Path, keys: Any) -> DecisionVerifier:
    return DecisionVerifier(boundary=keys.boundary, registry=keys.registry,
                            ledger=ConsumedDecisionLedger(tmp / "decisions.jsonl"))


def _resign(record: dict[str, Any], key: str) -> dict[str, Any]:
    """What a holder of ``key`` can produce for an arbitrary record (test-side MAC, not package code)."""
    forged = dict(record)
    forged["signature"] = hmac.new(key.encode(), signed_payload(forged), hashlib.sha256).hexdigest()
    return forged


# --- (a) per-approver keys ---------------------------------------------------------------------------------


def test_each_approver_signs_with_their_own_key(tmp_path: Path) -> None:
    keys = make_keys()
    verifier = _verifier(tmp_path, keys)
    for approver in (APPROVER, SECOND_APPROVER):
        record = _issue(keys.signer(approver))
        assert record["approver"] == approver
        outcome = verifier.check(record, BINDING, now=LATER)
        assert outcome.code is DecisionCode.AUTHORIZED and outcome.approver == approver
    assert keys.registry.key_ref(APPROVER) != keys.registry.key_ref(SECOND_APPROVER)


def test_approver_a_key_cannot_sign_as_approver_b(tmp_path: Path) -> None:
    keys = make_keys()
    verifier = _verifier(tmp_path, keys)
    genuine = _issue(keys.signer(APPROVER))
    as_b = _resign(dict(genuine, approver=SECOND_APPROVER), keys.values[key_ref(APPROVER)])
    assert verifier.check(as_b, BINDING, now=LATER).code is DecisionCode.DECISION_SIGNATURE_INVALID
    renamed = dict(genuine, approver=SECOND_APPROVER)  # A's MAC with B's name
    assert verifier.check(renamed, BINDING, now=LATER).code is DecisionCode.DECISION_SIGNATURE_INVALID
    assert verifier.check(genuine, BINDING, now=LATER).code is DecisionCode.AUTHORIZED


def test_naming_an_approver_without_their_key_cannot_produce_a_verifiable_record(tmp_path: Path) -> None:
    keys = make_keys()
    only_a = SecretBoundary(FixedResolver({key_ref(APPROVER): keys.values[key_ref(APPROVER)]}))
    terminal().login(SECOND_APPROVER)  # even from B's own login, B's key must resolve
    with pytest.raises(DecisionError, match="DECISION_KEY_UNAVAILABLE"):
        open_operator_signer(approver=SECOND_APPROVER, boundary=only_a, registry=keys.registry)
    # A boundary that answers B's reference with A's key (a misconfigured or hostile operator tool) still
    # yields nothing the verifier accepts as B.
    swapped = SecretBoundary(FixedResolver({key_ref(SECOND_APPROVER): keys.values[key_ref(APPROVER)]}))
    record = _issue(open_operator_signer(approver=SECOND_APPROVER, boundary=swapped, registry=keys.registry))
    assert _verifier(tmp_path, keys).check(record, BINDING, now=LATER).code is DecisionCode.DECISION_SIGNATURE_INVALID


def test_unknown_or_disabled_approver_is_refused(tmp_path: Path) -> None:
    keys = make_keys()
    verifier = _verifier(tmp_path, keys)
    with pytest.raises(DecisionError, match="APPROVER_NOT_REGISTERED"):
        open_operator_signer(approver="eve.outsider", boundary=keys.boundary, registry=keys.registry)
    genuine = _issue(keys.signer(APPROVER))
    for key in keys.values.values():  # whatever registered key an outsider's record is MACed with
        outsider = _resign(dict(genuine, approver="eve.outsider"), key)
        assert verifier.check(outsider, BINDING, now=LATER).code is DecisionCode.APPROVER_NOT_REGISTERED
    disabled = TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": [
        {"operator_id": APPROVER, "enabled": False, "key_ref": key_ref(APPROVER), "os_account": os_account(APPROVER)}]})
    assert not disabled.allows(APPROVER) and disabled.key_ref(APPROVER) is None
    with pytest.raises(DecisionError, match="APPROVER_NOT_REGISTERED"):
        open_operator_signer(approver=APPROVER, boundary=keys.boundary, registry=disabled)
    off = DecisionVerifier(boundary=keys.boundary, registry=disabled,
                           ledger=ConsumedDecisionLedger(tmp_path / "other.jsonl"))
    assert off.check(genuine, BINDING, now=LATER).code is DecisionCode.APPROVER_NOT_REGISTERED


ALICE, CAROL = os_account("alice.operator"), os_account("carol.operator")


@pytest.mark.parametrize("operators, code", [
    ([{"operator_id": "alice.operator", "enabled": True, "key_ref": key_ref("x"), "os_account": ALICE},
      {"operator_id": "carol.operator", "enabled": True, "key_ref": key_ref("x"), "os_account": CAROL}],
     "OPERATOR_REGISTRY_KEY_SHARED"),
    ([{"operator_id": "alice.operator", "enabled": True, "key_ref": key_ref("a"), "os_account": ALICE},
      {"operator_id": "alice.operator", "enabled": False, "key_ref": key_ref("b"), "os_account": CAROL}],
     "OPERATOR_REGISTRY_DUPLICATE_PRINCIPAL"),
    ([{"operator_id": "alice.operator", "enabled": True, "os_account": ALICE}], "OPERATOR_REGISTRY_INVALID"),
    ([{"operator_id": "alice.operator", "enabled": "yes", "key_ref": key_ref("a"), "os_account": ALICE}],
     "OPERATOR_REGISTRY_INVALID"),
    ([{"operator_id": "pados-trainer", "enabled": True, "key_ref": key_ref("a"), "os_account": ALICE}],
     "OPERATOR_REGISTRY_PRINCIPAL_INVALID"),
])
def test_operator_registry_requires_one_distinct_key_per_human(operators: list[dict[str, Any]], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        TrainingOperatorRegistry.from_document({"schema": "pados.training-operators/v3", "operators": operators})
    for old in ("pados.training-operators/v1", "pados.training-operators/v2"):  # no per-human key / no OS account
        with pytest.raises(ValueError, match="OPERATOR_REGISTRY_INVALID"):
            TrainingOperatorRegistry.from_document({"schema": old, "operators": []})


def test_signer_identity_comes_from_the_signer_not_the_caller() -> None:
    keys = make_keys()
    signer = keys.signer(APPROVER)
    assert signer.approver == APPROVER
    with pytest.raises(TypeError):
        signer.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=SUBJECT,  # type: ignore[call-arg]
                     approver=SECOND_APPROVER, issued_at=START, lifetime=timedelta(hours=1))


# --- (b) the verifier keeps no key bytes and offers no MAC computation -------------------------------------


class CountingResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.calls = 0

    def resolve(self, ref: str) -> SecretValue | None:
        self.calls += 1
        value = self.values.get(ref)
        return SecretValue(value) if value else None


def test_key_is_resolved_at_verification_time_and_not_retained(tmp_path: Path) -> None:
    keys = make_keys()
    resolver = CountingResolver(keys.values)
    verifier = DecisionVerifier(boundary=SecretBoundary(resolver), registry=keys.registry,
                                ledger=ConsumedDecisionLedger(tmp_path / "decisions.jsonl"))
    assert resolver.calls == 0  # constructing the verifier resolves nothing
    record = _issue(keys.signer(APPROVER))
    assert verifier.verify(record, BINDING, at=LATER).ok and resolver.calls == 1
    assert verifier.verify(record, BINDING, at=LATER).ok and resolver.calls == 2
    resolver.values[key_ref(APPROVER)] = "r" * 64  # rotate: no cached key keeps verifying the old MAC
    assert verifier.verify(record, BINDING, at=LATER).code is DecisionCode.DECISION_SIGNATURE_INVALID
    del resolver.values[key_ref(APPROVER)]
    assert verifier.verify(record, BINDING, at=LATER).code is DecisionCode.DECISION_KEY_UNAVAILABLE
    secret_values = set(keys.values.values())
    for value in vars(verifier).values():
        assert not isinstance(value, bytes | bytearray)
        assert value not in secret_values


def test_verifier_api_is_verify_only() -> None:
    public = {name for name in dir(DecisionVerifier) if not name.startswith("_")}
    assert public == {"verify", "check", "consume", "consumed_matches", "anchoring"}
    for name in dir(DecisionVerifier):
        lowered = name.lower()
        assert not any(word in lowered for word in ("mac", "sign", "issue", "digest", "key")), name
    assert not hasattr(decisions, "HmacKey")
    public_functions = {name for name, value in vars(decisions).items()
                        if callable(value) and not name.startswith("_") and getattr(value, "__module__", "")
                        == decisions.__name__}
    # ``signed_payload`` only serializes the signed fields (no key involved); nothing public computes a MAC.
    suspicious = {name for name in public_functions for word in ("mac", "sign", "digest") if word in name.lower()}
    assert suspicious == {"signed_payload"}
    record = {name: "x" for name in decisions.SIGNED_FIELDS}
    assert decisions.signed_payload(record) == b'{"approver":"x","decision_id":"x","expires_at":"x","issued_at":"x",' \
        b'"operation":"x","origin":"x","schema":"x","subject_hash":"x"}'


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_call_to(node: ast.AST, pairs: set[tuple[str, str]]) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and (node.func.value.id, node.func.attr) in pairs)


def test_only_operator_py_produces_macs_outside_the_verification_compare() -> None:
    """Static: outside ``operator.py``, an HMAC is computed only inside ``decisions._signature_matches``,
    as a direct argument of ``hmac.compare_digest`` in a function returning ``bool``."""
    allowed_sites = 0
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"hmac", "hashlib"}:
                assert path.name == "operator.py", f"{path.name}: from {node.module} import ..."
            if isinstance(node, ast.Import) and any(a.asname for a in node.names if a.name in {"hmac", "hashlib"}):
                raise AssertionError(f"{path.name}: aliased hmac/hashlib import")
        if path.name == "operator.py":
            continue
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not _is_call_to(node, MAC_PRODUCERS):
                continue
            assert path.name == "decisions.py", f"{path.name} computes a MAC"
            function = node
            while not isinstance(function, ast.FunctionDef):
                function = parents[function]
            assert function.name == "_signature_matches", f"MAC computed in {function.name}"
            assert isinstance(function.returns, ast.Constant | ast.Name)
            assert getattr(function.returns, "value", getattr(function.returns, "id", None)) == "bool"
            digest = parents[node]
            assert isinstance(digest, ast.Attribute) and digest.attr == "hexdigest"
            call = parents[digest]
            compare = parents[call]
            assert _is_call_to(compare, {("hmac", "compare_digest")}), "MAC escapes the compare"
            allowed_sites += 1
    assert allowed_sites == 1
