"""CR-12 TP-01: authority-bearing entries retain the signed decision and readers re-verify it.

The forgeries here also rewrite the anchor store (``anchor_too``): the signature layer holds on its own."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.training.decisions import RECORD_FIELDS, DecisionOperation
from pados.training.jobs import JobState, TrainingJobError
from pados.training.registry import CandidateState, RegistryError

from .support import make_env, rechain

DRAFT_ARGS = dict(adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT", version="1")


def _promoted(env: Any) -> tuple[Any, dict[str, Any]]:
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    assert env.gate.decide(candidate, record) is CandidateState.PROMOTED
    return candidate, record


def test_cr12_entries_retain_full_signed_record_and_never_key_material(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, record = _promoted(env)
    job = env.jobs.get(candidate.provenance.job_id)
    assert job.approval_decision is not None and set(job.approval_decision) == set(RECORD_FIELDS)
    decided = env.registry.record(candidate.candidate_hash)
    assert decided.decision == record and set(record) == set(RECORD_FIELDS)
    for name in ("jobs.jsonl", "registry.jsonl", "decisions.jsonl"):
        assert env.key not in (tmp_path / name).read_text(encoding="utf-8")
    assert env.gate.verified_state(candidate).state is CandidateState.PROMOTED


def test_cr12_rewriting_both_ledgers_without_the_key_is_unverified(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    fabricated = dict(env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate)), signature="d" * 64)

    def forge_consumption(entries: list[dict[str, Any]]) -> None:
        entries.append({"decision_id": fabricated["decision_id"], "operation": "PROMOTE_MODEL",
                        "subject_hash": candidate.candidate_hash, "approver": "alice.operator",
                        "consumed_at": env.clock.current.isoformat(), "decision": fabricated})

    def forge_promotion(entries: list[dict[str, Any]]) -> None:
        entries.append(dict(entries[-1], event="PROMOTED", from_state="PROMOTION_REQUESTED", to_state="PROMOTED",
                            decision_id=fabricated["decision_id"], decision=fabricated, actor="alice.operator"))

    rechain(tmp_path / "decisions.jsonl", forge_consumption, anchor_too=True)
    rechain(tmp_path / "registry.jsonl", forge_promotion, anchor_too=True)
    assert env.registry.state(candidate.candidate_hash) is CandidateState.PROMOTED
    assert env.gate.verified_state(candidate).state is CandidateState.UNVERIFIED
    with pytest.raises(RegistryError, match="DECISION_UNVERIFIED"):
        env.gate.descriptor_draft(candidate, **DRAFT_ARGS)


def test_cr12_genuine_decision_transplanted_to_another_candidate_is_unverified(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first, record = _promoted(env)
    second, _ = env.registered(job_id="job-00000002",
                               contract=env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 1.0}))
    env.registry.request_promotion(second, requested_by="bob.engineer")

    def transplant(entries: list[dict[str, Any]]) -> None:
        last = next(e for e in reversed(entries) if e["candidate_hash"] == second.candidate_hash)
        entries.append(dict(last, event="PROMOTED", from_state="PROMOTION_REQUESTED", to_state="PROMOTED",
                            decision_id=record["decision_id"], decision=record, actor="alice.operator"))

    rechain(tmp_path / "registry.jsonl", transplant, anchor_too=True)
    assert env.gate.verified_state(second).state is CandidateState.UNVERIFIED
    assert env.gate.verified_state(first).state is CandidateState.PROMOTED


def test_cr12_transition_time_outside_signed_window_is_unverified(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, record = _promoted(env)
    late = (env.clock.current + timedelta(days=2)).isoformat()

    def move_time(entries: list[dict[str, Any]]) -> None:
        entries[-1]["at"] = late

    rechain(tmp_path / "registry.jsonl", move_time, anchor_too=True)
    assert env.gate.verified_state(candidate).state is CandidateState.UNVERIFIED


def test_cr12_deleted_consumption_makes_decided_state_unverified_and_replay_harmless(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, record = _promoted(env)
    rechain(tmp_path / "decisions.jsonl", lambda entries: entries.clear(), anchor_too=True)
    assert env.gate.verified_state(candidate).state is CandidateState.UNVERIFIED  # fail closed
    before = (tmp_path / "registry.jsonl").read_text(encoding="utf-8")
    assert env.gate.decide(candidate, record) is CandidateState.PROMOTED  # same decision, same subject: no-op
    assert (tmp_path / "registry.jsonl").read_text(encoding="utf-8") == before
    second, _ = env.registered(job_id="job-00000002",
                               contract=env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 1.0}))
    env.registry.request_promotion(second, requested_by="bob.engineer")
    with pytest.raises(RegistryError, match="DECISION_ALREADY_USED"):
        env.gate.decide(second, record)


def test_cr12_run_approval_replay_after_ledger_deletion_is_harmless(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.jobs.create(contract, job_id="job-00000001")
    env.jobs.create(contract, job_id="job-00000002")
    record = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, env.run_subject("job-00000001"))
    env.jobs.approve("job-00000001", record)
    rechain(tmp_path / "decisions.jsonl", lambda entries: entries.clear(), anchor_too=True)
    before = (tmp_path / "jobs.jsonl").read_text(encoding="utf-8")
    assert env.jobs.approve("job-00000001", record).state is JobState.APPROVED_TO_RUN  # idempotent
    assert (tmp_path / "jobs.jsonl").read_text(encoding="utf-8") == before
    with pytest.raises(TrainingJobError, match="DECISION_ALREADY_USED"):
        env.jobs.approve("job-00000002", record)  # same contract hash, another job
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_UNVERIFIED"):
        env.jobs.start("job-00000001", approval_id=record["decision_id"])  # consumption evidence is gone


def test_cr12_tampered_retained_run_approval_blocks_registration(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()

    def tamper(entries: list[dict[str, Any]]) -> None:
        approved = next(e for e in entries if e["event"] == "APPROVED")
        approved["decision"] = dict(approved["decision"], approver="mallory.operator")

    rechain(tmp_path / "jobs.jsonl", tamper, anchor_too=True)
    assert not env.jobs.approval_verified(env.jobs.get(candidate.provenance.job_id))
    with pytest.raises(RegistryError, match="JOB_RUN_APPROVAL_UNVERIFIED"):
        env.register(candidate, contract)


def test_cr12_decision_ids_are_single_use_across_job_and_registry_logs(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    run_id = env.jobs.get(candidate.provenance.job_id).approval_id
    assert run_id is not None
    reused = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate), decision_id=run_id)
    with pytest.raises(RegistryError, match="DECISION_ALREADY_USED"):
        env.gate.decide(candidate, reused)  # id already named by the job log
    record = env.decision(DecisionOperation.PROMOTE_MODEL, env.subject(candidate))
    env.gate.decide(candidate, record)
    other = env.contract(seed=99)
    env.jobs.create(other, job_id="job-00000009")
    approval = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, env.run_subject("job-00000009"),
                            decision_id=record["decision_id"])
    with pytest.raises(TrainingJobError, match="DECISION_ALREADY_USED"):
        env.jobs.approve("job-00000009", approval)  # id already named by the registry log
