"""Codex CR-11c (Outcome Learning) regression tests: OL-11, OL-12 and the OL-02/03/04/08/09 partials."""
from __future__ import annotations

import ast
import multiprocessing
import os
import pickle
import shutil
import stat
import sys
from datetime import timedelta
from itertools import combinations_with_replacement, product
from pathlib import Path

import pytest

from pados.capability.planner import plan_binding_hash
from pados.capability.seams import apply_advice
from pados.capability.task import TaskContract
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.learning.advice_log import (
    GENESIS,
    AdviceLog,
    AdviceLogEntry,
    AdviceRecordBody,
    AnchorStatus,
    Checkpoint,
    DerivationSnapshot,
    EntryKind,
    LinkRef,
    LoggedAdvice,
    RemovedRange,
)
from pados.learning.advisor import (
    AdviceResult,
    AdvisorConfig,
    OutcomeAdvisor,
    VerificationLevel,
    score_from_lower_bound,
)
from pados.learning.anchor import AnchorError, FileAnchor
from pados.learning.profiles import wilson_lower_bound
from pados.learning.records import (
    IdentityDigests,
    IdentityKey,
    OutcomeRecord,
    adapter_ref_for,
    capability_ref_for,
    decision_ref_for,
    digest_mode,
    evaluator_ref_for,
    resource_ref_for,
    task_ref_for,
)
from pados.learning.store import LearningStoreError, OutcomeStore
from tests.learning.learning_support import (
    AUTH,
    BINDING,
    CONF,
    NOW,
    SCANNED_ZH,
    ListSource,
    adapter,
    file_anchor,
    file_log,
    history,
    memory_log,
    sha,
)
from tests.learning.test_cr11b_learning import Anchor as CasAnchor
from tests.learning.test_cr11b_learning import Clock, body, marker, write_chain

A, B = adapter("ocr.engine-a"), adapter("ocr.engine-b")
TASK = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR")
PAIRS = tuple(sorted((d.adapter_id, d.version) for d in (A, B)))
SRC = Path(__file__).resolve().parents[2] / "src" / "pados"
KEY_REF = "secretref://env/PADOS_LEARNING_IDENTITY_KEY"


class Anchor(CasAnchor):
    """The compare-and-set double, recording every call; ``refuse`` makes the write path fail."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Checkpoint | None, Checkpoint, bool]] = []
        self.refuse: Exception | None = None

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        self.calls.append((previous, new, marker))
        if self.refuse is not None:
            raise self.refuse
        super().record(previous, new, marker=marker)


def records(**kwargs: object) -> list[OutcomeRecord]:
    return [*history("ocr.engine-a", 38, 2, **kwargs), *history("ocr.engine-b", 25, 15, offset=100, **kwargs)]


def advisor(log: AdviceLog, *, source: list[OutcomeRecord] | None = None, clock: Clock | None = None,
            identity_key: IdentityKey | None = None, **config: object) -> OutcomeAdvisor:
    return OutcomeAdvisor(ListSource(records() if source is None else source),
                          config=AdvisorConfig(enabled=True, **config),  # type: ignore[arg-type]
                          log=log, clock=clock or (lambda: NOW), identity_key=identity_key)


def advise(target: OutcomeAdvisor, binding: str = BINDING) -> AdviceResult:
    return target.advise_logged(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                                plan_binding_hash=binding)


def log_path(tmp_path: Path) -> Path:
    return tmp_path / "learning" / "advice.jsonl"


def summary(advice: object) -> list[tuple[object, ...]]:
    return [(a.adapter_id, a.score, a.sample_size, a.profile_hash) for a in advice]  # type: ignore[attr-defined]


def resealed(entries: int) -> list[AdviceLogEntry]:
    chain: list[AdviceLogEntry] = []
    for seq in range(1, entries + 1):
        chain.append(AdviceLogEntry.seal(body(NOW), seq=seq, prev_hash=chain[-1].entry_hash if chain else GENESIS))
    return chain


# --- OL-11: every mutation is validated against the external anchor; the anchor is compare-and-set -----

def test_cr11c_ol11_resealed_substitution_then_append_is_refused_and_anchor_keeps_trusted_checkpoint(
        tmp_path: Path) -> None:
    anchor = Anchor()
    log = file_log(tmp_path, anchor=anchor)
    target = advisor(log)
    for index in range(3):
        advise(target, sha(f"plan-{index}"))
    trusted, calls = anchor.latest(), len(anchor.calls)
    assert trusted is not None and trusted == log.checkpoint() and trusted.seq == 3
    original = log_path(tmp_path).read_text()
    write_chain(log_path(tmp_path), resealed(3))  # a writer substitutes a freshly sealed chain
    substituted = log_path(tmp_path).read_text()
    assert file_log(tmp_path, anchor=None).verify().ok  # internally consistent
    for mutation in (lambda: log.record(body(NOW)), log.prune, log.entries, lambda: log.find(BINDING)):
        with pytest.raises(LearningStoreError, match="ADVICE_LOG_REWRITTEN"):
            mutation()
    assert advise(target).audit_ref is None and advise(target).advice == ()  # nothing logged, nothing returned
    assert target.issued(sha("plan-0"), candidates=PAIRS) is None
    # The anchor was never asked to move: it still holds the trusted checkpoint; nothing was laundered.
    assert anchor.latest() == trusted and len(anchor.calls) == calls
    assert log_path(tmp_path).read_text() == substituted and log.verify().code == "ADVICE_LOG_REWRITTEN"
    # A validly chained, sealed suffix after the trusted head is not adopted either.
    log_path(tmp_path).write_text(original)
    suffix = AdviceLogEntry.seal(body(NOW), seq=4, prev_hash=trusted.entry_hash)
    write_chain(log_path(tmp_path), [*file_log(tmp_path, anchor=None).entries(include_markers=True), suffix])
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_HEAD_NOT_ANCHORED"):
        log.record(body(NOW))
    assert log.verify().code == "ADVICE_LOG_HEAD_NOT_ANCHORED" and anchor.latest() == trusted
    log_path(tmp_path).write_text(original)
    assert advise(target).advice and log.verify().ok and log.verify().anchor is AnchorStatus.ANCHORED


@pytest.mark.parametrize(("failure", "code"), [
    (RuntimeError("anchor write path unreachable"), "ADVICE_LOG_ANCHOR_UNAVAILABLE"),
    (AnchorError("ANCHOR_CONFLICT"), "ADVICE_LOG_ANCHOR_REFUSED"),
])
def test_cr11c_ol11_refused_anchor_update_restores_the_log_and_keeps_the_checkpoint(
        tmp_path: Path, failure: Exception, code: str) -> None:
    clock = Clock(NOW)
    anchor = Anchor()
    log = file_log(tmp_path, anchor=anchor, clock=clock, max_entries=3)
    target = advisor(log, clock=clock)
    for index in range(3):
        advise(target, sha(f"p{index}"))
    before, trusted = log_path(tmp_path).read_text(), anchor.latest()
    anchor.refuse = failure
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_FULL_PRUNE_REQUIRED"):
        log.record(body(NOW))  # a full log never deletes on its own (CR-11d OL-14)
    with pytest.raises(LearningStoreError, match=code):
        log.prune()  # the operator's capacity purge is anchored: refused, restored
    clock.now = NOW + timedelta(days=15)
    with pytest.raises(LearningStoreError, match=code):
        log.prune()  # ...and so is the expiry purge
    assert log_path(tmp_path).read_text() == before and anchor.latest() == trusted
    assert advise(target, sha("p9")).audit_ref is None
    anchor.refuse = None
    assert log.prune() == 3 and log.verify().ok and anchor.latest() == log.checkpoint()


def test_cr11c_ol11_file_anchor_is_compare_and_set_and_refuses_non_continuing_updates(tmp_path: Path) -> None:
    anchor = FileAnchor(tmp_path / "anchor")
    assert anchor.latest() is None and anchor.markers() == ()
    c1, c2, c3 = (Checkpoint(seq, sha(f"head-{seq}")) for seq in (1, 2, 3))
    with pytest.raises(AnchorError, match="ANCHOR_NOT_CONTINUING"):
        anchor.record(None, c2)  # the first checkpoint is seq 1
    anchor.record(None, c1)
    refused = [((None, c2), "ANCHOR_CONFLICT"),  # previous must equal the latest
               ((Checkpoint(1, sha("other")), c2), "ANCHOR_CONFLICT"),
               ((c1, Checkpoint(1, sha("fork"))), "ANCHOR_NOT_CONTINUING"),  # same seq: a replacement
               ((c1, c3), "ANCHOR_NOT_CONTINUING")]  # a jump
    for (previous, new), code in refused:
        with pytest.raises(AnchorError, match=code):
            anchor.record(previous, new)
        assert anchor.latest() == c1
    anchor.record(c1, c2, marker=True)
    anchor.record(c2, c3)
    assert anchor.latest() == c3 and anchor.markers() == (c2,)
    with pytest.raises(AnchorError, match="ANCHOR_CONFLICT"):
        anchor.record(c1, Checkpoint(2, sha("stale-writer")))  # a stale writer can never regress the head
    reopened = FileAnchor(tmp_path / "anchor")
    assert reopened.latest() == c3 and reopened.markers() == (c2,)
    with pytest.raises(ValueError, match="CHECKPOINT_INVALID"):
        Checkpoint(0, sha("x"))
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(tmp_path / "anchor").st_mode) == 0o700
        for name in ("anchor.head.json", "anchor.initialized"):
            assert stat.S_IMODE(os.stat(tmp_path / "anchor" / name).st_mode) == 0o600
    bounded = FileAnchor(tmp_path / "bounded", max_markers=2)
    previous: Checkpoint | None = None
    for seq in range(1, 5):
        point = Checkpoint(seq, sha(f"m{seq}"))
        bounded.record(previous, point, marker=True)
        previous = point
    assert [point.seq for point in bounded.markers()] == [3, 4]


def test_cr11c_ol11_anchor_head_loss_after_initialization_fails_closed(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    target = advisor(log)
    advise(target)
    directory = tmp_path / "anchor"
    (directory / "anchor.head.json").unlink()
    with pytest.raises(AnchorError, match="ANCHOR_HEAD_LOST"):
        file_anchor(tmp_path).latest()
    assert log.verify().code == "ADVICE_LOG_ANCHOR_UNAVAILABLE"
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_ANCHOR_UNAVAILABLE"):
        log.record(body(NOW))
    assert target.issued(BINDING, candidates=PAIRS) is None
    shutil.rmtree(directory)  # the whole anchor gone: a non-empty log is never re-anchored silently
    assert log.verify().code == "ADVICE_LOG_NOT_ANCHORED"
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_NOT_ANCHORED"):
        log.record(body(NOW))
    assert file_anchor(tmp_path).latest() is None  # the refused write never initialized a new anchor
    corrupt = FileAnchor(tmp_path / "corrupt")
    corrupt.record(None, Checkpoint(1, sha("one")))
    (tmp_path / "corrupt" / "anchor.head.json").write_text('{"latest": 7}')
    with pytest.raises(AnchorError, match="ANCHOR_STATE_INVALID"):
        corrupt.latest()
    if sys.platform != "win32":
        loose = FileAnchor(tmp_path / "loose")
        loose.record(None, Checkpoint(1, sha("one")))
        os.chmod(tmp_path / "loose" / "anchor.head.json", 0o644)
        with pytest.raises(AnchorError, match="ANCHOR_STATE_INSECURE"):
            loose.latest()


def _cas_worker(directory: str, rounds: int, tag: str) -> None:
    anchor = FileAnchor(Path(directory))
    done, mine = 0, 0
    while done < rounds:
        current = anchor.latest()
        point = Checkpoint(1 if current is None else current.seq + 1, sha(f"{tag}-{done}"))
        try:
            anchor.record(current, point)
        except AnchorError as exc:
            assert exc.code == "ANCHOR_CONFLICT"  # somebody else advanced it: re-read and retry
            continue
        latest = anchor.latest()
        assert latest is not None and latest.seq >= point.seq > mine  # never regresses
        done, mine = done + 1, point.seq


def test_cr11c_ol11_concurrent_recorders_never_regress(tmp_path: Path) -> None:
    directory = tmp_path / "anchor"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_cas_worker, args=(str(directory), 15, f"w{index}")) for index in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
        assert worker.exitcode == 0
    latest = FileAnchor(directory).latest()
    assert latest is not None and latest.seq == 45  # every successful CAS advanced by exactly one


# --- OL-08: pruning transitions are authenticated through the anchor ----------------------------------

def test_cr11c_ol08_fabricated_marker_ending_at_a_known_checkpoint_is_refused(tmp_path: Path) -> None:
    anchor = file_anchor(tmp_path)
    log = file_log(tmp_path)
    target = advisor(log)
    for index in range(4):
        advise(target, sha(f"plan-{index}"))
    known = anchor.latest()  # the writer knows the anchored checkpoint...
    assert known is not None and known.seq == 4 and anchor.markers() == ()
    prior = LinkRef(seq=known.seq, entry_hash=known.entry_hash)
    removed = (RemovedRange(from_seq=1, through_seq=4, before_hash=GENESIS, through_hash=known.entry_hash),)
    forged = marker(prior, removed)  # ...and fabricates a marker whose removed range ends exactly there
    write_chain(log_path(tmp_path), [forged, AdviceLogEntry.seal(body(NOW), seq=6, prev_hash=forged.entry_hash)])
    assert file_log(tmp_path, anchor=None).verify().ok  # internally consistent
    assert forged.pruned is not None and forged.pruned.commitments()[4] == known.entry_hash
    for report in (log.verify(checkpoint=known), log.verify()):
        assert not report.ok and report.code == "ADVICE_LOG_MARKER_UNANCHORED"
    with pytest.raises(LearningStoreError, match="ADVICE_LOG_MARKER_UNANCHORED"):
        log.record(body(NOW))
    assert anchor.latest() == known and target.issued(sha("plan-3"), candidates=PAIRS) is None


def test_cr11c_ol08_every_prune_marker_is_anchored_by_cas_from_the_pre_prune_head(tmp_path: Path) -> None:
    clock = Clock(NOW)
    anchor = Anchor()
    log = file_log(tmp_path, anchor=anchor, max_entries=4, clock=clock)
    target = advisor(log, clock=clock)
    for index in range(9):
        log.prune()  # the operator's purge before each advice (CR-11d OL-14)
        advise(target, sha(f"plan-{index}"))
    pruned = [(previous, new) for previous, new, is_marker in anchor.calls if is_marker]
    assert pruned and all(previous is not None and new.seq == previous.seq + 1 for previous, new in pruned)
    assert anchor.marker_points == [new for _, new in pruned]
    latest_marker = [entry for entry in log.entries(include_markers=True) if entry.kind is EntryKind.PRUNED][-1]
    assert latest_marker.pruned is not None
    head_before = next(previous for previous, new in pruned if new.seq == latest_marker.seq)
    assert head_before == Checkpoint(latest_marker.pruned.prior_head.seq, latest_marker.pruned.prior_head.entry_hash)
    early = pruned[0][0]  # the anchored head before the first prune
    assert early is not None and log.verify(checkpoint=early).anchor is AnchorStatus.ANCHORED_THROUGH_MARKER
    head = anchor.latest()
    clock.now = NOW + timedelta(days=15)
    assert log.entries() == () and anchor.latest() == head  # a read hides and never deletes or anchors
    assert log.prune() > 0  # the operator's purge is an anchored transition
    assert anchor.calls[-1][0] == head and anchor.calls[-1][2] is True
    assert log.verify().ok and log.verify(checkpoint=head).anchor is AnchorStatus.ANCHORED_THROUGH_MARKER


# --- OL-12 / OL-02: issuance requires anchored provenance ------------------------------------------------

def test_cr11c_ol12_enabled_advice_requires_an_external_anchor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ADVICE_ANCHOR_REQUIRED"):
        OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True), log=file_log(tmp_path, anchor=None))
    OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True), log=file_log(tmp_path))
    assert memory_log().anchor is not None  # the acknowledged test-only path carries its own anchor
    disabled = OutcomeAdvisor(ListSource(records()), log=file_log(tmp_path, anchor=None))
    assert disabled.issued(BINDING, candidates=PAIRS) is None


def test_cr11c_ol12_issued_requires_anchored_provenance(tmp_path: Path) -> None:
    log = file_log(tmp_path)
    target = advisor(log)
    first = advise(target)
    assert first.advice and first.audit_ref == (first.entry_seq, first.entry_hash)
    assert summary(target.issued(BINDING, candidates=PAIRS)) == summary(first.advice)
    original = log_path(tmp_path).read_text()
    (entry,) = log.find(BINDING)
    fields = {k: v for k, v in entry.model_dump(mode="json").items() if k not in {"seq", "prev_hash", "entry_hash"}}
    # A resealed replacement entry (a subset of the advice: arithmetically valid, NOT what was issued).
    replacement = AdviceLogEntry.seal(AdviceRecordBody.model_validate({**fields, "advice": fields["advice"][:1]}),
                                      seq=entry.seq, prev_hash=entry.prev_hash)
    assert target.verify(replacement).level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED
    write_chain(log_path(tmp_path), [replacement])
    assert file_log(tmp_path, anchor=None).verify().ok
    assert target.issued(BINDING, candidates=PAIRS) is None  # rewritten
    log_path(tmp_path).write_text("")
    assert target.issued(BINDING, candidates=PAIRS) is None  # truncated
    log_path(tmp_path).write_text(original)
    newer = AdviceLogEntry.seal(AdviceRecordBody.model_validate(fields), seq=2, prev_hash=entry.entry_hash)
    write_chain(log_path(tmp_path), [entry, newer])
    assert target.issued(BINDING, candidates=PAIRS) is None  # an unanchored "latest" entry
    log_path(tmp_path).write_text(original)
    assert summary(target.issued(BINDING, candidates=PAIRS)) == summary(first.advice)
    # Unavailable anchor -> nothing issued.
    flaky = Anchor()
    other = advisor(file_log(tmp_path / "other", anchor=flaky))
    advise(other)
    assert other.issued(BINDING, candidates=PAIRS) is not None
    flaky.fail = True
    assert other.issued(BINDING, candidates=PAIRS) is None


def test_cr11c_ol12_retrieval_is_bound_to_the_recorded_audit_entry() -> None:
    log = memory_log()
    target = advisor(log)
    first = advise(target)
    second = advise(advisor(log, source=records()[:30]))  # later, fewer outcomes: different advice, same binding
    assert first.audit_ref is not None and second.audit_ref is not None
    assert summary(first.advice) != summary(second.advice)
    assert summary(target.issued(BINDING, candidates=PAIRS)) == summary(second.advice)  # latest by default
    assert summary(target.issued(BINDING, candidates=PAIRS, audit_ref=first.audit_ref)) == summary(first.advice)
    assert target.issued(BINDING, candidates=PAIRS, audit_ref=(first.audit_ref[0], sha("other"))) is None
    assert target.issued(sha("another-plan"), candidates=PAIRS, audit_ref=first.audit_ref) is None


# --- OL-03: validity period and what the plan binding covers -------------------------------------------

def test_cr11c_ol03_issued_advice_expires_after_the_ttl() -> None:
    assert AdvisorConfig().advice_ttl == timedelta(hours=24)
    for invalid in (timedelta(days=8), timedelta(seconds=10)):
        with pytest.raises(ValueError, match="ADVICE_TTL_OUT_OF_RANGE"):
            AdvisorConfig(advice_ttl=invalid)
    clock = Clock(NOW)
    log = memory_log(clock=clock)
    target = advisor(log, clock=clock)
    advise(target)
    clock.now = NOW + timedelta(hours=23)
    assert target.issued(BINDING, candidates=PAIRS)
    clock.now = NOW + timedelta(hours=25)
    assert target.issued(BINDING, candidates=PAIRS) is None
    assert advisor(log, clock=clock, advice_ttl=timedelta(days=7)).issued(BINDING, candidates=PAIRS)


def test_cr11c_ol03_plan_binding_covers_project_and_work_contract() -> None:
    base = dict(task_id="task-1", task_type="doc.extract", classification=CONF, required_outputs=("text.chunks/v1",))
    one = TaskContract(project_id="proj-a", work_contract_id="wc-1", **base)  # type: ignore[arg-type]
    two = TaskContract(project_id="proj-b", work_contract_id="wc-1", **base)  # type: ignore[arg-type]
    three = TaskContract(project_id="proj-a", work_contract_id="wc-2", **base)  # type: ignore[arg-type]
    assert one.model_dump(exclude={"project_id"}) == two.model_dump(exclude={"project_id"})
    bindings = [plan_binding_hash(task, sha("template"), "DOCUMENT_OCR", PAIRS) for task in (one, two, three)]
    assert len(set(bindings)) == 3  # the fabric task hash binds project and work contract
    target = advisor(memory_log())
    advice = target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                           plan_binding_hash=bindings[0])
    other = target.context_hash(**TASK, context_features=SCANNED_ZH, candidates=PAIRS, classification=CONF.value,
                                plan_binding_hash=bindings[1])
    assert advice and apply_advice(["ocr.engine-a", "ocr.engine-b"], advice, context_hash=other) == {}
    assert target.issued(bindings[1], candidates=PAIRS) is None


# --- OL-09: counters_consistent accepts exactly the realizable combinations -------------------------------

_KINDS = [(success, verification, human) for success in (True, False) for verification in ("PASSED", "FAILED", "NONE")
          for human in ("ACCEPTED", "REJECTED", "NONE") if success or verification != "PASSED"]


def _counters(kinds: tuple[tuple[bool, str, str], ...]) -> tuple[int, ...]:
    good = sum(s and v != "FAILED" and h != "REJECTED" for s, v, h in kinds)
    return (len(kinds), sum(s for s, _, _ in kinds), good, sum(v != "NONE" for _, v, _ in kinds),
            sum(v == "PASSED" for _, v, _ in kinds), sum(h != "NONE" for _, _, h in kinds),
            sum(h == "ACCEPTED" for _, _, h in kinds))


def _snapshot(n: int, successes: int, good: int, verified: int, passed: int, decided: int,
              accepted: int) -> DerivationSnapshot:
    return DerivationSnapshot.model_construct(
        adapter_ref=adapter_ref_for("ocr.engine-a", "1"), sample_size=n, successes=successes, good_outcomes=good,
        verified=verified, verification_passed=passed, human_decided=decided, human_accepted=accepted,
        good_wilson_lower=0.0, min_samples=1, profile_hash=sha("p"))


def test_cr11c_ol09_counters_consistent_is_exactly_realizability() -> None:
    for n in range(5):
        realizable = {_counters(kinds) for kinds in combinations_with_replacement(_KINDS, n)}
        for values in product(range(n + 1), repeat=6):
            assert _snapshot(n, *values).counters_consistent() == ((n, *values) in realizable), (n, values)


@pytest.mark.parametrize("counters", [
    (20, 20, 20, 20, 0, 0, 0),  # reviewer: every outcome failed verification, yet all "good"
    (20, 20, 20, 0, 0, 20, 0),  # every outcome human-REJECTED, yet all "good"
    (20, 10, 0, 20, 10, 0, 0),  # 10 passed verification and nobody rejected: at least 10 good
    (20, 15, 16, 0, 0, 0, 0),  # more good than successes
    (20, 5, 5, 20, 6, 0, 0),  # a failed outcome passed verification
])
def test_cr11c_ol09_impossible_counters_are_rejected_even_with_matching_arithmetic(
        counters: tuple[int, ...]) -> None:
    n, successes, good, verified, passed, decided, accepted = counters
    assert not _snapshot(*counters).counters_consistent()
    target = advisor(memory_log())
    result = advise(target)
    assert target.log is not None
    (entry,) = [e for e in target.log.find(BINDING) if e.entry_hash == result.entry_hash]
    lower = round(wilson_lower_bound(good, n), 6)
    snap = DerivationSnapshot(adapter_ref=entry.advice[0].adapter_ref, sample_size=n, successes=successes,
                              good_outcomes=good, verified=verified, verification_passed=passed,
                              human_decided=decided, human_accepted=accepted, good_wilson_lower=lower,
                              min_samples=20, profile_hash=sha("forged"))
    item = LoggedAdvice(adapter_ref=snap.adapter_ref, score=score_from_lower_bound(lower), snapshot=snap,
                        snapshot_hash=snap.snapshot_hash)
    forged = entry.model_copy(update={"advice": (item, *entry.advice[1:])})
    verdict = target.verify(forged, use_records=False)
    assert not verdict.ok and verdict.code == "ADVICE_COUNTERS_INCONSISTENT"


def test_cr11c_ol09_real_profiles_stay_consistent() -> None:
    target = advisor(memory_log())
    result = advise(target)
    assert target.log is not None
    (entry,) = [e for e in target.log.find(BINDING) if e.entry_hash == result.entry_hash]
    assert entry.advice and all(item.snapshot.counters_consistent() for item in entry.advice)
    assert target.verify(entry).level is VerificationLevel.DERIVATION_VERIFIED


# --- OL-04: optional keyed identity digests ---------------------------------------------------------------

def _key(fill: str = "k") -> IdentityKey:
    material = fill * 24 + "-pados-learning-id"  # built at runtime; an operator key would come from a vault
    boundary = SecretBoundary(EnvironmentSecretResolver({"PADOS_LEARNING_IDENTITY_KEY": material}))
    return IdentityKey.from_secret_ref(boundary, KEY_REF)


def test_cr11c_ol04_keyed_identity_digests_keep_prefixes_and_differ() -> None:
    key = _key()
    helpers = [("task", lambda k: task_ref_for("DOCUMENT_EXTRACTION", k)),
               ("res", lambda k: resource_ref_for("node-1", k)), ("dec", lambda k: decision_ref_for("appr-1", k)),
               ("evr", lambda k: evaluator_ref_for("bench-1", k)), ("cap", lambda k: capability_ref_for("DOCUMENT_OCR", k)),
               ("adp", lambda k: adapter_ref_for("ocr.engine-a", "1", k))]
    for prefix, helper in helpers:
        unkeyed, keyed, other = helper(None), helper(key), helper(_key("q"))
        assert unkeyed.startswith(f"{prefix}-") and keyed.startswith(f"{prefix}-k1-")
        assert len({unkeyed, keyed, other}) == 3 and len(keyed) == len(unkeyed) + 3
        assert digest_mode(unkeyed) is IdentityDigests.UNKEYED and digest_mode(keyed) is IdentityDigests.KEYED
    with pytest.raises(ValueError, match="IDENTITY_KEY_TOO_SHORT"):
        IdentityKey(b"short")
    with pytest.raises(PermissionError, match="SECRET_REFERENCE_UNAVAILABLE"):
        IdentityKey.from_secret_ref(SecretBoundary(EnvironmentSecretResolver({})), KEY_REF)
    assert "[REDACTED]" in repr(key) and "kkkk" not in repr(key) + str(key)
    with pytest.raises(TypeError, match="IDENTITY_KEY_NOT_SERIALIZABLE"):
        pickle.dumps(key)


def test_cr11c_ol04_dictionary_attack_works_on_unkeyed_refs_only() -> None:
    hidden = "MERGER_TARGET_SCREENING"
    dictionary = ["DOCUMENT_EXTRACTION", "INVOICE_PARSING", "CONTRACT_REVIEW", hidden,
                  *(f"TASK_{index:03d}" for index in range(300))]
    unkeyed = task_ref_for(hidden)
    assert [word for word in dictionary if task_ref_for(word) == unkeyed] == [hidden]  # guessable
    key, wrong = _key(), _key("w")
    keyed = task_ref_for(hidden, key)
    assert not [word for word in dictionary if keyed in {task_ref_for(word), task_ref_for(word, wrong)}]
    assert [word for word in dictionary if task_ref_for(word, key) == keyed] == [hidden]  # only with the key


def test_cr11c_ol04_keyed_end_to_end_and_the_key_is_never_persisted(tmp_path: Path) -> None:
    key = _key()
    keyed_records = records(identity_key=key)
    store = OutcomeStore(tmp_path / "learning" / "outcomes.jsonl", authenticator=AUTH, clock=lambda: NOW,
                         identity_key=key)
    for record in keyed_records:
        store.append(record)
    unkeyed_record = history("ocr.engine-a", 1, 0, offset=500)[0]
    with pytest.raises(LearningStoreError, match="LEARNING_OUTCOME_UNAUTHENTICATED"):
        store.append(unkeyed_record)  # identities must be digested under the store's key
    target = OutcomeAdvisor(store, config=AdvisorConfig(enabled=True), log=file_log(tmp_path), clock=lambda: NOW,
                            identity_key=key)
    result = advise(target)
    assert len(result.advice) == 2 and all(a.explanation.endswith("IDENTITY_DIGESTS_KEYED") for a in result.advice)
    assert target.log is not None
    (entry,) = target.log.find(BINDING)
    assert entry.task_ref is not None and entry.task_ref.startswith("task-k1-")
    verdict = target.verify(entry)
    assert verdict.level is VerificationLevel.DERIVATION_VERIFIED
    assert verdict.identity_digests is IdentityDigests.KEYED
    assert summary(target.issued(BINDING, candidates=PAIRS)) == summary(result.advice)
    unkeyed = OutcomeAdvisor(store, config=AdvisorConfig(enabled=True), log=file_log(tmp_path / "u"),
                             clock=lambda: NOW)
    assert advise(unkeyed, sha("u")).advice == ()  # a different digest space never matches (fail safe)
    material = ("k" * 24 + "-pados-learning-id").encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert material not in raw and material.hex().encode() not in raw, path
    mixed = entry.model_copy(update={"task_ref": task_ref_for("DOCUMENT_EXTRACTION")})
    assert target.verify(mixed).code == "ADVICE_IDENTITY_DIGESTS_MIXED"


def test_cr11c_ol04_unkeyed_digests_are_reported() -> None:
    target = advisor(memory_log())
    result = advise(target)
    assert result.advice and all(a.explanation.endswith("IDENTITY_DIGESTS_UNKEYED") for a in result.advice)
    assert target.identity_digests is IdentityDigests.UNKEYED
    assert target.log is not None
    (entry,) = target.log.find(BINDING)
    assert target.verify(entry).identity_digests is IdentityDigests.UNKEYED
    issued = target.issued(BINDING, candidates=PAIRS)
    assert issued and all(a.explanation.endswith("IDENTITY_DIGESTS_UNKEYED") for a in issued)


def test_cr11c_test_memory_anchor_is_reachable_only_from_the_test_only_path() -> None:
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Name) and node.id == "_TestMemoryAnchor":
                hits.append(path.relative_to(SRC).as_posix())
    assert set(hits) == {"learning/advice_log.py"} and len(hits) == 1  # used once, in ephemeral_for_tests

