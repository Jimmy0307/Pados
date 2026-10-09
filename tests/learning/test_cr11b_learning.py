"""Codex CR-11b (Outcome Learning) regression tests: OL-08 .. OL-10 and the closed partials (OL-01/02/04/06)."""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pados.capability.receipts import VerificationStatus
from pados.connectivity.state import ConnectivityState
from pados.enterprise.classification import DataClassification
from pados.learning.advice_log import (
    GENESIS,
    TEST_ONLY_ACKNOWLEDGEMENT,
    AdviceLog,
    AdviceLogEntry,
    AdviceRecordBody,
    AdviceStatus,
    AnchorStatus,
    Checkpoint,
    DerivationSnapshot,
    EntryKind,
    LinkRef,
    LoggedAdvice,
    LoggedCandidate,
    PruneMarker,
    PruneReason,
    RemovedRange,
    config_hash_of,
)
from pados.learning.anchor import AnchorError, continues
from pados.learning.advisor import (
    ADVISOR_VERSION,
    UNBOUND_CONTEXT_HASH,
    AdvisorConfig,
    ConfigSource,
    OutcomeAdvisor,
    VerificationLevel,
    score_from_lower_bound,
)
from pados.learning.profiles import wilson_lower_bound
from pados.learning.records import (
    EVALUATION_KEYS,
    HardwareClass,
    LearningRecordError,
    OutcomeRecord,
    adapter_ref_for,
    task_ref_for,
)
from pados.learning.store import EvaluationStore, LearningStoreError, OutcomeStore
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

A, B = adapter("ocr.engine-a"), adapter("ocr.engine-b")
TASK = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR")
SRC = Path(__file__).resolve().parents[2] / "src" / "pados"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Anchor:
    """A compare-and-set external anchor double (in production: outside the writable store, e.g. the audit
    authority)."""

    def __init__(self) -> None:
        self.points: list[Checkpoint] = []
        self.marker_points: list[Checkpoint] = []
        self.fail = False

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        if self.fail:
            raise RuntimeError("anchor unreachable")
        if previous != (self.points[-1] if self.points else None) or not continues(previous, new):
            raise AnchorError("ANCHOR_CONFLICT")
        self.points.append(new)
        if marker:
            self.marker_points.append(new)

    def latest(self) -> Checkpoint | None:
        if self.fail:
            raise RuntimeError("anchor unreachable")
        return self.points[-1] if self.points else None

    def markers(self) -> tuple[Checkpoint, ...]:
        if self.fail:
            raise RuntimeError("anchor unreachable")
        return tuple(self.marker_points)


def records() -> list[OutcomeRecord]:
    return [*history("ocr.engine-a", 38, 2), *history("ocr.engine-b", 25, 15, offset=100)]


def advisor(log: AdviceLog | None = None, source: ListSource | None = None, **config: object) -> OutcomeAdvisor:
    return OutcomeAdvisor(source or ListSource(records()),
                          config=AdvisorConfig(enabled=True, **config),  # type: ignore[arg-type]
                          log=log or memory_log(), clock=lambda: NOW)


def advise_n(target: OutcomeAdvisor, count: int, start: int = 0) -> None:
    """Advise ``count`` times; the OPERATOR prunes before each advice (CR-11d OL-14: the log never deletes on
    its own, so these tests run the explicit operator purge the way an operator schedule would)."""
    for index in range(start, start + count):
        if target.log is not None:
            target.log.prune()
        target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                      plan_binding_hash=sha(f"plan-{index}"))


def body(when: datetime) -> AdviceRecordBody:
    config = AdvisorConfig()
    return AdviceRecordBody(logged_at=when.isoformat(), advisor_version=ADVISOR_VERSION,
                            config_hash=config.config_hash, min_samples=config.min_samples,
                            status=AdviceStatus.CONTEXT_INVALID)


def write_chain(path: Path, entries: list[AdviceLogEntry]) -> None:
    path.write_text("".join(json.dumps(e.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
                            for e in entries))


def marker(prior: LinkRef, ranges: tuple[RemovedRange, ...], when: datetime = NOW,
           ancestry: tuple[LinkRef, ...] = ()) -> AdviceLogEntry:
    prune = PruneMarker(prior_head=prior, ranges=ranges, ancestry=ancestry, removed=1,
                        reasons=(PruneReason.EXPIRED,))
    return AdviceLogEntry.seal(AdviceRecordBody(kind=EntryKind.PRUNED, logged_at=when.isoformat(), pruned=prune),
                               seq=prior.seq + 1, prev_hash=prior.entry_hash)


# --- OL-08: a checkpoint is accepted only through proven hash ancestry ------------------------------

def test_cr11b_ol08_reseal_with_fabricated_marker_below_first_seq_is_refused(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    advise_n(advisor(log), 4)
    path = tmp_path / "learning" / "advice.jsonl"
    original = list(log.entries())
    anchor = Checkpoint(2, original[1].entry_hash)
    assert log.verify(checkpoint=anchor).anchor is AnchorStatus.ANCHORED
    # A writer replaces the history with a fresh chain that "documents" the removal of 1..4.
    fake_head = LinkRef(seq=4, entry_hash=sha("fabricated-head"))
    removed = (RemovedRange(from_seq=1, through_seq=4, before_hash=GENESIS, through_hash=fake_head.entry_hash),)
    forged_marker = marker(fake_head, removed)
    forged_entry = AdviceLogEntry.seal(body(NOW), seq=6, prev_hash=forged_marker.entry_hash)
    write_chain(path, [forged_marker, forged_entry])
    plain = file_log(tmp_path, anchor=None)
    assert plain.verify().ok  # internally consistent, but...
    assert not log.verify().ok  # ...the external anchor rejects it
    report = plain.verify(checkpoint=anchor)
    assert not report.ok and report.code == "ADVICE_LOG_ANCHOR_UNVERIFIABLE"
    # Committing to the anchored seq with another hash is a rewrite, not an anchor.
    forged_marker = marker(fake_head, removed, ancestry=(LinkRef(seq=2, entry_hash=sha("other")),))
    write_chain(path, [forged_marker, AdviceLogEntry.seal(body(NOW), seq=6, prev_hash=forged_marker.entry_hash)])
    assert plain.verify(checkpoint=anchor).code == "ADVICE_LOG_REWRITTEN"


def test_cr11b_ol08_checkpoint_inside_a_fabricated_gap_is_refused(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    advise_n(advisor(log), 5)
    path = tmp_path / "learning" / "advice.jsonl"
    e1, e2, _, e4, e5 = log.entries()
    fake = LinkRef(seq=5, entry_hash=sha("fabricated-5"))
    gap = marker(fake, (RemovedRange(from_seq=3, through_seq=5, before_hash=e2.entry_hash,
                                     through_hash=fake.entry_hash),))
    write_chain(path, [e1, e2, gap])
    plain = file_log(tmp_path, anchor=None)
    assert plain.verify().ok and log.verify().code == "ADVICE_LOG_REWRITTEN"  # the anchor holds e5
    inside = plain.verify(checkpoint=Checkpoint(4, e4.entry_hash))
    assert not inside.ok and inside.code == "ADVICE_LOG_ANCHOR_UNVERIFIABLE"
    assert plain.verify(checkpoint=Checkpoint(5, e5.entry_hash)).code == "ADVICE_LOG_REWRITTEN"
    # A claimed removal that bridges nothing is itself refused.
    unused = marker(e2.link, (RemovedRange(from_seq=1, through_seq=1, before_hash=GENESIS,
                                           through_hash=e1.entry_hash),))
    write_chain(path, [e1, e2, unused])
    assert plain.verify().code == "ADVICE_LOG_PRUNE_RANGE_INVALID"


def test_cr11b_ol08_legitimate_prune_keeps_boundaries_verifiable_and_never_silently_ok(tmp_path: Path) -> None:
    log = file_log(tmp_path, max_entries=5)
    target = advisor(log)
    advise_n(target, 4)
    e1, e2 = log.entries()[:2]
    advise_n(target, 6, start=10)  # several prunes (CAPACITY): 1..2 go first, later more
    assert log.verify().ok
    boundary = log.verify(checkpoint=Checkpoint(2, e2.entry_hash))
    assert boundary.ok and boundary.anchor is AnchorStatus.ANCHORED_THROUGH_MARKER  # carried as ancestry
    inside = log.verify(checkpoint=Checkpoint(1, e1.entry_hash))
    assert not inside.ok and inside.code == "ADVICE_LOG_ANCHOR_UNVERIFIABLE"
    assert log.verify(checkpoint=Checkpoint(2, sha("x"))).code == "ADVICE_LOG_REWRITTEN"
    head = log.checkpoint()
    assert head is not None and log.verify(checkpoint=head).anchor is AnchorStatus.ANCHORED


def test_cr11b_ol08_external_anchor_is_recorded_and_reported(tmp_path: Path) -> None:
    anchor = Anchor()
    log = file_log(tmp_path, anchor=anchor, max_entries=4)
    target = advisor(log)
    assert log.verify().anchor is AnchorStatus.UNANCHORED and log.verify().ok
    advise_n(target, 6)
    head = log.checkpoint()
    assert anchor.latest() == head and len(anchor.points) >= 6  # every new head (markers included) anchored
    report = log.verify()
    assert report.ok and report.anchor is AnchorStatus.ANCHORED
    assert file_log(tmp_path, anchor=None).verify().anchor is AnchorStatus.UNANCHORED  # no anchor: explicit
    # Whole-file reseal: internally consistent, but the external anchor no longer matches.
    path = tmp_path / "learning" / "advice.jsonl"
    write_chain(path, [AdviceLogEntry.seal(body(NOW), seq=1, prev_hash=GENESIS)])
    assert file_log(tmp_path, anchor=None).verify().ok and log.verify().code == "ADVICE_LOG_TRUNCATED"
    anchor.fail = True
    assert log.verify().code == "ADVICE_LOG_ANCHOR_UNAVAILABLE"
    assert target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                         plan_binding_hash=BINDING) == ()  # an unanchorable entry never yields advice


# --- OL-06: reads prune expired/future-dated entries ANYWHERE, with a chained marker ---------------

def test_cr11b_ol06_mid_log_future_and_expired_entries_are_physically_pruned_by_the_operator(tmp_path: Path) -> None:
    clock = Clock(NOW)
    log = file_log(tmp_path, clock=clock, anchor=None)  # raw chain written below: chain-level mechanics only
    path = tmp_path / "learning" / "advice.jsonl"
    log.record(body(NOW))  # creates the owner-only file
    e1 = AdviceLogEntry.seal(body(NOW), seq=1, prev_hash=GENESIS)
    e2 = AdviceLogEntry.seal(body(NOW + timedelta(days=2)), seq=2, prev_hash=e1.entry_hash)  # skewed writer
    e3 = AdviceLogEntry.seal(body(NOW - timedelta(days=20)), seq=3, prev_hash=e2.entry_hash)  # expired
    e4 = AdviceLogEntry.seal(body(NOW), seq=4, prev_hash=e3.entry_hash)
    write_chain(path, [e1, e2, e3, e4])
    assert log.verify().code == "ADVICE_LOG_FUTURE_DATED"
    assert [e.seq for e in log.entries()] == [1, 4]  # hidden by the read (CR-11d OL-14: reads never delete)
    assert '"seq":2,' in path.read_text() and '"seq":3,' in path.read_text()
    assert log.prune() == 2  # the operator's explicit purge
    raw = path.read_text()
    assert e2.entry_hash not in raw and e3.entry_hash in raw  # the run 2..3 survives only as its last hash
    assert '"seq":2,' not in raw and '"seq":3,' not in raw
    final = log.entries(include_markers=True)[-1]
    assert final.pruned is not None and set(final.pruned.reasons) == {PruneReason.EXPIRED, PruneReason.FUTURE_DATED}
    assert final.pruned.ranges == (RemovedRange(from_seq=2, through_seq=3, before_hash=e1.entry_hash,
                                                through_hash=e3.entry_hash),)
    assert log.verify().ok
    assert log.verify(checkpoint=Checkpoint(4, e4.entry_hash)).anchor is AnchorStatus.ANCHORED
    # A pruned checkpoint verifies only through a marker an external anchor recorded (CR-11c OL-08).
    assert log.verify(checkpoint=Checkpoint(3, e3.entry_hash)).code == "ADVICE_LOG_MARKER_UNANCHORED"


def test_cr11b_ol06_every_store_has_an_explicit_operator_prune(tmp_path: Path) -> None:
    clock = Clock(NOW)
    log = file_log(tmp_path, clock=clock)
    advise_n(advisor(log), 2)
    clock.now = NOW + timedelta(days=15)
    assert log.prune() == 2 and log.prune() == 0
    evaluations = EvaluationStore(tmp_path / "learning" / "evaluations.jsonl", clock=lambda: NOW)
    evaluations.append(evaluation())
    future = evaluation(metric_name="cer", evaluated_at=NOW + timedelta(days=3))
    with evaluations.path.open("a") as handle:
        handle.write(json.dumps(future.to_record(), sort_keys=True, separators=(",", ":")) + "\n")
    assert len(evaluations.read()) == 1 and future.evaluation_id in evaluations.path.read_text()
    assert evaluations.prune() == 1 and future.evaluation_id not in evaluations.path.read_text()
    assert evaluations.prune() == 0 and len(evaluations.read()) == 1  # R2: nothing goes on age


def test_cr11b_ol06_learning_has_no_timer_or_scheduler() -> None:
    banned = {"threading", "sched", "asyncio", "signal", "multiprocessing", "concurrent"}
    for path in sorted((SRC / "learning").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(name.split(".")[0] in banned for name in names), (path.name, names)


# --- OL-09: the verifier checks the whole derivation, not only the arithmetic ----------------------

def _entry(target: OutcomeAdvisor) -> AdviceLogEntry:
    result = target.advise_logged(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                                  plan_binding_hash=BINDING)
    assert target.log is not None and result.entry_hash is not None
    (entry,) = [e for e in target.log.find(BINDING) if e.entry_hash == result.entry_hash]
    return entry


def test_cr11b_ol09_levels_distinguish_arithmetic_from_full_derivation() -> None:
    source = ListSource(records())
    target = advisor(source=source)
    entry = _entry(target)
    full = target.verify(entry)
    assert full.ok and full.level is VerificationLevel.DERIVATION_VERIFIED and full.config_source is ConfigSource.ADVISOR
    assert target.verify(entry, use_records=False).level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED
    # An advice set that omits a qualifying candidate is arithmetically fine but not the derivation.
    omitted = entry.model_copy(update={"advice": entry.advice[:1]})
    assert target.verify(omitted).level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED
    assert target.verify(omitted).code == "RECORDS_DO_NOT_REPRODUCE"


def test_cr11b_ol09_claimed_lower_min_samples_is_rejected_or_reported_as_logged_config() -> None:
    target = advisor()  # requires 20 samples
    entry = _entry(target)
    # A validly sealed entry claiming a laxer configuration (5 samples) and 5 perfect observations.
    snap = DerivationSnapshot(adapter_ref=adapter_ref_for("ocr.engine-a", "1"), sample_size=5, successes=5,
                              good_outcomes=5, verified=5, verification_passed=5, human_decided=0, human_accepted=0,
                              min_samples=5, profile_hash=sha("p"), good_wilson_lower=round(wilson_lower_bound(5, 5), 6))
    item = LoggedAdvice(adapter_ref=snap.adapter_ref, score=score_from_lower_bound(snap.good_wilson_lower),
                        snapshot=snap, snapshot_hash=snap.snapshot_hash)
    lax = config_hash_of(advisor_version=ADVISOR_VERSION, min_samples=5, bucket_keys=None)
    fields = {k: v for k, v in entry.model_dump(mode="json").items() if k not in {"seq", "prev_hash", "entry_hash"}}
    forged = AdviceLogEntry.seal(AdviceRecordBody.model_validate(
        {**fields, "min_samples": 5, "config_hash": lax, "advice": [item.model_dump(mode="json")]}),
        seq=entry.seq + 1, prev_hash=entry.entry_hash)
    rejected = target.verify(forged)
    assert not rejected.ok and rejected.code == "ADVICE_CONFIG_DIFFERS"
    explicit = target.verify(forged, against_logged_config=True)
    assert explicit.config_source is ConfigSource.LOGGED
    assert explicit.level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED  # never a full derivation
    with pytest.raises(ValueError, match="ADVICE_CONFIG_HASH_MISMATCH"):  # inputs must hash to config_hash
        AdviceRecordBody.model_validate({**fields, "min_samples": 5})
    assert target.verify(forged.model_copy(update={"min_samples": 20})).code == "ADVICE_CONFIG_HASH_MISMATCH"
    floor = config_hash_of(advisor_version=ADVISOR_VERSION, min_samples=1, bucket_keys=None)
    tiny = forged.model_copy(update={"min_samples": 1, "config_hash": floor})
    assert target.verify(tiny, against_logged_config=True).code == "ADVICE_CONFIG_INVALID"


@pytest.mark.parametrize(("change", "code"), [
    ({"successes": 41}, "ADVICE_COUNTERS_INCONSISTENT"),
    ({"verification_passed": 41, "verified": 40}, "ADVICE_COUNTERS_INCONSISTENT"),
    ({"human_accepted": 3, "human_decided": 2}, "ADVICE_COUNTERS_INCONSISTENT"),
    ({"successes": 40, "verification_passed": 40, "verified": 40}, "ADVICE_COUNTERS_INCONSISTENT"),
    ({"min_samples": 10}, "ADVICE_BELOW_MIN_SAMPLES"),
])
def test_cr11b_ol09_forged_snapshot_counters_are_rejected(change: dict[str, int], code: str) -> None:
    target = advisor()
    entry = _entry(target)
    first = next(item for item in entry.advice if item.snapshot.good_outcomes == 38)
    snap = first.snapshot.model_copy(update=change)
    forged_item = LoggedAdvice(adapter_ref=first.adapter_ref, score=first.score, snapshot=snap,
                               snapshot_hash=snap.snapshot_hash)
    forged = entry.model_copy(update={"advice": tuple(forged_item if item is first else item for item in entry.advice)})
    assert target.verify(forged).code == code and not target.verify(forged).ok


@pytest.mark.parametrize("field", ["plan_binding_hash", "classification", "task_ref", "context_features",
                                   "candidates_dup", "capability_ref"])
def test_cr11b_ol09_context_binding_and_candidate_uniqueness_are_reconstructed(field: str) -> None:
    target = advisor()
    entry = _entry(target)
    update: dict[str, object] = {
        "plan_binding_hash": {"plan_binding_hash": sha("another-plan")},
        "classification": {"classification": DataClassification.RESTRICTED},
        "task_ref": {"task_ref": task_ref_for("OTHER_TASK")},
        "context_features": {"context_features": (("scan_type", "PHOTO"),)},
        "candidates_dup": {"candidates": (entry.candidates[0], entry.candidates[0])},
        "capability_ref": {"capability_ref": "cap-" + "0" * 24},
    }[field]
    result = target.verify(entry.model_copy(update=update))
    assert not result.ok
    assert result.code in {"ADVICE_CONTEXT_BINDING_MISMATCH", "ADVICE_CANDIDATES_NOT_UNIQUE"}


# --- OL-10: retention ages from the trusted execution time; one ingestion per receipt per window ------

def store(tmp_path: Path, auth: TrustedReceipts, clock: Clock, **kwargs: object) -> OutcomeStore:
    return OutcomeStore(tmp_path / "learning" / "outcomes.jsonl", authenticator=auth,  # type: ignore[arg-type]
                        clock=clock, **kwargs)


def test_cr11b_ol10_reappend_is_refused_and_a_full_store_never_evicts(tmp_path: Path) -> None:
    auth, clock = TrustedReceipts(), Clock(NOW)
    outcomes = store(tmp_path, auth, clock, max_records=2)
    oldest, middle, newest = outcome(30, auth=auth), outcome(20, auth=auth), outcome(10, auth=auth)
    for record in (oldest, middle):
        outcomes.append(record)
    with pytest.raises(LearningStoreError, match="OUTCOME_STORE_FULL_PRUNE_REQUIRED"):
        outcomes.append(newest)  # CR-11d OL-14: an append never deletes a stored record
    assert {r.outcome_id for r in outcomes.read()} == {oldest.outcome_id, middle.outcome_id}
    assert not outcomes.append(oldest).appended  # the identical record is a duplicate
    refreshed = oldest.model_copy(update={"recorded_at": NOW.isoformat()})  # same receipt, fresh ingestion time
    with pytest.raises(LearningStoreError, match="LEARNING_RECORD_ID_CONFLICT|OUTCOME_ALREADY_INGESTED"):
        outcomes.append(refreshed)  # still stored: the conflicting re-ingestion is refused
    assert oldest.outcome_id in (tmp_path / "learning" / "outcomes.jsonl.ingest").read_text()


def test_cr11b_ol10_old_execution_never_reenters_the_live_window(tmp_path: Path) -> None:
    auth, clock = TrustedReceipts(), Clock(NOW)
    outcomes = store(tmp_path, auth, clock)
    old = outcome(0, at=NOW - timedelta(days=15), auth=auth)
    reingested = OutcomeRecord.model_validate({**old.model_dump(), "recorded_at": NOW.isoformat()})
    with pytest.raises(LearningStoreError, match="OUTCOME_RECORD_ALREADY_EXPIRED"):
        outcomes.append(reingested)
    aged = outcome(0, at=NOW - timedelta(days=13), auth=auth)
    outcomes.append(OutcomeRecord.model_validate({**aged.model_dump(), "recorded_at": NOW.isoformat()}))
    clock.now = NOW + timedelta(days=2)  # ingested 2 days ago, but EXECUTED 15 days ago: no longer live
    assert outcomes.read() == () and outcomes.path.read_text() != ""  # hidden; deletion is the operator's
    assert outcomes.prune() == 1 and outcomes.path.read_text() == ""
    assert aged.outcome_id not in outcomes.ledger.path.read_text()  # identity expired with it
    with pytest.raises(LearningStoreError, match="OUTCOME_RECORD_ALREADY_EXPIRED"):
        outcomes.append(aged)
    forged_time = OutcomeRecord.model_validate({**outcome(1, auth=auth).model_dump(),
                                                "executed_at": (NOW + timedelta(days=1)).isoformat(),
                                                "recorded_at": (NOW + timedelta(days=1)).isoformat()})
    with pytest.raises(LearningStoreError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        outcomes.append(forged_time)  # execution time is authenticated against the receipt


def test_cr11b_ol10_full_identity_ledger_refuses_instead_of_folding(tmp_path: Path) -> None:
    """CR-17 (OL-14 remainder): the ingestion-identity ledger never deletes or folds on its own; a full ledger
    refuses until the operator prunes, and an existing floor (older format) is still honoured."""
    auth, clock = TrustedReceipts(), Clock(NOW)
    outcomes = store(tmp_path, auth, clock, max_records=1)  # ledger capacity: 4 identities + 1
    chronological = [outcome(index, auth=auth) for index in range(8, 0, -1)]  # oldest execution first
    with outcomes._file.locked():
        for record in chronological[:5]:
            identities, floor = outcomes.ledger.load_locked(NOW, outcomes.window)
            outcomes.ledger.add_locked(record, identities, floor)
        before = outcomes.ledger.path.read_bytes()
        identities, floor = outcomes.ledger.load_locked(NOW, outcomes.window)
        with pytest.raises(LearningStoreError, match="^OUTCOME_INGESTION_LEDGER_FULL_PRUNE_REQUIRED$"):
            outcomes.ledger.add_locked(chronological[5], identities, floor)
        assert outcomes.ledger.path.read_bytes() == before
    floor_line = '{"executed_at":"%s","outcome_id":null}\n' % chronological[6].executed_at
    outcomes.ledger.path.write_text(floor_line, encoding="utf-8")
    with pytest.raises(LearningStoreError, match="OUTCOME_BELOW_INGESTION_FLOOR"):
        outcomes.append(chronological[0])


# --- OL-01: task type, classification, execution time and features are authenticated ---------------

def test_cr11b_ol01_task_classification_and_features_come_from_the_trusted_plan(tmp_path: Path) -> None:
    auth, clock = TrustedReceipts(), Clock(NOW)
    outcomes = store(tmp_path, auth, clock)
    genuine = outcome(1, auth=auth)
    assert genuine.features_authenticated and genuine.task_ref == task_ref_for("DOCUMENT_EXTRACTION")
    forgeries = [
        {"task_ref": task_ref_for("OTHER_TASK")},
        {"classification": "PUBLIC"},
        {"context_features": (("scan_type", "PHOTO"),)},
        {"features_authenticated": False},
    ]
    for change in forgeries:
        with pytest.raises(LearningStoreError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
            outcomes.append(OutcomeRecord.model_validate({**genuine.model_dump(), **change}))
    outcomes.append(genuine)
    plan = auth.resolve_receipt(genuine.receipt_hash)
    assert plan is not None
    auth.register(plan, classification=DataClassification.RESTRICTED, features=SCANNED_ZH)  # trusted side changed
    assert outcomes.read_checked().unauthenticated == 1
    auth.contexts.clear()  # no trusted plan context: nothing authenticates
    assert outcomes.read_checked().records == ()
    stage = stage_receipt(plan_id="p-ctx")
    trusted = auth.register(plan_receipt(stage, plan_id="p-ctx"), features=SCANNED_ZH)
    with pytest.raises(LearningRecordError, match="CONTEXT_FEATURES_CONFLICT_WITH_TRUSTED_PLAN"):
        OutcomeRecord.from_receipts(trusted, trusted.stages[0], authenticator=auth,
                                    hardware_class=HardwareClass.CPU, network_state=ConnectivityState.OFFLINE,
                                    context_features={"scan_type": "PHOTO"}, recorded_at=NOW)


def test_cr11b_ol01_unauthenticated_features_never_steer_a_bucket() -> None:
    unauthenticated = [*history("ocr.engine-a", 38, 2, trusted_features=False),
                       *history("ocr.engine-b", 25, 15, offset=100, trusted_features=False)]
    assert not any(record.features_authenticated for record in unauthenticated)
    assert all(record.context_features for record in unauthenticated)  # kept, but marked unauthenticated
    target = advisor(source=ListSource(unauthenticated))
    assert target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                         plan_binding_hash=BINDING) == ()
    pooled = advisor(source=ListSource(unauthenticated), bucket_keys=())
    assert len(pooled.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                             plan_binding_hash=BINDING)) == 2


# --- OL-02: binding mandatory, stable entry identity, test-only ephemeral logs ---------------------

def test_cr11b_ol02_plan_binding_is_mandatory_when_enabled() -> None:
    target = advisor()
    assert target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF) == ()
    assert target.log is not None and target.log.entries()[-1].status is AdviceStatus.BINDING_INCOMPLETE
    pairs = tuple(sorted((d.adapter_id, d.version) for d in (A, B)))
    assert target.context_hash(**TASK, context_features=SCANNED_ZH, candidates=pairs,
                               classification=CONF.value) == UNBOUND_CONTEXT_HASH


def test_cr11b_ol02_stable_entry_identity_without_mutable_state(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    target = advisor(log)
    first = target.advise_logged(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                                 plan_binding_hash=sha("plan-1"))
    target.advise_logged(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                         plan_binding_hash=sha("plan-2"))
    assert not hasattr(target, "last_entry")
    (linked,) = log.find(sha("plan-1"))
    assert (linked.seq, linked.entry_hash) == (first.entry_seq, first.entry_hash) and first.advice
    assert log.verify(checkpoint=Checkpoint(linked.seq, linked.entry_hash)).ok


def test_cr11b_ol02_ephemeral_logs_need_the_test_only_acknowledgement() -> None:
    with pytest.raises(ValueError, match="ADVICE_LOG_EPHEMERAL_IS_TEST_ONLY"):
        AdviceLog.ephemeral_for_tests(acknowledgement="production")
    log = AdviceLog.ephemeral_for_tests(acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT)
    assert log.for_tests and not log.durable


def test_cr11b_ol02_no_production_module_reaches_the_test_only_log_path() -> None:
    hits: list[tuple[str, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            name = (node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name)
                    else None)
            imported = ([alias.name for alias in node.names] if isinstance(node, ast.ImportFrom) else [])
            touched = {name, *imported} & {"ephemeral_for_tests", "TEST_ONLY_ACKNOWLEDGEMENT"}
            if touched:
                hits.append((path.relative_to(SRC).as_posix(), getattr(node, "lineno", 0)))
    # Only the definition module mentions them (the classmethod's own guard compares the acknowledgement).
    assert {file for file, _ in hits} <= {"learning/advice_log.py"}, hits
    definition = ast.parse((SRC / "learning" / "advice_log.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(definition) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "ephemeral_for_tests"]
    assert calls == []


# --- OL-04: identities in other persistence paths are digests ----------------------------------------

def test_cr11b_ol04_evaluation_and_advice_log_persist_identity_digests_only(tmp_path: Path) -> None:
    record = evaluation()
    assert {"capability_id", "adapter_id", "adapter_version"}.isdisjoint(EVALUATION_KEYS)
    assert record.adapter_ref == adapter_ref_for("ocr.engine-a", "1")
    assert record.adapter_ref != evaluation(adapter_version="2").adapter_ref  # versions stay distinct
    entry = _entry(advisor())
    assert all(isinstance(c, LoggedCandidate) and c.adapter_ref.startswith("adp-") for c in entry.candidates)
    raw = json.dumps(entry.model_dump(mode="json"))
    assert "ocr.engine" not in raw and "DOCUMENT_OCR" not in raw and "DOCUMENT_EXTRACTION" not in raw
    with pytest.raises(ValueError):
        AdviceRecordBody.model_validate({**{k: v for k, v in entry.model_dump(mode="json").items()
                                            if k not in {"seq", "prev_hash", "entry_hash"}},
                                         "candidates": [{"adapter_ref": "ocr.engine-a"}]})


def test_cr11b_ol10_execution_time_is_the_trusted_receipt_finish_time() -> None:
    record = outcome(1, verification=VerificationStatus.NOT_VERIFIED)
    assert record.executed_at == (NOW - timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="OUTCOME_RECORDED_BEFORE_EXECUTION"):
        OutcomeRecord.model_validate({**record.model_dump(),
                                      "recorded_at": (NOW - timedelta(hours=1)).isoformat()})
