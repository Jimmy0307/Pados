"""Registry candidates, provenance and the human PromotionGate."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    LifecycleState,
    Modality,
    VerificationMethod,
)
from pados.capability.policy import PolicyCode, PolicyContext, authorize_scope, evaluate
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.training.decisions import DecisionOperation
from pados.training.evaluation import compare
from pados.training.registry import (
    REGISTRY_FIELDS,
    CandidateState,
    ModelDescriptorDraft,
    ModelRegistryCandidate,
    Provenance,
    RegistryError,
)

from .support import COMMIT, make_env, make_pair, reanchor

HOUR = timedelta(hours=1)


def test_provenance_is_complete_and_derived_from_contract_and_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    provenance = candidate.provenance
    assert provenance.dataset_hash == env.train.content_hash
    assert provenance.dataset_snapshot_hash == env.train.snapshot_hash
    assert provenance.config_hash == contract.config_hash
    assert provenance.contract_hash == contract.contract_hash
    assert provenance.base_model is None and provenance.kind.value == "ALGORITHM_FIT"
    assert provenance.backend_adapter_id == "training.algorithm-fit"
    assert provenance.backend_license_spdx == "AGPL-3.0-only"
    assert provenance.code_version == COMMIT and provenance.seed == 7
    assert provenance.compute_resource_ref == "resource.local-cpu"
    assert provenance.output_license_spdx == "Apache-2.0" and provenance.dataset_license_spdx == "CC-BY-4.0"
    data = provenance.model_dump(mode="json")
    for field in Provenance.model_fields:
        if field == "base_model":
            continue
        incomplete = {k: v for k, v in data.items() if k != field}
        with pytest.raises(ValidationError):
            Provenance.model_validate(incomplete)
    with pytest.raises(ValidationError, match="PROVENANCE_INCOMPLETE"):
        Provenance.model_validate(dict(data, code_version="HEAD"))


def test_registration_rejects_provenance_that_does_not_match(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    forged = candidate.model_copy(update={"provenance": candidate.provenance.model_copy(update={"seed": 8})})
    with pytest.raises(RegistryError, match="PROVENANCE_MISMATCH"):
        env.register(forged, contract)
    inflated = candidate.model_copy(update={"metrics": (("train_r2", 1.0),)})
    with pytest.raises(RegistryError, match="CANDIDATE_METRICS_NOT_FROM_JOB"):
        env.register(inflated, contract)
    other_artifact = candidate.model_copy(update={"artifact": env.train.members[0], "evaluations": ()})
    with pytest.raises(RegistryError, match="JOB_NOT_COMPLETED_FOR_ARTIFACT"):
        env.register(other_artifact, contract)


def test_job_cannot_self_promote(tmp_path: Path, hermetic_decision_environment: dict[str, str]) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    # Automation identities cannot be registered approvers, nor sign, nor pass the check.
    with pytest.raises(ValueError, match="OPERATOR_REGISTRY_PRINCIPAL_INVALID"):
        make_pair(tmp_path, operators=("training-job",))
    for principal in ("training.job-00000001", "pados-trainer", "github-actions[bot]"):
        with pytest.raises(ValueError):
            env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate), approver=principal)
    hermetic_decision_environment["GITHUB_ACTIONS"] = "true"
    with pytest.raises(ValueError, match="AUTOMATION_CANNOT_DECIDE"):
        env.signer.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=env.subject(candidate),
                         issued_at=env.clock.current, lifetime=HOUR)
    hermetic_decision_environment.clear()
    # A run approval for the job's contract is not a promotion decision.
    run_approval = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, candidate.provenance.contract_hash)
    with pytest.raises(RegistryError, match="PROMOTION_DECISION_OPERATION_INVALID"):
        env.gate.decide(candidate, run_approval)
    assert env.registry.state(candidate.candidate_hash) is CandidateState.PROMOTION_REQUESTED


def test_better_metrics_without_approval_stay_candidate(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    _, _, ridge = env.trained(env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 5000.0}),
                              job_id="job-00000002")
    comparison = compare(candidate.evaluations[0], env.report(ridge, report_id="baseline"), metric="r2")
    assert comparison.improved and comparison.authorizes is False
    assert env.registry.state(candidate.candidate_hash) is CandidateState.CANDIDATE
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    assert env.gate.verified_state(candidate).state is CandidateState.PROMOTION_REQUESTED
    with pytest.raises(RegistryError, match="CANDIDATE_NOT_PROMOTED"):
        env.gate.descriptor_draft(candidate, adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT", version="1")


def test_promotion_requires_evaluation_and_a_request(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered(evaluate=False)
    with pytest.raises(RegistryError, match="PROMOTION_REQUIRES_EVALUATION"):
        env.registry.request_promotion(candidate, requested_by="bob.engineer")
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    with pytest.raises(RegistryError, match="CANDIDATE_TRANSITION_INVALID"):
        env.gate.decide(candidate, record)
    # The refused move burned nothing: the decision is still unconsumed.
    assert env.verifier.ledger.consumed(record["decision_id"]) is None


def test_promotion_with_approval_bound_to_exact_candidate_hash(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    other_hash = canonical_hash({"other": "candidate"})
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH"):
        env.gate.decide(candidate, env.decision(DecisionOperation.PROMOTE_MODEL, other_hash))
    altered = candidate.model_copy(update={"candidate_id": "cand-renamed"})
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH|CANDIDATE_NOT_REGISTERED"):
        env.gate.decide(altered, record)
    assert env.gate.decide(candidate, record) is CandidateState.PROMOTED
    assert env.gate.verified_state(candidate).state is CandidateState.PROMOTED
    lines = (tmp_path / "registry.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert set(last) == set(REGISTRY_FIELDS)
    assert last["decision_id"] == record["decision_id"] and last["actor"] == "alice.operator"


def test_forged_and_replayed_promotion_approvals_are_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first, _ = env.registered()
    env.registry.request_promotion(first, requested_by="bob.engineer")
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(first))
    forged = dict(record, signature="0" * 64)
    with pytest.raises(RegistryError, match="DECISION_SIGNATURE_INVALID"):
        env.gate.decide(first, forged)
    edited = dict(record, approver="carol.operator")  # registered, but alice's MAC is not carol's
    with pytest.raises(RegistryError, match="DECISION_SIGNATURE_INVALID"):
        env.gate.decide(first, edited)
    with pytest.raises(RegistryError, match="APPROVER_NOT_REGISTERED"):
        env.gate.decide(first, dict(record, approver="mallory.operator"))
    foreign, _ = make_pair(tmp_path, ledger_name="foreign.jsonl")  # a different key
    foreign_record = foreign.issue(operation=DecisionOperation.PROMOTE_MODEL, subject_hash=env.subject(first),
                                   issued_at=env.clock.current, lifetime=HOUR)
    with pytest.raises(RegistryError, match="DECISION_SIGNATURE_INVALID"):
        env.gate.decide(first, foreign_record)
    unsigned = {k: v for k, v in record.items() if k != "signature"}
    with pytest.raises(RegistryError, match="DECISION_MALFORMED"):
        env.gate.decide(first, unsigned)
    assert env.gate.decide(first, record) is CandidateState.PROMOTED
    second, _ = env.registered(job_id="job-00000002",
                               contract=env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 1.0}))
    env.registry.request_promotion(second, requested_by="bob.engineer")
    with pytest.raises(RegistryError, match="DECISION_ALREADY_USED"):
        env.gate.decide(second, record)  # replay onto another candidate
    before = (tmp_path / "registry.jsonl").read_text(encoding="utf-8")
    assert env.gate.decide(first, record) is CandidateState.PROMOTED  # replay onto the same candidate: no-op
    assert (tmp_path / "registry.jsonl").read_text(encoding="utf-8") == before
    rebound = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(second),
                           decision_id=record["decision_id"])  # same id, new signature
    with pytest.raises(RegistryError, match="DECISION_ALREADY_USED"):
        env.gate.decide(second, rebound)
    assert env.registry.state(second.candidate_hash) is CandidateState.PROMOTION_REQUESTED


def test_rejection_is_also_a_signed_decision(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    promote_record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    assert env.gate.decide(candidate, env.decision(DecisionOperation.REJECT_MODEL, env.subject(candidate))) \
        is CandidateState.REJECTED
    with pytest.raises(RegistryError, match="CANDIDATE_TRANSITION_INVALID"):
        env.gate.decide(candidate, promote_record)


def test_forged_registry_ledger_promotion_is_not_trusted(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    unsigned = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    fabricated = dict(unsigned, signature="d" * 64)  # attacker without the key
    path = tmp_path / "registry.jsonl"
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    forged = dict(entries[-1], seq=len(entries) + 1, event="PROMOTED", from_state="PROMOTION_REQUESTED",
                  to_state="PROMOTED", decision_id=fabricated["decision_id"], decision=fabricated,
                  actor="alice.operator", prev_hash=entries[-1]["entry_hash"])
    forged.pop("entry_hash")
    forged["entry_hash"] = canonical_hash(forged)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(RegistryError, match="LEDGER_HEAD_NOT_ANCHORED"):  # a suffix nobody anchored
        env.gate.verified_state(candidate)
    reanchor(path)  # an attacker who also rewrites the anchor store
    assert env.registry.state(candidate.candidate_hash) is CandidateState.PROMOTED  # raw ledger says so...
    assert env.gate.verified_state(candidate).state is CandidateState.UNVERIFIED  # ...but the MAC does not verify
    with pytest.raises(RegistryError, match="DECISION_UNVERIFIED"):
        env.gate.descriptor_draft(candidate, adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT",
                                  version="1")


def test_promoted_candidate_yields_only_an_unregistered_unauthorized_draft(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    env.gate.decide(candidate, env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate)))
    draft = env.gate.descriptor_draft(candidate, adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT",
                                      version="1")
    assert isinstance(draft.proposed, AdapterDescriptor)
    assert draft.registration_state == "UNREGISTERED" and draft.authorization_state == "UNAUTHORIZED"
    assert draft.required_grant == "CAPABILITY:TABULAR_PREDICT"
    assert draft.proposed.lifecycle is LifecycleState.EXPERIMENTAL
    assert draft.proposed.network_requirement is NetworkRequirement.LOCAL_ONLY
    assert draft.proposed.max_classification is env.train.classification
    with pytest.raises(ValueError, match="DESCRIPTOR_DRAFT_CANNOT_CARRY_AUTHORITY"):
        ModelDescriptorDraft(candidate_hash=draft.candidate_hash, artifact_sha256=draft.artifact_sha256,
                             promotion_decision_id=draft.promotion_decision_id, proposed=draft.proposed,
                             required_grant=draft.required_grant, authorization_state="AUTHORIZED")
    # Promotion does not authorize: a project grant without CAPABILITY:TABULAR_PREDICT still refuses it.
    capability = CapabilityDescriptor(capability_id="TABULAR_PREDICT", version="1",
                                      family=CapabilityFamily.ALGORITHM, operation="PREDICT",
                                      input_contract="tabular.features/v1", output_contract="tabular.prediction/v1",
                                      determinism=Determinism.DETERMINISTIC, modalities=(Modality.TABLE,),
                                      verification=(VerificationMethod.DETERMINISTIC_REPLAY,))
    contract_doc = {"work_contract_id": "wc-1", "project_id": "p", "required_capabilities": ["CAPABILITY:OTHER"]}
    grant = {"work_contract_id": "wc-1", "project_id": "p", "capabilities": ["CAPABILITY:OTHER"]}
    scope = authorize_scope(contract_doc, grant)
    context = PolicyContext(scope=scope, project_id="p", stage_id="s1", classification=DataClassification.INTERNAL,
                            network_policy=NetworkRequirement.LOCAL_ONLY)
    decision = evaluate(capability, draft.proposed, context, adapter_available=lambda: True)
    assert decision.code is PolicyCode.NOT_AUTHORIZED


def test_candidate_cannot_carry_authority_or_foreign_evaluations(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.candidate()
    data = candidate.model_dump(mode="json")
    with pytest.raises(ValidationError, match="CANDIDATE_CANNOT_CARRY_AUTHORITY"):
        ModelRegistryCandidate.model_validate(dict(data, semantic_authority="PROMOTED"))
    other = dict(data, artifact=dict(data["artifact"], sha256="b" * 64))
    with pytest.raises(ValidationError, match="EVALUATION_NOT_FOR_CANDIDATE"):
        ModelRegistryCandidate.model_validate(other)
    with pytest.raises(RegistryError, match="CANDIDATE_ALREADY_REGISTERED"):
        for _ in range(2):
            env.register(candidate, env.contract())
