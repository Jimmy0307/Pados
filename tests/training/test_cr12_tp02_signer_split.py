"""CR-12 TP-02: operator signing is separate from training-side verification."""
from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from pados.training import decisions
from pados.training.backends import default_backends
from pados.training.decisions import DecisionOperation, DecisionVerifier, require_verifier
from pados.training.jobs import TrainingJobs
from pados.training.operator import DecisionSigner, open_operator_signer
from pados.training.registry import ModelRegistry

from .support import APPROVER, START, make_env, make_pair

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "pados" / "training"
TRAINING_SIDE = ("jobs.py", "registry.py", "evaluation.py", "backends.py", "contracts.py", "_chain.py")


def _tree(name: str) -> ast.Module:
    return ast.parse((PACKAGE / name).read_text(encoding="utf-8"))


def test_cr12_training_side_never_imports_or_names_a_signer() -> None:
    for name in TRAINING_SIDE:
        tree = _tree(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "pados.training.operator", name
                assert not (node.module == "pados.training" and any(a.name == "operator" for a in node.names)), name
            if isinstance(node, ast.Import):
                assert all(alias.name != "pados.training.operator" for alias in node.names), name
            if isinstance(node, ast.Name | ast.Attribute):
                label = node.id if isinstance(node, ast.Name) else node.attr
                assert label not in {"DecisionSigner", "open_operator_signer", "issue"}, f"{name}: {label}"


def test_cr12_decisions_module_cannot_sign() -> None:
    tree = _tree("decisions.py")
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.ClassDef)}
    assert "issue" not in defined and "DecisionSigner" not in defined
    assert not hasattr(DecisionVerifier, "issue")
    assert "_environment" not in getattr(decisions, "__all__", ())


def test_cr12_jobs_and_registry_refuse_a_signing_capable_object(tmp_path: Path) -> None:
    signer, verifier = make_pair(tmp_path)
    with pytest.raises(TypeError, match="DECISION_VERIFIER_REQUIRED"):
        require_verifier(signer)

    class SigningVerifier(DecisionVerifier):
        def issue(self) -> None:  # a verifier that grew a signing method
            return None

    sneaky = SigningVerifier.__new__(SigningVerifier)
    for bad in (signer, sneaky):
        with pytest.raises(TypeError, match="DECISION_VERIFIER_REQUIRED"):
            TrainingJobs(tmp_path / "jobs.jsonl", verifier=bad)  # type: ignore[arg-type]
    env = make_env(tmp_path / "env")
    with pytest.raises(TypeError, match="DECISION_VERIFIER_REQUIRED"):
        ModelRegistry(tmp_path / "registry.jsonl", jobs=env.jobs, verifier=signer,  # type: ignore[arg-type]
                      backends=default_backends(), store=env.store)
    assert isinstance(require_verifier(verifier), DecisionVerifier)


def test_cr12_no_environment_override_in_production_signatures() -> None:
    for target in (DecisionVerifier.__init__, DecisionSigner.__init__, DecisionSigner.issue, open_operator_signer,
                   TrainingJobs.__init__, ModelRegistry.__init__):
        assert "environ" not in inspect.signature(target).parameters, target
    for path in PACKAGE.glob("*.py"):
        assert "environ=" not in path.read_text(encoding="utf-8"), path.name


def test_cr12_ambient_automation_refuses_signing_but_not_retained_verification(
        tmp_path: Path, hermetic_decision_environment: dict[str, str]) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    env.gate.decide(candidate, record)
    hermetic_decision_environment["GITHUB_ACTIONS"] = "true"
    with pytest.raises(ValueError, match="AUTOMATION_CANNOT_DECIDE"):
        env.signer.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=env.subject(candidate),
                         issued_at=START, lifetime=timedelta(hours=1))
    with pytest.raises(ValueError, match="AUTOMATION_CANNOT_DECIDE"):
        open_operator_signer(approver=APPROVER, boundary=None,  # type: ignore[arg-type]
                             registry=None)  # type: ignore[arg-type]
    assert env.gate.verified_state(candidate).state.value == "PROMOTED"  # reading past human decisions still works
