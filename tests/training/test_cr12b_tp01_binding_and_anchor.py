"""CR-12b TP-01: decisions bind one exact log entry; with the (now mandatory) anchor, rollback is detected."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.training._chain import (
    GENESIS,
    AnchorHead,
    Anchoring,
    ChainedLogError,
    ExternalAnchor,
    FileExternalAnchor,
    HashChainedLog,
)
from pados.training.decisions import DecisionOperation
from pados.training.jobs import JobState, TrainingJobError, TrainingJobs, run_approval_subject
from pados.training.registry import CandidateState, ModelRegistryCandidate, RegistryError, build_candidate
from tests.windows_links import create_link_or_skip

from .support import CREATED, Env, anchor_dir, make_env, rechain, restore_logs, snapshot_logs

DRAFT_ARGS = dict(adapter_id="model.fit-v1", capability_id="TABULAR_PREDICT", version="1")
RUN = DecisionOperation.APPROVE_TRAINING_RUN
PROMOTE = DecisionOperation.PROMOTE_MODEL


def _twins(env: Env) -> tuple[ModelRegistryCandidate, ModelRegistryCandidate]:
    """Two registered candidates with identical artifacts (same job, same result), both awaiting a decision."""
    job_id, contract, result = env.trained()
    twins = tuple(build_candidate(candidate_id=name, job=env.jobs.get(job_id), result=result, contract=contract,
                                  dataset=env.train, backend=env.backend, store=env.store,
                                  eval_dataset=env.held_out, created_at=CREATED) for name in ("cand-a", "cand-b"))
    for candidate in twins:
        env.register(candidate, contract)
        env.registry.request_promotion(candidate, requested_by="bob.engineer")
    assert twins[0].artifact == twins[1].artifact
    return twins[0], twins[1]


# --- (a) one signed decision authorizes exactly one job / one registry entry -------------------------------


def test_run_approval_is_refused_for_another_job_with_the_same_contract(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    first, second = (env.jobs.create(contract, job_id=j) for j in ("job-00000001", "job-00000002"))
    assert first.contract_hash == second.contract_hash
    assert run_approval_subject(first) != run_approval_subject(second)
    record = env.decision(RUN, env.run_subject("job-00000001"))
    with pytest.raises(TrainingJobError, match="DECISION_SUBJECT_MISMATCH"):
        env.jobs.approve("job-00000002", record)  # presented first to the other job: refused, nothing burned
    assert env.verifier.ledger.consumed(record["decision_id"]) is None
    assert env.jobs.approve("job-00000001", record).state is JobState.APPROVED_TO_RUN
    assert env.jobs.get("job-00000002").state is JobState.DRAFT


def test_transplanted_run_approval_event_does_not_verify_for_another_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.jobs.create(contract, job_id="job-00000001")
    env.jobs.create(contract, job_id="job-00000002")
    record = env.decision(RUN, env.run_subject("job-00000001"))
    env.jobs.approve("job-00000001", record)

    def transplant(entries: list[dict[str, Any]]) -> None:  # attacker with write access to every log
        approved = next(e for e in entries if e["event"] == "APPROVED")
        entries.append(dict(approved, job_id="job-00000002"))

    rechain(tmp_path / "jobs.jsonl", transplant, anchor_too=True)  # logs AND anchor rewritten
    victim = env.jobs.get("job-00000002")
    assert victim.state is JobState.APPROVED_TO_RUN and victim.approval_decision == record  # raw log says so...
    assert not env.jobs.approval_verified(victim)  # ...but the signed subject names job-00000001 only
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_UNVERIFIED"):
        env.jobs.start("job-00000002", approval_id=record["decision_id"])
    assert env.jobs.approval_verified(env.jobs.get("job-00000001"))


def test_promotion_is_refused_for_another_candidate_with_identical_artifacts(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    first, second = _twins(env)
    assert env.subject(first) != env.subject(second)
    record = env.decision(PROMOTE, env.subject(first))
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH"):
        env.gate.decide(second, record)
    assert env.gate.decide(first, record) is CandidateState.PROMOTED
    reject = env.decision(DecisionOperation.REJECT_MODEL, env.subject(first))
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH"):
        env.gate.decide(second, reject)

    def transplant(entries: list[dict[str, Any]]) -> None:
        promoted = next(e for e in entries if e["event"] == "PROMOTED")
        last = next(e for e in reversed(entries) if e["candidate_hash"] == second.candidate_hash)
        entries.append(dict(promoted, candidate_hash=second.candidate_hash, candidate_id=last["candidate_id"],
                            provenance_hash=last["provenance_hash"]))

    rechain(tmp_path / "registry.jsonl", transplant, anchor_too=True)  # logs AND anchor rewritten
    assert env.registry.state(second.candidate_hash) is CandidateState.PROMOTED
    assert env.gate.verified_state(second).state is CandidateState.UNVERIFIED
    with pytest.raises(RegistryError, match="DECISION_UNVERIFIED"):
        env.gate.descriptor_draft(second, **DRAFT_ARGS)
    assert env.gate.verified_state(first).state is CandidateState.PROMOTED


# --- (b) anchor store compromised too: a rollback can only re-apply the identical transition in its window ---
# (with an intact anchor the same rollback fails closed: see test_cr12c_tp01_mandatory_anchor.py)


def test_compromised_anchor_rollback_replays_only_the_identical_run_approval_inside_its_window(
        tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    pristine = snapshot_logs(tmp_path)
    env.jobs.create(contract, job_id="job-00000001")
    env.jobs.create(contract, job_id="job-00000002")
    env.jobs.create(env.contract(seed=11), job_id="job-00000003")
    before = snapshot_logs(tmp_path)
    record = env.decision(RUN, env.run_subject("job-00000001"))
    env.jobs.approve("job-00000001", record)
    approved_line = (tmp_path / "jobs.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert env.jobs.anchoring is Anchoring.ANCHORED

    restore_logs(tmp_path, before, anchor_too=True)  # every log AND the anchor rolled back
    for other in ("job-00000002", "job-00000003"):  # same contract, or another contract: never
        with pytest.raises(TrainingJobError, match="DECISION_SUBJECT_MISMATCH"):
            env.jobs.approve(other, record)
    candidate_like = env.decision(PROMOTE, env.run_subject("job-00000001"))
    with pytest.raises(TrainingJobError, match="DECISION_OPERATION_MISMATCH"):
        env.jobs.approve("job-00000001", candidate_like)  # another operation is never a run approval
    replayed = env.jobs.approve("job-00000001", record)  # the residual: the identical approved transition
    assert replayed.approval_decision == record and replayed.state is JobState.APPROVED_TO_RUN
    replayed_line = json.loads((tmp_path / "jobs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    original = json.loads(approved_line)
    assert {k: v for k, v in replayed_line.items() if k not in {"at", "entry_hash"}} == \
        {k: v for k, v in original.items() if k not in {"at", "entry_hash"}}

    restore_logs(tmp_path, before, anchor_too=True)
    env.clock.current += timedelta(hours=2)  # past the signed window
    with pytest.raises(TrainingJobError, match="DECISION_EXPIRED"):
        env.jobs.approve("job-00000001", record)

    restore_logs(tmp_path, pristine, anchor_too=True)  # rolled back past creation; job re-created, same id
    env.clock.current -= timedelta(hours=2)
    env.jobs.create(contract, job_id="job-00000001")
    with pytest.raises(TrainingJobError, match="DECISION_SUBJECT_MISMATCH"):
        env.jobs.approve("job-00000001", record)  # a different CREATED event is a different job


def test_compromised_anchor_rollback_replays_only_the_identical_promotion_inside_its_window(
        tmp_path: Path) -> None:
    env = make_env(tmp_path)
    job_id, contract, result = env.trained()
    env.jobs.create(env.contract(seed=12), job_id="job-00000003")
    unregistered = snapshot_logs(tmp_path)
    twins = [build_candidate(candidate_id=name, job=env.jobs.get(job_id), result=result, contract=contract,
                             dataset=env.train, backend=env.backend, store=env.store, eval_dataset=env.held_out,
                             created_at=CREATED) for name in ("cand-a", "cand-b")]
    for candidate in twins:
        env.register(candidate, contract)
        env.registry.request_promotion(candidate, requested_by="bob.engineer")
    first, second = twins
    before = snapshot_logs(tmp_path)
    record = env.decision(PROMOTE, env.subject(first))
    assert env.gate.decide(first, record) is CandidateState.PROMOTED

    restore_logs(tmp_path, before, anchor_too=True)
    assert env.registry.state(first.candidate_hash) is CandidateState.PROMOTION_REQUESTED
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH"):
        env.gate.decide(second, record)  # identical artifact, other registry entry
    with pytest.raises(TrainingJobError, match="DECISION_OPERATION_MISMATCH"):
        env.jobs.approve("job-00000003", record)  # a promotion is never a run approval
    run_record = env.decision(RUN, env.subject(first))
    with pytest.raises(RegistryError, match="PROMOTION_DECISION_OPERATION_INVALID"):
        env.gate.decide(first, run_record)
    verdict_before = env.gate.verified_state(first)
    assert verdict_before.state is CandidateState.PROMOTION_REQUESTED
    assert env.gate.decide(first, record) is CandidateState.PROMOTED  # the residual: identical transition
    verdict = env.gate.verified_state(first)
    assert verdict.state is CandidateState.PROMOTED and verdict.anchoring is Anchoring.ANCHORED
    assert env.gate.verified_state(second).state is CandidateState.PROMOTION_REQUESTED
    assert env.gate.descriptor_draft(first, **DRAFT_ARGS).anchoring is Anchoring.ANCHORED

    restore_logs(tmp_path, before, anchor_too=True)
    env.clock.current += timedelta(hours=2)
    with pytest.raises(RegistryError, match="DECISION_EXPIRED"):
        env.gate.decide(first, record)

    restore_logs(tmp_path, unregistered, anchor_too=True)  # rolled back past registration; re-registered
    env.clock.current -= timedelta(hours=2)
    env.register(first, contract)
    env.registry.request_promotion(first, requested_by="bob.engineer")
    with pytest.raises(RegistryError, match="DECISION_SUBJECT_MISMATCH"):
        env.gate.decide(first, record)  # a different REGISTERED event is a different registry entry


# --- (b) anchored: every append records the head; rollback below it fails closed ---------------------------


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_every_append_records_the_head_and_verdicts_say_anchored(tmp_path: Path) -> None:
    env = make_env(tmp_path, anchored=True)
    first, _ = _twins(env)
    env.gate.decide(first, env.decision(PROMOTE, env.subject(first)))
    anchor = FileExternalAnchor(anchor_dir(tmp_path))
    for log_name, file_name in (("training-job-events", "jobs.jsonl"), ("model-registry", "registry.jsonl"),
                                ("training-decision-ledger", "decisions.jsonl")):
        entries = _lines(tmp_path / file_name)
        assert anchor.latest(log_name) == AnchorHead(log_name, len(entries), entries[-1]["entry_hash"])
        stored = json.loads((anchor_dir(tmp_path) / f"{log_name}.anchor.json").read_text(encoding="utf-8"))
        assert set(stored) == {"schema", "log_name", "seq", "head_hash"}
    verdict = env.gate.verified_state(first)
    assert verdict.state is CandidateState.PROMOTED and verdict.anchoring is Anchoring.ANCHORED
    assert env.gate.anchoring is Anchoring.ANCHORED and env.jobs.anchoring is Anchoring.ANCHORED
    assert env.gate.descriptor_draft(first, **DRAFT_ARGS).anchoring is Anchoring.ANCHORED


def test_anchored_coordinated_rollback_is_detected_and_replay_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path, anchored=True)
    first, _ = _twins(env)
    env.jobs.create(env.contract(seed=13), job_id="job-00000003")
    before = snapshot_logs(tmp_path)
    record = env.decision(PROMOTE, env.subject(first))
    env.gate.decide(first, record)
    run_record = env.decision(RUN, env.run_subject("job-00000003"))
    env.jobs.approve("job-00000003", run_record)

    restore_logs(tmp_path, before)
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.verified_state(first)
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.decide(first, record)
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
        env.jobs.approve("job-00000003", run_record)
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
        env.jobs.get("job-00000003")


def test_anchored_rollback_of_the_decision_ledger_alone_fails_closed(tmp_path: Path) -> None:
    env = make_env(tmp_path, anchored=True)
    first, second = _twins(env)
    env.jobs.create(env.contract(seed=14), job_id="job-00000003")
    ledger = tmp_path / "decisions.jsonl"
    saved = ledger.read_bytes()
    env.gate.decide(first, env.decision(PROMOTE, env.subject(first)))
    ledger.write_bytes(saved)  # the consumption entry disappears
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.verified_state(first)
    with pytest.raises(RegistryError, match="LEDGER_ROLLBACK_DETECTED"):
        env.gate.decide(second, env.decision(PROMOTE, env.subject(second)))
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
        env.jobs.approve("job-00000003", env.decision(RUN, env.run_subject("job-00000003")))


def test_anchored_rewrite_below_the_head_is_detected(tmp_path: Path) -> None:
    env = make_env(tmp_path, anchored=True)
    env.jobs.create(env.contract(), job_id="job-00000001")
    env.jobs.create(env.contract(seed=15), job_id="job-00000002")

    def rewrite(entries: list[dict[str, Any]]) -> None:  # same length, consistent chain, other content
        entries[-1]["at"] = entries[0]["at"]

    rechain(tmp_path / "jobs.jsonl", rewrite)
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):
        env.jobs.all()


def test_partial_anchoring_is_reported_unanchored(tmp_path: Path) -> None:
    env = make_env(tmp_path / "plain", anchored=False)
    anchor_dir(tmp_path).mkdir()
    anchored_log = HashChainedLog(tmp_path / "x.jsonl", fields=("seq", "v", "prev_hash", "entry_hash"),
                                  corrupt_code="X_CORRUPT", log_name="x-log",
                                  anchor=FileExternalAnchor(anchor_dir(tmp_path)))
    assert anchored_log.anchoring is Anchoring.ANCHORED
    assert env.gate.anchoring is Anchoring.UNANCHORED and env.jobs.anchoring is Anchoring.UNANCHORED
    anchored = make_env(tmp_path / "anchored")
    no_registry_log = TrainingJobs(tmp_path / "anchored" / "jobs.jsonl", verifier=anchored.verifier,
                                   anchor=anchored.anchor)  # job log + ledger anchored, registry log unknown
    assert anchored.jobs.anchoring is Anchoring.ANCHORED and no_registry_log.anchoring is Anchoring.UNANCHORED


# --- FileExternalAnchor and the ExternalAnchor protocol ----------------------------------------------------


def test_file_anchor_is_monotonic_and_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "anchor"
    directory.mkdir()
    anchor = FileExternalAnchor(directory)
    assert anchor.latest("model-registry") is None  # never bound
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.record("model-registry", 1, "a" * 64)  # only a log bound through this anchor records
    anchor.bind("model-registry", "1" * 64)
    assert anchor.latest("model-registry") == AnchorHead("model-registry", 0, GENESIS)
    anchor.record("model-registry", 2, "a" * 64)
    anchor.record("model-registry", 2, "a" * 64)  # same head again: no-op
    anchor.record("model-registry", 3, "b" * 64)
    for seq, head in ((2, "c" * 64), (3, "c" * 64)):
        with pytest.raises(ChainedLogError, match="LEDGER_ROLLBACK_DETECTED"):
            anchor.record("model-registry", seq, head)
    assert anchor.latest("model-registry") == AnchorHead("model-registry", 3, "b" * 64)
    for bad in ((0, "a" * 64), (4, "XYZ"), (True, "a" * 64)):
        with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
            anchor.record("model-registry", *bad)  # type: ignore[arg-type]
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.latest("../escape")
    path = directory / "model-registry.anchor.json"
    path.write_text('{"schema": "x"}', encoding="utf-8")
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.latest("model-registry")
    path.unlink()
    target = directory / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    create_link_or_skip(target, path, directory=False)
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.latest("model-registry")


def test_anchor_must_live_outside_the_log_store_and_be_an_external_anchor(tmp_path: Path) -> None:
    fields = ("seq", "v", "prev_hash", "entry_hash")
    for inside in (tmp_path, tmp_path / "anchors", tmp_path.parent):
        with pytest.raises(ValueError, match="ANCHOR_INSIDE_LOG_STORE"):
            HashChainedLog(tmp_path / "x.jsonl", fields=fields, corrupt_code="X", log_name="x-log",
                           anchor=FileExternalAnchor(inside))
    with pytest.raises(TypeError, match="EXTERNAL_ANCHOR_REQUIRED"):
        HashChainedLog(tmp_path / "x.jsonl", fields=fields, corrupt_code="X", log_name="x-log",
                       anchor=object())  # type: ignore[arg-type]


class MemoryAnchor:
    """An operator-supplied anchor (e.g. a WORM bucket or a second machine), here held in memory."""

    def __init__(self) -> None:
        self.heads: dict[str, AnchorHead] = {}
        self.owners: dict[str, str] = {}
        self.fail = False

    def bind(self, log_name: str, log_id: str) -> None:
        if self.owners.setdefault(log_name, log_id) != log_id:
            raise ChainedLogError("ANCHOR_LOG_NAME_IN_USE")
        self.heads.setdefault(log_name, AnchorHead(log_name, 0, GENESIS))

    def record(self, log_name: str, seq: int, head_hash: str) -> None:
        if self.fail:
            raise OSError("anchor offline")
        self.heads[log_name] = AnchorHead(log_name, seq, head_hash)

    def latest(self, log_name: str) -> AnchorHead | None:
        if self.fail:
            raise OSError("anchor offline")
        return self.heads.get(log_name)


def test_custom_external_anchor_detects_truncation_and_fails_closed_when_unavailable(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    assert isinstance(anchor, ExternalAnchor)
    log = HashChainedLog(tmp_path / "x.jsonl", fields=("seq", "v", "prev_hash", "entry_hash"),
                         corrupt_code="X_CORRUPT", log_name="x-log", anchor=anchor)
    for value in range(3):
        log.append(lambda entries, value=value: {"v": value})
    assert anchor.heads["x-log"].seq == 3
    data = (tmp_path / "x.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "x.jsonl").write_text("\n".join(data[:2]) + "\n", encoding="utf-8")
    with pytest.raises(ChainedLogError, match="LEDGER_ROLLBACK_DETECTED"):
        log.entries()
    anchor.fail = True
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        log.entries()
