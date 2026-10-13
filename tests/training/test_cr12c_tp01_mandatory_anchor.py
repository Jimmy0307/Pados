"""CR-12c TP-01: anchoring is mandatory for authority, and every authority-bearing read requires all three logs
(jobs, registry, consumed-decision ledger) to sit exactly at their anchored heads."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from pados.training._chain import Anchoring, FileExternalAnchor
from pados.training.backends import default_backends
from pados.training.decisions import ConsumedDecisionLedger, DecisionOperation, DecisionVerifier
from pados.training.jobs import JobState, TrainingJobError, TrainingJobs
from pados.training.contracts import TrainingContract
from pados.training.registry import (
    CandidateState,
    ModelRegistry,
    ModelRegistryCandidate,
    PromotionGate,
    RegistryError,
)

from .support import LOG_NAMES, Env, anchor_dir, make_env, restore_logs, snapshot_logs

DRAFT_ARGS = dict(adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT", version="1")
RUN = DecisionOperation.APPROVE_TRAINING_RUN
PROMOTE = DecisionOperation.PROMOTE_MODEL
REJECT = DecisionOperation.REJECT_MODEL


def _awaiting(env: Env, job_id: str = "job-00000001") -> tuple[ModelRegistryCandidate, TrainingContract]:
    candidate, contract = env.registered(job_id=job_id)
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    return candidate, contract


def _open(tmp: Path, env: Env, anchor: FileExternalAnchor | None) -> tuple[TrainingJobs, PromotionGate]:
    """The same three logs opened by another process with the same operator keys (and optionally an anchor)."""
    verifier = DecisionVerifier(boundary=env.keys.boundary, registry=env.keys.registry,
                                ledger=ConsumedDecisionLedger(tmp / "decisions.jsonl", anchor=anchor))
    jobs = TrainingJobs(tmp / "jobs.jsonl", verifier=verifier, clock=env.clock, registry_log=tmp / "registry.jsonl",
                        anchor=anchor)
    registry = ModelRegistry(tmp / "registry.jsonl", jobs=jobs, verifier=verifier, backends=default_backends(),
                             store=env.store, clock=env.clock, anchor=anchor)
    return jobs, PromotionGate(registry)


# --- transitions are refused without an anchor --------------------------------------------------------------


def test_run_approval_and_start_are_refused_without_an_anchor(tmp_path: Path) -> None:
    env = make_env(tmp_path, anchored=False)
    assert env.jobs.anchoring is Anchoring.UNANCHORED
    env.jobs.create(env.contract(), job_id="job-00000001")
    record = env.decision(RUN, env.run_subject("job-00000001"))
    with pytest.raises(TrainingJobError, match="ANCHOR_REQUIRED"):
        env.jobs.approve("job-00000001", record)
    assert env.verifier.ledger.consumed(record["decision_id"]) is None  # nothing consumed, nothing recorded
    assert env.jobs.get("job-00000001").state is JobState.DRAFT  # read-only reporting still works
    with pytest.raises(TrainingJobError, match="ANCHOR_REQUIRED"):
        env.jobs.start("job-00000001", approval_id=record["decision_id"])


def test_partially_anchored_logs_refuse_every_authority_bearing_transition(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    plain_ledger = DecisionVerifier(boundary=env.keys.boundary, registry=env.keys.registry,
                                    ledger=ConsumedDecisionLedger(tmp_path / "plain-decisions.jsonl"))
    jobs_without_anchored_ledger = TrainingJobs(tmp_path / "jobs.jsonl", verifier=plain_ledger, clock=env.clock,
                                                registry_log=tmp_path / "registry.jsonl", anchor=env.anchor)
    jobs_without_registry_log = TrainingJobs(tmp_path / "jobs.jsonl", verifier=env.verifier, clock=env.clock,
                                             anchor=env.anchor)
    env.jobs.create(env.contract(seed=21), job_id="job-00000021")
    run = env.decision(RUN, env.run_subject("job-00000021"))
    for jobs in (jobs_without_anchored_ledger, jobs_without_registry_log):
        assert jobs.anchoring is Anchoring.UNANCHORED
        with pytest.raises(TrainingJobError, match="ANCHOR_REQUIRED"):
            jobs.approve("job-00000021", run)

    candidate, contract = env.candidate()
    unanchored_registry = ModelRegistry(tmp_path / "registry-plain.jsonl", jobs=env.jobs, verifier=env.verifier,
                                        backends=default_backends(), store=env.store, clock=env.clock)
    assert unanchored_registry.anchoring is Anchoring.UNANCHORED
    with pytest.raises(RegistryError, match="ANCHOR_REQUIRED"):
        unanchored_registry.register(candidate, contract=contract, dataset=env.train, eval_dataset=env.held_out,
                                     registered_by="bob.engineer")
    env.register(candidate, contract)
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    _, gate = _open(tmp_path, env, None)  # the same, decidable entry seen through unanchored logs
    for operation in (PROMOTE, REJECT):
        record = env.decision(operation, env.subject(candidate))
        with pytest.raises(RegistryError, match="ANCHOR_REQUIRED"):
            gate.decide(candidate, record)
        assert env.verifier.ledger.consumed(record["decision_id"]) is None
    assert env.gate.verified_state(candidate).state is CandidateState.PROMOTION_REQUESTED


def test_unanchored_reads_report_but_never_verify_a_decided_entry(tmp_path: Path) -> None:
    env = make_env(tmp_path / "store")
    candidate, _ = _awaiting(env)
    env.gate.decide(candidate, env.decision(PROMOTE, env.subject(candidate)))
    assert env.gate.verified_state(candidate).state is CandidateState.PROMOTED
    copy = tmp_path / "copy"
    copy.mkdir()
    for name in LOG_NAMES:
        shutil.copy2(tmp_path / "store" / name, copy / name)
    jobs, gate = _open(copy, env, None)
    verdict = gate.verified_state(candidate)  # read-only reporting over unanchored logs
    assert verdict.state is CandidateState.UNVERIFIED and verdict.anchoring is Anchoring.UNANCHORED
    with pytest.raises(RegistryError, match="ANCHOR_REQUIRED"):
        gate.descriptor_draft(candidate, **DRAFT_ARGS)
    assert not jobs.approval_verified(jobs.get(candidate.provenance.job_id))


# --- a coordinated rollback with the anchor directory left intact is always detected -----------------------


def test_coordinated_rollback_of_all_logs_with_the_anchor_intact_is_detected(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = _awaiting(env)
    env.jobs.create(env.contract(seed=22), job_id="job-00000022")
    before = snapshot_logs(tmp_path)
    record = env.decision(PROMOTE, env.subject(candidate))
    env.gate.decide(candidate, record)
    run = env.decision(RUN, env.run_subject("job-00000022"))
    env.jobs.approve("job-00000022", run)

    restore_logs(tmp_path, before)  # all three logs, consistently, to before both decisions
    for read in (lambda: env.gate.verified_state(candidate), lambda: env.gate.descriptor_draft(candidate, **DRAFT_ARGS),
                 lambda: env.gate.decide(candidate, record)):
        with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
            read()
    for action in (lambda: env.jobs.approve("job-00000022", run), lambda: env.jobs.all(),
                   lambda: env.jobs.verify_heads()):
        with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
            action()


@pytest.mark.parametrize("rolled_back", LOG_NAMES)
def test_rollback_of_any_single_log_fails_every_authority_bearing_read(tmp_path: Path, rolled_back: str) -> None:
    env = make_env(tmp_path)
    candidate, _ = _awaiting(env)
    env.jobs.create(env.contract(seed=23), job_id="job-00000023")
    saved = (tmp_path / rolled_back).read_bytes()
    env.gate.decide(candidate, env.decision(PROMOTE, env.subject(candidate)))
    env.jobs.approve("job-00000023", env.decision(RUN, env.run_subject("job-00000023")))
    (tmp_path / rolled_back).write_bytes(saved)
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.verified_state(candidate)  # reads the registry, the job log AND the ledger
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.descriptor_draft(candidate, **DRAFT_ARGS)
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
        env.jobs.verify_heads()  # what approving, starting and registering re-check


# --- a restored, already-consumed historical promotion is refused -----------------------------------------


def test_restored_consumed_historical_promotion_is_refused(tmp_path: Path) -> None:
    store = tmp_path / "store"
    env = make_env(store)
    first, _ = _awaiting(env)
    env.gate.decide(first, env.decision(PROMOTE, env.subject(first)))
    historical = snapshot_logs(store)  # FIRST promoted, its decision consumed
    second, _ = _awaiting(env, job_id="job-00000002")
    env.gate.decide(second, env.decision(REJECT, env.subject(second)))  # history moves on; the anchor follows

    restore_logs(store, historical)  # the consumed historical promotion is restored over the live store
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.verified_state(first)
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.descriptor_draft(first, **DRAFT_ARGS)

    elsewhere = tmp_path / "elsewhere"  # ...or replanted into a fresh store
    elsewhere.mkdir()
    restore_logs(elsewhere, historical)
    _, unanchored = _open(elsewhere, env, None)
    verdict = unanchored.verified_state(first)
    assert verdict.state is CandidateState.UNVERIFIED and verdict.anchoring is Anchoring.UNANCHORED
    with pytest.raises(RegistryError, match="ANCHOR_REQUIRED"):
        unanchored.descriptor_draft(first, **DRAFT_ARGS)
    fresh = FileExternalAnchor(anchor_dir(elsewhere))  # an anchor configured only after the fact
    fresh.directory.mkdir()
    _, late = _open(elsewhere, env, fresh)
    with pytest.raises(RegistryError, match="LEDGER_HEAD_NOT_ANCHORED"):
        late.verified_state(first)


def test_anchored_promotion_still_verifies_and_reports_anchored(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, _ = _awaiting(env)
    env.gate.decide(candidate, env.decision(PROMOTE, env.subject(candidate)))
    verdict = env.gate.verified_state(candidate)
    assert verdict.state is CandidateState.PROMOTED and verdict.anchoring is Anchoring.ANCHORED
    draft = env.gate.descriptor_draft(candidate, **DRAFT_ARGS)
    assert draft.anchoring is Anchoring.ANCHORED
    assert (draft.registration_state, draft.authorization_state) == ("UNREGISTERED", "UNAUTHORIZED")


def test_approval_verified_does_not_trust_a_caller_built_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    job_id, _, _ = env.trained()
    job = env.jobs.get(job_id)
    assert env.jobs.approval_verified(job)
    assert not env.jobs.approval_verified(replace(job, approved_at=job.created_at))  # not what the log says

