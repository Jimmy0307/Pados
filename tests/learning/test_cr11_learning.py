"""Codex CR-11 (Outcome Learning) regression tests: OL-01 .. OL-07."""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.capability.receipts import VerificationStatus
from pados.capability.seams import apply_advice
from pados.connectivity.state import ConnectivityState
from pados.enterprise.classification import DataClassification
from pados.learning import anchor as anchor_module
from pados.learning.advice_log import (
    AdviceLog,
    AdviceLogEntry,
    AdviceRecordBody,
    AdviceStatus,
    AnchorStatus,
    EntryKind,
    LoggedAdvice,
    PruneReason,
)
from pados.learning.anchor import Checkpoint, FileAnchor
from pados.learning.advisor import (
    ADVISOR_VERSION,
    AdvisorConfig,
    OutcomeAdvisor,
    VerificationLevel,
    advice_context_hash,
)
from pados.learning.records import (
    FailureCode,
    HardwareClass,
    HumanDecision,
    OutcomeRecord,
    decision_ref_for,
    failure_code_for,
)
from pados.learning.store import EvaluationStore, LearningStoreError, OutcomeStore
from pados.resources.hardware import AcceleratorRuntime
from tests.learning.learning_support import (
    BINDING,
    CONF,
    NOW,
    SCANNED_ZH,
    ListSource,
    TrustedReceipts,
    adapter,
    file_log,
    history,
    memory_log,
    outcome,
    plan_receipt,
    sha,
    stage_receipt,
)
from tests.learning.test_records import evaluation

A, B, C = adapter("ocr.engine-a"), adapter("ocr.engine-b"), adapter("ocr.engine-c")
TASK = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR")
LABEL = "CONFIDENTIAL_" + "ACQUISITION_TARGET"  # a content-bearing label that must never persist


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def records(auth: TrustedReceipts | None = None) -> list[OutcomeRecord]:
    kwargs = {} if auth is None else {"auth": auth}
    return [*history("ocr.engine-a", 38, 2, **kwargs), *history("ocr.engine-b", 25, 15, offset=100, **kwargs)]


def enabled(source: list[OutcomeRecord] | None = None, log: AdviceLog | None = None,
            clock: Clock | None = None) -> OutcomeAdvisor:
    return OutcomeAdvisor(ListSource(records() if source is None else source), config=AdvisorConfig(enabled=True),
                          log=log or memory_log(), clock=clock or (lambda: NOW))


def ctx(candidates: tuple[object, ...] = (A, B), classification: DataClassification = CONF,
        plan_binding_hash: str = BINDING) -> str:
    return advice_context_hash(**TASK, context_features=SCANNED_ZH, candidates=candidates,  # type: ignore[arg-type]
                               classification=classification, plan_binding_hash=plan_binding_hash)


def store(tmp_path: Path, auth: TrustedReceipts, clock: Clock | None = None) -> OutcomeStore:
    return OutcomeStore(tmp_path / "learning" / "outcomes.jsonl", authenticator=auth, clock=clock or Clock(NOW))


# --- OL-01: ingestion is authenticated ----------------------------------------------------------

def test_cr11_ol01_forged_outcomes_are_refused(tmp_path: Path) -> None:
    auth = TrustedReceipts()
    outcomes = store(tmp_path, auth)
    genuine_fail = outcome(1, success=False, auth=auth)
    rejected = outcome(2, human=HumanDecision.REJECTED, auth=auth)
    forged = [
        OutcomeRecord.model_validate({**outcome(3, auth=TrustedReceipts()).model_dump()}),  # receipt unknown here
        OutcomeRecord.model_validate({**genuine_fail.model_dump(), "success": True, "failure_code": None}),
        OutcomeRecord.model_validate({**rejected.model_dump(), "human_decision": "ACCEPTED"}),
        OutcomeRecord.model_validate({**genuine_fail.model_dump(), "latency_ms": 1}),
        OutcomeRecord.model_validate({**genuine_fail.model_dump(), "adapter_id": "ocr.engine-z"}),
    ]
    for record in forged:
        with pytest.raises(LearningStoreError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
            outcomes.append(record)
    assert outcomes.append(genuine_fail).appended and outcomes.append(rejected).appended


def test_cr11_ol01_reads_reauthenticate_and_count_exclusions(tmp_path: Path) -> None:
    auth = TrustedReceipts()
    outcomes = store(tmp_path, auth)
    kept, revoked, overturned = (outcome(1, auth=auth), outcome(2, auth=auth),
                                 outcome(3, human=HumanDecision.ACCEPTED, auth=auth))
    for record in (kept, revoked, overturned):
        outcomes.append(record)
    auth.revoke(revoked.receipt_hash)
    plan = auth.resolve_receipt(overturned.receipt_hash)
    assert plan is not None and plan.human_decision_ref is not None
    auth.decide(plan.human_decision_ref, HumanDecision.REJECTED)
    result = outcomes.read_checked()
    assert [r.outcome_id for r in result.records] == [kept.outcome_id]
    assert result.unauthenticated == 2

    class Exploding(TrustedReceipts):
        def resolve_receipt(self, stage_receipt_hash: str) -> None:  # type: ignore[override]
            raise RuntimeError("receipt store down")

    down = OutcomeStore(outcomes.path, authenticator=Exploding(), clock=Clock(NOW)).read_checked()
    assert down.records == () and down.unauthenticated == 3  # fail closed


def test_cr11_ol01_from_receipts_requires_the_trusted_envelope() -> None:
    auth = TrustedReceipts()
    plan = auth.register(plan_receipt(stage_receipt(plan_id="p-7"), plan_id="p-7", human_decision_ref="appr-7"))
    auth.decide("appr-7", HumanDecision.ACCEPTED)
    record = OutcomeRecord.from_receipts(plan, plan.stages[0], authenticator=auth,
                                         hardware_class=HardwareClass.CUDA, network_state=ConnectivityState.CONNECTED,
                                         context_features=SCANNED_ZH, recorded_at=NOW)
    assert record.human_decision is HumanDecision.ACCEPTED and record.human_decision_ref == decision_ref_for("appr-7")


# --- OL-02: durable, sequenced, hash-chained advice log -------------------------------------------

def test_cr11_ol02_enabled_advisor_requires_a_durable_log(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ADVICE_LOG_DURABLE_REQUIRED"):
        OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True))
    assert file_log(tmp_path).durable and not memory_log().durable and memory_log().for_tests
    OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True), log=file_log(tmp_path))


def _advise_many(target: OutcomeAdvisor, count: int) -> None:
    """The OPERATOR prunes before each advice (CR-11d OL-14: no autonomous deletion in the log itself)."""
    for index in range(count):
        if target.log is not None:
            target.log.prune()
        target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                      plan_binding_hash=sha(f"plan-draft-{index}"))


def test_cr11_ol02_chain_sequence_and_plan_binding(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    target = enabled(log=log)
    _advise_many(target, 3)
    entries = log.entries()
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == "0" * 64 and entries[1].prev_hash == entries[0].entry_hash
    assert log.find(sha("plan-draft-2")) == (entries[-1],)
    advice = target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                           plan_binding_hash=sha("plan-draft-9"))
    assert apply_advice(["ocr.engine-a", "ocr.engine-b"], advice, context_hash=ctx()) == {}  # binding differs
    assert apply_advice(["ocr.engine-a", "ocr.engine-b"], advice,
                        context_hash=ctx(plan_binding_hash=sha("plan-draft-9")))
    assert log.verify().ok


def test_cr11_ol02_verify_detects_edits_gaps_head_deletion_and_truncation(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    _advise_many(enabled(log=log), 4)
    path = tmp_path / "learning" / "advice.jsonl"
    original = path.read_text().splitlines()
    anchor = log.checkpoint()
    assert anchor is not None and anchor.seq == 4 and log.verify(checkpoint=anchor).ok

    path.write_text("\n".join(original).replace('"score":0.16748', '"score":0.25') + "\n")
    assert log.verify().code == "LEARNING_STORE_CORRUPT"  # entry hash no longer matches
    path.write_text("\n".join([original[0], *original[2:]]) + "\n")
    assert log.verify().code == "ADVICE_LOG_CHAIN_BROKEN"
    path.write_text("\n".join(original[1:]) + "\n")
    assert log.verify().code == "ADVICE_LOG_HEAD_UNDOCUMENTED"
    path.write_text("\n".join(original[:3]) + "\n")
    assert file_log(tmp_path, anchor=None).verify().ok  # internally consistent...
    assert log.verify().code == "ADVICE_LOG_TRUNCATED"  # ...but the external anchor holds seq 4
    assert log.verify(checkpoint=anchor).code == "ADVICE_LOG_TRUNCATED"
    path.write_text("\n".join(original[1:]) + "\n")
    config = AdvisorConfig(enabled=True)
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_HEAD_UNDOCUMENTED"):  # a broken chain blocks writes
        log.record(AdviceRecordBody(logged_at=NOW.isoformat(), advisor_version=ADVISOR_VERSION,
                                    config_hash=config.config_hash, min_samples=config.min_samples,
                                    status=AdviceStatus.CONTEXT_INVALID))


def test_cr11_ol02_pruning_writes_an_explicit_marker_and_stays_verifiable(tmp_path: Path) -> None:
    log = file_log(tmp_path, max_entries=4)
    target = enabled(log=log)
    _advise_many(target, 2)
    anchor = log.checkpoint()
    _advise_many(target, 4)
    every = log.entries(include_markers=True)
    markers = [e for e in every if e.kind is EntryKind.PRUNED]
    assert markers and all(m.pruned is not None and PruneReason.CAPACITY in m.pruned.reasons for m in markers)
    assert len(every) <= 4 and log.verify().ok and log.verify(checkpoint=anchor).ok
    assert log.verify(checkpoint=anchor).anchor is AnchorStatus.ANCHORED_THROUGH_MARKER
    assert [e.seq for e in every] == list(range(every[0].seq, every[-1].seq + 1))


def _log_from_process(path: str, start: int, results: Any) -> None:
    log = AdviceLog(Path(path), clock=lambda: NOW, anchor=FileAnchor(Path(path).parent.parent / "anchor"))
    record = log.record
    record_failures = []
    logged = []

    def record_with_diagnostic(body: AdviceRecordBody) -> AdviceLogEntry:
        try:
            return record(body)
        except Exception as exc:  # noqa: BLE001 - this test reports the fail-closed logging cause
            chain = []
            current: BaseException | None = exc
            while current is not None:
                chain.append((type(current).__name__, str(current)))
                current = current.__cause__ or current.__context__
            record_failures.append((start, body.plan_binding_hash, chain))
            raise

    log.record = record_with_diagnostic  # type: ignore[method-assign]
    target = OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True), log=log, clock=lambda: NOW)
    for index in range(start, start + 10):
        result = target.advise_logged(**TASK, candidates=[A], context_features=SCANNED_ZH, classification=CONF,
                                      plan_binding_hash=sha(f"p-{index}"))
        logged.append((start, index, result.entry_seq, result.entry_hash))
    results.send((logged, record_failures))
    results.close()


def test_cr11_ol02_log_is_process_safe(tmp_path: Path) -> None:
    path = tmp_path / "learning" / "advice.jsonl"
    context = multiprocessing.get_context("spawn")
    pipes = []
    workers = []
    for start in (0, 100):
        receiver, sender = context.Pipe(duplex=False)
        pipes.append(receiver)
        workers.append(context.Process(target=_log_from_process, args=(str(path), start, sender)))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0
    results = [pipe.recv() for pipe in pipes]
    for pipe in pipes:
        pipe.close()
    logged = [entry for worker_results, _ in results for entry in worker_results]
    record_failures = [failure for _, worker_failures in results for failure in worker_failures]
    assert not record_failures, f"advice log append failures: {record_failures}"
    missing_results = [(start, index, entry_seq, entry_hash)
                       for start, index, entry_seq, entry_hash in logged
                       if entry_seq is None or entry_hash is None]
    assert not missing_results, f"advice calls were not durably logged: {missing_results}"
    log = AdviceLog(path, clock=lambda: NOW, anchor=FileAnchor(tmp_path / "anchor"))
    assert log.verify().ok and [e.seq for e in log.entries()] == list(range(1, 21))
    assert log.verify().anchor is AnchorStatus.ANCHORED  # both processes advanced the anchor by CAS


@pytest.mark.skipif(sys.platform != "win32", reason="Windows atomic-replace sharing errors are platform-specific")
def test_cr11_ol02_anchor_retries_transient_windows_replace_refusal(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    first = Checkpoint(seq=1, entry_hash="a" * 64)
    second = Checkpoint(seq=2, entry_hash="b" * 64)
    replace = anchor_module.os.replace
    attempts = 0

    def transient_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            refusal = PermissionError("transient Windows sharing refusal")
            refusal.winerror = 5
            raise refusal
        replace(source, destination)

    monkeypatch.setattr(anchor_module.os, "replace", transient_replace)
    anchor.record(None, first)
    assert attempts == 3 and anchor.latest() == first

    attempts = 0

    def persistent_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        refusal = PermissionError("persistent Windows sharing refusal")
        refusal.winerror = 5
        raise refusal

    monkeypatch.setattr(anchor_module.os, "replace", persistent_replace)
    with pytest.raises(PermissionError, match="persistent Windows sharing refusal"):
        anchor.record(first, second)
    assert attempts == 8 and anchor.latest() == first


# --- OL-03: advice binds candidates (ids + versions) and classification ----------------------------

def test_cr11_ol03_candidate_set_and_classification_are_bound() -> None:
    target = enabled([*records(), *history("ocr.engine-c", 30, 0, offset=200)])
    advice = target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                           plan_binding_hash=BINDING)
    feasible = ["ocr.engine-a", "ocr.engine-b", "ocr.engine-c"]
    assert set(apply_advice(feasible, advice, context_hash=ctx())) == {"ocr.engine-a", "ocr.engine-b"}
    assert apply_advice(feasible, advice, context_hash=ctx((A,))) == {}
    assert apply_advice(feasible, advice, context_hash=ctx((A, B, C))) == {}
    assert apply_advice(feasible, advice, context_hash=ctx(classification=DataClassification.RESTRICTED)) == {}
    assert target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, plan_binding_hash=BINDING) == ()
    assert target.log is not None and target.log.entries()[-1].status is AdviceStatus.BINDING_INCOMPLETE


def test_cr11_ol03_old_version_advice_cannot_apply_after_upgrade() -> None:
    target = enabled()
    advice = target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                           plan_binding_hash=BINDING)
    assert apply_advice(["ocr.engine-a", "ocr.engine-b"], advice, context_hash=ctx())
    upgraded = adapter("ocr.engine-a", version="2")
    after = target.context_hash(**TASK, context_features=SCANNED_ZH, candidates=[upgraded, B], classification=CONF,
                                plan_binding_hash=BINDING)
    assert apply_advice(["ocr.engine-a", "ocr.engine-b"], advice, context_hash=after) == {}


# --- OL-04: closed codes, closed hardware classes, opaque identifiers --------------------------------

def test_cr11_ol04_content_like_labels_never_persist(tmp_path: Path) -> None:
    auth = TrustedReceipts()
    stage = stage_receipt(plan_id="p-label", failure_code=LABEL, verification=VerificationStatus.NOT_VERIFIED,
                          resource_id=LABEL)
    plan = auth.register(plan_receipt(stage, plan_id="p-label", human_decision_ref=LABEL), task_type=LABEL)
    auth.decide(LABEL, HumanDecision.REJECTED)
    record = OutcomeRecord.from_receipts(plan, plan.stages[0], authenticator=auth,
                                         hardware_class=HardwareClass.UNKNOWN,
                                         network_state=ConnectivityState.OFFLINE,
                                         context_features=SCANNED_ZH, recorded_at=NOW)
    assert record.failure_code is FailureCode.ADAPTER_FAILURE
    outcomes = store(tmp_path, auth)
    outcomes.append(record)
    log = file_log(tmp_path)
    labelled = adapter(LABEL.lower(), capability_id=LABEL)  # an operator-registered adapter with a telling name
    OutcomeAdvisor(outcomes, config=AdvisorConfig(enabled=True), log=log, clock=lambda: NOW).advise(
        task_type=LABEL, capability_id=LABEL, candidates=[labelled], context_features=SCANNED_ZH,
        classification=CONF, plan_binding_hash=BINDING)
    assert log.entries()[-1].candidates  # the candidate WAS logged (as a digest)
    evaluations = EvaluationStore(tmp_path / "learning" / "evaluations.jsonl", clock=lambda: NOW)
    evaluations.append(evaluation(task_type=LABEL, evaluator=LABEL, capability_id=LABEL,
                                  adapter_id=LABEL.lower(), adapter_version="acquisition-v1"))
    for name in ("outcomes.jsonl", "outcomes.jsonl.ingest", "advice.jsonl", "evaluations.jsonl"):
        raw = (tmp_path / "learning" / name).read_text()
        assert raw and "acquisition" not in raw.lower(), name


def test_cr11_ol04_closed_vocabularies() -> None:
    assert failure_code_for("TIMEOUT") is FailureCode.TIMEOUT
    assert failure_code_for(LABEL) is FailureCode.ADAPTER_FAILURE
    assert failure_code_for(None, partial=True) is FailureCode.PARTIAL_OUTPUT
    assert {h.value for h in HardwareClass} == {r.value for r in AcceleratorRuntime} | {"UNKNOWN"}
    with pytest.raises(ValueError):
        OutcomeRecord.model_validate({**outcome(1).model_dump(), "hardware_class": "cpu.x86"})
    with pytest.raises(ValueError):
        OutcomeRecord.model_validate({**outcome(1, success=False).model_dump(), "failure_code": LABEL})


# --- OL-05: insecure files are refused on every open ---------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and permission bits")
def test_cr11_ol05_permissive_or_foreign_files_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = TrustedReceipts()
    outcomes = store(tmp_path, auth)
    outcomes.append(outcome(1, auth=auth))
    log = file_log(tmp_path)
    _advise_many(enabled(log=log), 1)
    evaluations = EvaluationStore(tmp_path / "learning" / "evaluations.jsonl", clock=lambda: NOW)
    evaluations.append(evaluation())
    for path, read in ((outcomes.path, outcomes.read), (tmp_path / "learning" / "advice.jsonl", log.entries),
                       (evaluations.path, evaluations.read)):
        os.chmod(path, 0o640)
        with pytest.raises(LearningStoreError, match="LEARNING_STORE_INSECURE"):
            read()
        os.chmod(path, 0o600)
        read()
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 4242)
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_INSECURE"):
        outcomes.read()


# --- OL-06: physical pruning on read; future-dated entries dropped; memory logs age out -------------

def test_cr11_ol06_reads_hide_and_operator_prune_deletes_expired_and_future_dated_lines(tmp_path: Path) -> None:
    """CR-11d OL-14: reads never delete; the operator-invoked ``prune()`` is the only deletion path."""
    auth = TrustedReceipts()
    clock = Clock(NOW)
    outcomes = store(tmp_path, auth, clock)
    outcomes.append(outcome(1, auth=auth))
    outcomes.append(outcome(2, auth=auth))
    future = outcome(3, at=NOW + timedelta(days=2), auth=auth)
    with outcomes.path.open("a") as handle:
        handle.write(json.dumps(future.to_record(), sort_keys=True, separators=(",", ":")) + "\n")
    result = outcomes.read_checked()
    assert result.pruned == 0 and len(result.records) == 2
    assert future.outcome_id in outcomes.path.read_text()  # hidden by the read, not deleted
    assert outcomes.prune() == 1 and future.outcome_id not in outcomes.path.read_text()
    clock.now = NOW + timedelta(days=20)
    assert outcomes.read_checked().records == () and outcomes.path.read_text() != ""
    assert outcomes.prune() == 2 and outcomes.path.read_text() == ""


def test_cr11_ol06_memory_log_ages_out_and_future_tail_is_dropped_with_a_marker() -> None:
    clock = Clock(NOW)
    log = memory_log(clock=clock)
    target = enabled(log=log, clock=clock)
    _advise_many(target, 2)
    clock.now = NOW + timedelta(days=15)
    assert log.entries() == ()  # hidden by the read
    assert len(log.entries(include_markers=True)) == 0 and log.verify().ok  # ...and still stored, verifiable
    _advise_many(target, 1)  # the operator prune runs first
    marker = log.entries(include_markers=True)[0]
    assert marker.pruned is not None and PruneReason.EXPIRED in marker.pruned.reasons
    clock.now = NOW + timedelta(days=16)
    _advise_many(target, 1)  # written "in the future" relative to the next clock
    clock.now = NOW + timedelta(days=15, hours=1)
    assert log.verify().code == "ADVICE_LOG_FUTURE_DATED"  # verify never modifies the log
    assert len(log.entries()) == 1 and log.verify().code == "ADVICE_LOG_FUTURE_DATED"  # a read never deletes
    assert log.prune() == 1 and log.verify().ok  # only the operator prune deletes the future-dated entry
    last_marker = [e for e in log.entries(include_markers=True) if e.kind is EntryKind.PRUNED][-1]
    assert last_marker.pruned is not None and PruneReason.FUTURE_DATED in last_marker.pruned.reasons


# --- OL-07: scores re-derive from the logged snapshot alone ----------------------------------------

def test_cr11_ol07_snapshot_reproduces_the_score_after_records_are_gone() -> None:
    source = ListSource(records())
    target = OutcomeAdvisor(source, config=AdvisorConfig(enabled=True), log=memory_log(), clock=lambda: NOW)
    target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                  plan_binding_hash=BINDING)
    assert target.log is not None
    (entry,) = target.log.find(BINDING)
    assert entry.advice
    snap = next(item.snapshot for item in entry.advice if item.snapshot.sample_size == 40
                and item.snapshot.good_outcomes == 38)
    assert snap.min_samples == 20
    source.records.clear()  # R1 pruning removed the evidence
    assert target.verify(entry).level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED
    assert target.verify(entry).code == "RECORDS_DO_NOT_REPRODUCE"
    inflated = entry.advice[0].model_copy(update={"score": 0.25})
    assert not target.verify(entry.model_copy(update={"advice": (inflated, *entry.advice[1:])})).ok
    padded_snap = snap.model_copy(update={"good_outcomes": 40, "successes": 40})
    padded = LoggedAdvice(adapter_ref=snap.adapter_ref, score=entry.advice[0].score, snapshot=padded_snap,
                          snapshot_hash=padded_snap.snapshot_hash)
    assert not target.verify(entry.model_copy(update={"advice": (padded, *entry.advice[1:])})).ok
    with pytest.raises(ValueError, match="ADVICE_SNAPSHOT_HASH_MISMATCH"):
        AdviceRecordBody.model_validate({**AdviceRecordBody.model_validate(
            {k: v for k, v in entry.model_dump().items() if k not in {"seq", "prev_hash", "entry_hash"}}).model_dump(),
            "advice": [{**entry.advice[0].model_dump(), "snapshot": padded_snap.model_dump()}]})
