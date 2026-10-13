"""CR-12c-01: an entry made durable before its anchor record is never accepted while its head is unanchored.

The appending log object records the outstanding head before accepting it on the next read (recovery);
every other reader, and that same object while the anchor is still unavailable, fails closed with
``LEDGER_HEAD_NOT_ANCHORED``. After a crash the operator confirms the outstanding head explicitly."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from pados.execution.context import canonical_hash
from pados.training._chain import AnchorHead, ChainedLogError, FileExternalAnchor, HashChainedLog
from pados.training.decisions import (
    ConsumedDecisionLedger,
    DecisionBinding,
    DecisionOperation,
    DecisionVerifier,
)
from pados.training.jobs import JobState, TrainingJobError, TrainingJobs
from pados.training.registry import CandidateState, RegistryError

from .support import Env, anchor_dir, make_env

RUN = DecisionOperation.APPROVE_TRAINING_RUN
PROMOTE = DecisionOperation.PROMOTE_MODEL


class FlakyAnchor:
    """A file anchor whose ``record`` fails while ``down`` names the log (the anchor store went away)."""

    def __init__(self, directory: Path) -> None:
        self.inner = FileExternalAnchor(directory)
        self.down: set[str] = set()

    def bind(self, log_name: str, log_id: str) -> None:
        self.inner.bind(log_name, log_id)

    def record(self, log_name: str, seq: int, head_hash: str) -> None:
        if log_name in self.down:
            raise OSError("anchor store unavailable")
        self.inner.record(log_name, seq, head_hash)

    def latest(self, log_name: str) -> AnchorHead | None:
        return self.inner.latest(log_name)


def _flaky_env(tmp: Path) -> tuple[Env, FlakyAnchor]:
    directory = anchor_dir(tmp)
    directory.mkdir(parents=True)
    anchor = FlakyAnchor(directory)
    return make_env(tmp, anchor=anchor), anchor


def _head(path: Path) -> tuple[int, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return len(lines), json.loads(lines[-1])["entry_hash"]


def _anchored(anchor: FlakyAnchor, log_name: str) -> tuple[int, str]:
    head = anchor.latest(log_name)
    assert head is not None
    return head.seq, head.head_hash


def test_interrupted_job_log_append_is_recovered_before_it_is_accepted(tmp_path: Path) -> None:
    env, anchor = _flaky_env(tmp_path)
    env.jobs.create(env.contract(), job_id="job-00000001")
    record = env.decision(RUN, env.run_subject("job-00000001"))
    anchor.down.add("training-job-events")
    with pytest.raises(TrainingJobError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        env.jobs.approve("job-00000001", record)  # the APPROVED entry is durable, its head is not anchored
    assert _head(tmp_path / "jobs.jsonl")[0] == _anchored(anchor, "training-job-events")[0] + 1

    with pytest.raises(TrainingJobError, match="LEDGER_HEAD_NOT_ANCHORED"):
        env.jobs.get("job-00000001")  # retry while the anchor is still down: fail closed
    other_process = TrainingJobs(tmp_path / "jobs.jsonl", verifier=env.verifier, clock=env.clock,
                                 registry_log=tmp_path / "registry.jsonl", anchor=anchor.inner)
    with pytest.raises(TrainingJobError, match="LEDGER_HEAD_NOT_ANCHORED"):
        other_process.get("job-00000001")  # a reader that did not append never accepts the suffix
    with pytest.raises(RegistryError, match="LEDGER_HEAD_NOT_ANCHORED"):
        env.registry.verify_heads()

    anchor.down.clear()
    retried = env.jobs.approve("job-00000001", record)  # recovery records the head, then the replay is a no-op
    assert retried.state is JobState.APPROVED_TO_RUN and retried.approval_decision == record
    assert _anchored(anchor, "training-job-events") == _head(tmp_path / "jobs.jsonl")
    assert other_process.get("job-00000001").state is JobState.APPROVED_TO_RUN
    assert env.jobs.start("job-00000001", approval_id=record["decision_id"]).state is JobState.RUNNING


def test_interrupted_ledger_append_never_lets_the_unanchored_consumption_count(tmp_path: Path) -> None:
    env, anchor = _flaky_env(tmp_path)
    env.jobs.create(env.contract(), job_id="job-00000001")
    record = env.decision(RUN, env.run_subject("job-00000001"))
    anchor.down.add("training-decision-ledger")
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_REFUSED:LEDGER_ANCHOR_UNAVAILABLE"):
        env.jobs.approve("job-00000001", record)
    assert env.jobs.get("job-00000001").state is JobState.DRAFT  # the job moved nowhere
    fresh_ledger = ConsumedDecisionLedger(tmp_path / "decisions.jsonl", anchor=anchor.inner)
    with pytest.raises(ValueError, match="LEDGER_HEAD_NOT_ANCHORED"):
        fresh_ledger.consumed(record["decision_id"])
    verifier = DecisionVerifier(boundary=env.keys.boundary, registry=env.keys.registry, ledger=fresh_ledger)
    with pytest.raises(ValueError, match="LEDGER_HEAD_NOT_ANCHORED"):
        verifier.consumed_matches(record, verifier_binding(env, "job-00000001"))

    anchor.down.clear()  # retry: the ledger records its outstanding head first; the decision stays used once
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_REFUSED:DECISION_ALREADY_CONSUMED"):
        env.jobs.approve("job-00000001", record)
    assert _anchored(anchor, "training-decision-ledger") == _head(tmp_path / "decisions.jsonl")
    assert fresh_ledger.consumed(record["decision_id"]) is not None


def verifier_binding(env: Env, job_id: str) -> DecisionBinding:
    return DecisionBinding(RUN, env.run_subject(job_id))


def test_interrupted_registry_append_blocks_the_gate_until_its_head_is_anchored(tmp_path: Path) -> None:
    env, anchor = _flaky_env(tmp_path)
    candidate, _ = env.registered()
    env.registry.request_promotion(candidate, requested_by="bob.engineer")
    record = env.decision(PROMOTE, env.subject(candidate))
    anchor.down.add("model-registry")
    with pytest.raises(RegistryError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        env.gate.decide(candidate, record)  # PROMOTED is durable, its head is not anchored
    for read in (lambda: env.gate.verified_state(candidate),
                 lambda: env.gate.descriptor_draft(candidate, adapter_id="model.fit-v1",
                                                   capability_id="TABULAR_PREDICT", version="1")):
        with pytest.raises(RegistryError, match="LEDGER_HEAD_NOT_ANCHORED"):
            read()
    env.jobs.create(env.contract(seed=31), job_id="job-00000031")
    with pytest.raises(TrainingJobError, match="LEDGER_HEAD_NOT_ANCHORED"):  # the registry head is checked too
        env.jobs.approve("job-00000031", env.decision(RUN, env.run_subject("job-00000031")))

    anchor.down.clear()
    verdict = env.gate.verified_state(candidate)  # the same registry object recovers its own outstanding head
    assert verdict.state is CandidateState.PROMOTED
    assert _anchored(anchor, "model-registry") == _head(tmp_path / "registry.jsonl")


def test_after_a_crash_an_unanchored_head_stays_fail_closed_and_no_recovery_api_exists(tmp_path: Path) -> None:
    """CR-12d-01 (integrator mitigation): a caller can never bless an unanchored head. After a crash the log
    stays fail-closed for every reader until the operator restores or retires it offline."""
    env, anchor = _flaky_env(tmp_path)
    env.jobs.create(env.contract(), job_id="job-00000001")
    anchor.down.add("training-job-events")
    with pytest.raises(TrainingJobError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        env.jobs.create(env.contract(seed=32), job_id="job-00000002")
    anchor.down.clear()
    restarted = TrainingJobs(tmp_path / "jobs.jsonl", verifier=env.verifier, clock=env.clock,
                             registry_log=tmp_path / "registry.jsonl", anchor=anchor.inner)  # the crash lost memory
    for _ in range(2):
        with pytest.raises(TrainingJobError, match="LEDGER_HEAD_NOT_ANCHORED"):
            restarted.all()
    for owner in (restarted, env.registry, env.verifier, HashChainedLog):
        assert not hasattr(owner, "recover_interrupted_append")


def test_more_than_one_unanchored_entry_is_never_recovered(tmp_path: Path) -> None:
    directory = tmp_path / "anchor"
    directory.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    flaky = FlakyAnchor(directory)
    log = HashChainedLog(store / "x.jsonl", fields=("seq", "v", "prev_hash", "entry_hash"), corrupt_code="X",
                         log_name="x-log", anchor=flaky)
    log.append(lambda entries: {"v": 0})
    flaky.down.add("x-log")
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        log.append(lambda entries: {"v": 1})
    with pytest.raises(ChainedLogError, match="LEDGER_HEAD_NOT_ANCHORED"):
        log.append(lambda entries: {"v": 2})  # an unanchored suffix never grows
    assert len((store / "x.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    flaky.down.clear()
    peer = HashChainedLog(store / "x.jsonl", fields=("seq", "v", "prev_hash", "entry_hash"), corrupt_code="X",
                          log_name="x-log", anchor=flaky)
    with pytest.raises(ChainedLogError, match="LEDGER_HEAD_NOT_ANCHORED"):
        peer.entries()
    log.entries()  # the appender recovers its own single outstanding entry
    seq, head_hash = _head(store / "x.jsonl")
    assert flaky.latest("x-log") == AnchorHead("x-log", seq, head_hash)

    previous = head_hash  # someone with file access appends two correctly chained entries
    with (store / "x.jsonl").open("a", encoding="utf-8") as handle:
        for number in (seq + 1, seq + 2):
            entry: dict[str, object] = {"seq": number, "v": 9, "prev_hash": previous}
            entry["entry_hash"] = previous = canonical_hash(entry)
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    for reader in (log, peer):
        with pytest.raises(ChainedLogError, match="LEDGER_HEAD_NOT_ANCHORED"):
            reader.entries()
    assert not hasattr(log, "recover_interrupted_append")  # no caller can bless the suffix (CR-12d-01)
