"""Governed Outcome Learning Adapter: external problem-solving evidence stays advisory and authenticated."""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pados.connectivity.state import ConnectivityState
from pados.learning import external_outcomes as module
from pados.learning.external_outcomes import (
    CandidateKind,
    ExternalFeedback,
    ExternalOutcomeObservation,
    LearningCandidate,
    TrainingMethod,
    TrainingProposal,
    outcome_from_external,
)
from pados.learning.records import (
    HumanDecision,
    LearningRecordError,
    authenticate_outcome,
    capability_ref_for,
    outcome_id_for,
)
from tests.learning.learning_support import NOW, SCANNED_ZH, TrustedReceipts, plan_receipt, sha, stage_receipt


def trusted(*, decision: HumanDecision | None = None, features: dict[str, str] | None = SCANNED_ZH,
            ) -> tuple[TrustedReceipts, str]:
    auth = TrustedReceipts()
    ref = None if decision is None else "decision-ext-1"
    plan = auth.register(plan_receipt(stage_receipt("s1", plan_id="plan-ext"), plan_id="plan-ext",
                                      human_decision_ref=ref), features=features)
    if ref is not None and decision is not None:
        auth.decide(ref, decision)
    return auth, plan.stages[0].receipt_hash


def observation(receipt_hash: str, **overrides: Any) -> ExternalOutcomeObservation:
    values: dict[str, Any] = dict(source_label="problem-solving-1", stage_receipt_hash=receipt_hash,
                                  external_case_digest=sha("case-1"), evidence_digests=(sha("ev-1"),),
                                  feedback=ExternalFeedback.RESOLVED_EFFECTIVE,
                                  network_state=ConnectivityState.OFFLINE, quality_metric=0.9, observed_at=NOW)
    values.update(overrides)
    return ExternalOutcomeObservation(**values)


def test_authenticated_observation_becomes_an_outcome_through_the_existing_path() -> None:
    auth, receipt = trusted()
    record = outcome_from_external(observation(receipt), authenticator=auth, recorded_at=NOW)
    assert record.outcome_id == outcome_id_for(receipt) and record.quality_metric == 0.9
    assert authenticate_outcome(record, auth)
    assert record.features_authenticated and record.context_features == tuple(sorted(SCANNED_ZH.items()))


def test_external_feedback_never_becomes_a_human_decision() -> None:
    auth, receipt = trusted()  # no governed decision recorded
    for feedback in ExternalFeedback:
        record = outcome_from_external(observation(receipt, feedback=feedback), authenticator=auth, recorded_at=NOW)
        assert record.human_decision is HumanDecision.NONE and record.human_decision_ref is None
    for field in ("human_decision", "human_decision_ref", "human_confirmed", "verification", "success"):
        with pytest.raises(ValidationError):
            observation(receipt, **{field: "ACCEPTED"})


@pytest.mark.parametrize("verdict", [HumanDecision.ACCEPTED, HumanDecision.REJECTED])
def test_decision_comes_only_from_the_governed_approval_path(verdict: HumanDecision) -> None:
    auth, receipt = trusted(decision=verdict)
    effective = observation(receipt, feedback=ExternalFeedback.RESOLVED_EFFECTIVE)
    record = outcome_from_external(effective, authenticator=auth, recorded_at=NOW)
    assert record.human_decision is verdict  # the external process saying "effective" changes nothing


def test_unauthenticated_observations_are_refused() -> None:
    auth, receipt = trusted()
    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        outcome_from_external(observation(sha("unknown-receipt")), authenticator=auth, recorded_at=NOW)
    auth.revoke(receipt)
    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        outcome_from_external(observation(receipt), authenticator=auth, recorded_at=NOW)

    class Broken(TrustedReceipts):
        def resolve_receipt(self, stage_receipt_hash: str) -> Any:
            raise RuntimeError("resolver down")

    with pytest.raises(LearningRecordError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        outcome_from_external(observation(receipt), authenticator=Broken(), recorded_at=NOW)
    with pytest.raises(LearningRecordError, match="EXTERNAL_OUTCOME_INVALID"):
        outcome_from_external({"stage_receipt_hash": receipt}, authenticator=auth,  # type: ignore[arg-type]
                              recorded_at=NOW)


def test_conflicting_features_and_recording_before_execution_are_refused() -> None:
    auth, receipt = trusted()
    with pytest.raises(LearningRecordError, match="CONTEXT_FEATURES_CONFLICT_WITH_TRUSTED_PLAN"):
        outcome_from_external(observation(receipt, context_features={"scan_type": "PHOTO"}), authenticator=auth,
                              recorded_at=NOW)
    with pytest.raises(ValidationError, match="OUTCOME_RECORDED_BEFORE_EXECUTION"):
        outcome_from_external(observation(receipt), authenticator=auth, recorded_at=NOW - timedelta(hours=1))


@pytest.mark.parametrize("overrides", [
    {"external_case_digest": "case 42 at line 3"},
    {"evidence_digests": ("not-a-digest",)},
    {"evidence_digests": tuple(sha(str(i)) for i in range(17))},
    {"source_label": "a free text sentence"},
    {"context_features": {"customer": "x"}},
    {"context_features": {"scan_type": "a document sentence"}},
    {"feedback": "FIXED_BY_ALICE"},
    {"observed_at": NOW.replace(tzinfo=None)},
    {"quality_metric": float("nan")},
    {"case_text": "content never enters"},
])
def test_observations_are_content_free(overrides: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, LearningRecordError)):
        observation(sha("r"), **overrides)


# --- inert candidates and training proposals -------------------------------------------------------------------

def candidate(**overrides: Any) -> LearningCandidate:
    values: dict[str, Any] = dict(kind=CandidateKind.RULE, capability_ref=capability_ref_for("DOCUMENT_OCR"),
                                  proposal_digest=sha("rule-proposal"),
                                  supporting_outcome_ids=(outcome_id_for(sha("r1")),))
    values.update(overrides)
    return LearningCandidate(**values)


def proposal(**overrides: Any) -> TrainingProposal:
    values: dict[str, Any] = dict(method=TrainingMethod.LORA, base_manifest_digest="sha256:" + "a" * 64,
                                  dataset_snapshot_hash=sha("dataset"), evaluation_plan_digest=sha("eval-plan"),
                                  supporting_outcome_ids=(outcome_id_for(sha("r1")),))
    values.update(overrides)
    return TrainingProposal(**values)


def test_candidates_are_inert_and_carry_no_authority() -> None:
    item = candidate()
    assert item.authority == "NONE" and item.status == "CANDIDATE"
    assert item.requires_evaluation and item.requires_governed_promotion
    assert item.candidate_id.startswith("lc-") and item.candidate_id == candidate().candidate_id
    for field, value in (("authority", "OPERATOR"), ("status", "PROMOTED"), ("requires_evaluation", False),
                         ("requires_governed_promotion", False)):
        with pytest.raises(ValidationError):
            candidate(**{field: value})
    with pytest.raises(ValidationError):
        candidate(supporting_outcome_ids=())  # a candidate without outcome evidence is refused
    with pytest.raises(ValidationError):
        candidate(capability_ref="DOCUMENT_OCR")  # identities are digests only
    with pytest.raises((TypeError, ValidationError)):
        item.authority = "OPERATOR"  # type: ignore[assignment,misc]


def test_training_proposal_is_offline_gated_reversible_and_never_online() -> None:
    item = proposal()
    assert item.authority == "NONE" and item.requires_offline_evaluation and item.gated and item.reversible
    assert item.online is False and item.result_enters_as == "NEW_MEASURED_ARTIFACT"
    assert item.proposal_id.startswith("tp-") and item.to_record()["base_manifest_digest"].startswith("sha256:")
    for field, value in (("online", True), ("requires_offline_evaluation", False), ("gated", False),
                         ("reversible", False), ("authority", "SYSTEM"), ("result_enters_as", "OVERWRITE")):
        with pytest.raises(ValidationError):
            proposal(**{field: value})
    with pytest.raises(ValidationError):
        proposal(base_manifest_digest="model:latest")  # a tag is not an artifact identity


def test_training_proposal_has_no_execution_path() -> None:
    callables = {name for name in dir(TrainingProposal) if not name.startswith("_")
                 and callable(getattr(TrainingProposal, name)) and not hasattr(module.BaseModel, name)}
    validators = {"outcomes", "evaluations", "manifest", "digest", "minimized"}  # construction-time checks
    assert callables == validators | {"to_record"}  # serialization only: nothing starts, applies or writes
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    verbs = ("write", "apply", "promote", "deploy", "train", "execute", "submit", "register", "authorize", "commit")
    assert not [name for name in functions if name.startswith(verbs)], functions


def test_module_imports_no_authority_writer_runner_or_network() -> None:
    names: set[str] = set()
    for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = ("pados.capability.policy", "pados.capability.registry", "pados.capability.adapter",
                 "pados.execution.authorization", "pados.enterprise.grants", "pados.enterprise.approval",
                 "pados.state_proof", "pados.worker", "pados.compute_node", "pados.resources", "pados.training",
                 "pados.models", "pados.providers", "pados.db", "pados.memory", "pados.learning.advisor",
                 "pados.learning.store", "subprocess", "socket", "http", "urllib", "requests", "httpx")
    assert not [name for name in names if name.startswith(forbidden)], names
