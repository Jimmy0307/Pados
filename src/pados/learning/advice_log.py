# SPDX-License-Identifier: AGPL-3.0-only
"""Hash-chained, sequenced advice log: the observable record of every advice set (operational evidence).

Each entry carries ``seq``, ``prev_hash`` and ``entry_hash`` (canonical hash of everything else), so any
edit, insertion, reordering or undocumented gap is detected by ``verify``. Each advice entry stores the
minimized advisor configuration inputs (version, ``min_samples``, bucket keys; their hash must equal
``config_hash``), digests of the candidate identities (``adp-``/``cap-``, never labels), the planner's plan
binding hash and a derivation snapshot per advised candidate, so the score can be re-derived from the log
alone after the outcome records are gone.

Pruning (R1 age window, entry cap, future-dated entries anywhere in the log) never deletes silently. It
rewrites the retained entries verbatim and appends a chained ``PRUNED`` marker recording:

* ``prior_head``: the chain head before the prune (the marker's ``prev_hash`` is that head's hash);
* ``ranges``: EVERY removed run between retained entries (and before the first / after the last), each with
  the hash before the run and the hash of the last entry it removed, so the retained chain is still one
  hash-linked chain from genesis to the head;
* ``ancestry``: hashes committed by earlier markers (bounded, newest first kept), carried forward so a
  checkpoint anchored before an earlier prune stays verifiable.

Only the latest marker's ranges may bridge gaps; a range that bridges nothing is a forgery
(``ADVICE_LOG_PRUNE_RANGE_INVALID``). ``verify(checkpoint)`` accepts an anchored ``(seq, entry_hash)`` only when
the current chain proves it: the entry is retained with that hash (``ANCHORED``), or the latest chained
marker commits to exactly that hash (``ANCHORED_THROUGH_MARKER``). Otherwise the result is the explicit
``ADVICE_LOG_ANCHOR_UNVERIFIABLE``; a claimed removed range never substitutes for the hash.

Reads (``entries``, ``find``), writes (``record``) and the operator-invoked ``prune()`` physically delete
expired and future-dated entries under the lock; there is no timer (an idle log is pruned on its next access
or by the operator). ``verify`` and ``checkpoint`` never modify the log.

Durability: an enabled advisor requires a file-backed log (append-only JSONL, owner-only, cross-process
lock, fail closed on corruption or a broken chain). The in-memory variant is reachable only through
``AdviceLog.ephemeral_for_tests(acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT)``; a static test proves no
production module references it.

External anchoring (``pados.learning.anchor``): with an ``ExternalAnchor`` (operator-supplied, outside the
writable store, e.g. the existing audit authority) EVERY mutation - append, prune, read-time physical pruning -
first validates the existing log against the anchor: the anchored latest checkpoint must be exactly the current
head and the chain must reach it (``ADVICE_LOG_NOT_ANCHORED`` / ``ADVICE_LOG_HEAD_NOT_ANCHORED`` /
``ADVICE_LOG_TRUNCATED`` / ``ADVICE_LOG_REWRITTEN`` otherwise). Only then is the change written and the anchor
advanced by compare-and-set from that validated head (``record(previous, new)``); a refused or unreachable
anchor restores the previous file content and fails closed, so the anchor keeps the trusted checkpoint and a
substituted chain is never laundered. Each mutation adds exactly one entry (a prune and an append are two
anchored transitions). Every ``PRUNED`` marker is anchored with ``marker=True`` at prune time, and
``ANCHORED_THROUGH_MARKER`` is accepted only for a marker the anchor actually recorded
(``ADVICE_LOG_MARKER_UNANCHORED`` otherwise, also when no anchor is configured). Without an anchor the chain
proves internal consistency only: a writer able to rewrite the whole file can reseal a fresh chain.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.seams import ADVICE_BOUND
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.learning.anchor import AnchorError, Checkpoint, ExternalAnchor, continues
from pados.learning.profiles import validate_bucket_keys
from pados.learning.records import (
    ADAPTER_REF_PATTERN,
    CAPABILITY_REF_PATTERN,
    ContextFeatures,
    validate_context_features,
)
from pados.learning.store import ADVICE_LOG_RETENTION, FUTURE_SKEW, JsonlFile, LearningStoreError, utc_now
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import RetentionPolicy

GENESIS = "0" * 64
MAX_CANDIDATES = 32
MAX_RANGES = 16
MAX_ANCESTRY = 32
MAX_COUNT = 10**9
DEFAULT_LOG_ENTRIES = 1000
TEST_ONLY_ACKNOWLEDGEMENT = "pados.learning.advice-log.ephemeral.test-only"
_LOG_LINE_BYTES = 64 * 1024
_SHA256 = r"^[0-9a-f]{64}$"
_TASK_REF = re.compile(r"^task-(?:k1-)?[0-9a-f]{16}$")
_ADVISOR_VERSION = re.compile(r"^pados\.learning\.advisor\.v[0-9]{1,3}$")


class EntryKind(StrEnum):
    ADVICE = "ADVICE"
    PRUNED = "PRUNED"


class AdviceStatus(StrEnum):
    ADVISED = "ADVISED"  # at least one candidate received advice
    NO_QUALIFYING_PROFILE = "NO_QUALIFYING_PROFILE"  # no candidate met min_samples
    BINDING_INCOMPLETE = "BINDING_INCOMPLETE"  # planner did not supply classification and plan binding hash
    CONTEXT_INVALID = "CONTEXT_INVALID"
    CANDIDATES_INVALID = "CANDIDATES_INVALID"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


COMPUTED_STATUSES = frozenset({AdviceStatus.ADVISED, AdviceStatus.NO_QUALIFYING_PROFILE})


class PruneReason(StrEnum):
    EXPIRED = "EXPIRED"
    CAPACITY = "CAPACITY"
    FUTURE_DATED = "FUTURE_DATED"


class AnchorStatus(StrEnum):
    UNANCHORED = "UNANCHORED"  # no checkpoint was supplied or anchored
    ANCHORED = "ANCHORED"  # the anchored entry is retained with the anchored hash
    ANCHORED_THROUGH_MARKER = "ANCHORED_THROUGH_MARKER"  # pruned; the latest chained marker commits to the hash


def config_hash_of(*, advisor_version: str, min_samples: int, bucket_keys: Sequence[str] | None) -> str:
    """Hash of the advisor configuration inputs that determine a score (persisted with every entry)."""
    return canonical_hash({"advisor_version": advisor_version, "advice_bound": ADVICE_BOUND,
                           "min_samples": min_samples,
                           "bucket_keys": None if bucket_keys is None else list(bucket_keys)})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LoggedCandidate(_Frozen):
    adapter_ref: str = Field(pattern=ADAPTER_REF_PATTERN)  # adapter id AND version, as a digest


class DerivationSnapshot(_Frozen):
    """Minimized inputs of one score (no content): enough to re-derive it without the outcome records."""

    adapter_ref: str = Field(pattern=ADAPTER_REF_PATTERN)
    sample_size: int = Field(ge=0, le=MAX_COUNT)
    successes: int = Field(ge=0, le=MAX_COUNT)
    good_outcomes: int = Field(ge=0, le=MAX_COUNT)
    verified: int = Field(ge=0, le=MAX_COUNT)
    verification_passed: int = Field(ge=0, le=MAX_COUNT)
    human_decided: int = Field(ge=0, le=MAX_COUNT)
    human_accepted: int = Field(ge=0, le=MAX_COUNT)
    good_wilson_lower: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    min_samples: int = Field(ge=1, le=MAX_COUNT)
    profile_hash: str = Field(pattern=_SHA256)

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def counters_consistent(self) -> bool:
        """Exactly the counter combinations some real set of ``sample_size`` outcomes can produce.

        "Good" = succeeded AND verification not FAILED AND human decision not REJECTED; a failed outcome can
        never pass verification. With ``F = verified - verification_passed`` (verification FAILED) and
        ``R = human_decided - human_accepted`` (REJECTED):

        * ``verification_passed <= verified <= n``, ``human_accepted <= human_decided <= n``,
          ``verification_passed <= successes <= n``;
        * upper bound: ``good <= successes``, ``good <= n - F`` and ``good <= n - R`` (at most ``n - successes``
          FAILED verifications / rejections can fall on failed outcomes, the rest remove successes);
        * lower bound: ``good >= successes - min(F, successes - verification_passed) - min(R, successes)``
          (only successes that did not pass verification can be verification-FAILED, and each rejection
          removes at most one success); this implies ``successes - good <= F + R`` and
          ``verification_passed - good <= R``.
        """
        n, s, good = self.sample_size, self.successes, self.good_outcomes
        failed_verification = self.verified - self.verification_passed
        rejected = self.human_decided - self.human_accepted
        if not (self.verification_passed <= self.verified <= n and self.human_accepted <= self.human_decided <= n
                and self.verification_passed <= s <= n):
            return False
        upper = min(s, n - failed_verification, n - rejected)
        lower = s - min(failed_verification, s - self.verification_passed) - min(rejected, s)
        return max(lower, 0) <= good <= upper


class LoggedAdvice(_Frozen):
    adapter_ref: str = Field(pattern=ADAPTER_REF_PATTERN)
    score: float = Field(ge=-ADVICE_BOUND, le=ADVICE_BOUND, allow_inf_nan=False)
    snapshot: DerivationSnapshot
    snapshot_hash: str = Field(pattern=_SHA256)


class LinkRef(_Frozen):
    seq: int = Field(ge=1)
    entry_hash: str = Field(pattern=_SHA256)


class RemovedRange(_Frozen):
    """One removed run ``from_seq..through_seq``: the hash before it and the hash of its last entry."""

    from_seq: int = Field(ge=1)
    through_seq: int = Field(ge=1)
    before_hash: str = Field(pattern=_SHA256)
    through_hash: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def ordered(self) -> RemovedRange:
        if self.through_seq < self.from_seq:
            raise ValueError("PRUNE_RANGE_INVALID")
        return self


class PruneMarker(_Frozen):
    prior_head: LinkRef
    ranges: tuple[RemovedRange, ...] = Field(min_length=1, max_length=MAX_RANGES)
    ancestry: tuple[LinkRef, ...] = Field(default=(), max_length=MAX_ANCESTRY)
    removed: int = Field(ge=1)
    reasons: tuple[PruneReason, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def consistent(self) -> PruneMarker:
        for low, high in pairwise(self.ranges):
            if low.through_seq + 1 >= high.from_seq:
                raise ValueError("PRUNE_RANGES_NOT_DISJOINT")
        if self.ranges[-1].through_seq > self.prior_head.seq:
            raise ValueError("PRUNE_RANGE_AFTER_PRIOR_HEAD")
        seqs = [link.seq for link in self.ancestry]
        if seqs != sorted(set(seqs)) or (seqs and seqs[-1] > self.prior_head.seq):
            raise ValueError("PRUNE_ANCESTRY_INVALID")
        return self

    def commitments(self) -> dict[int, str]:
        """``seq -> entry_hash`` this marker commits to (prior head, last entry of every removed run, ancestry)."""
        found = {link.seq: link.entry_hash for link in self.ancestry}
        found.update({item.through_seq: item.through_hash for item in self.ranges})
        found[self.prior_head.seq] = self.prior_head.entry_hash
        return found


class AdviceRecordBody(_Frozen):
    """What one enabled ``advise`` call saw and returned (metadata, digests and hashes only)."""

    kind: EntryKind = EntryKind.ADVICE
    logged_at: str
    advisor_version: str | None = None
    config_hash: str | None = Field(default=None, pattern=_SHA256)
    min_samples: int | None = Field(default=None, ge=1, le=MAX_COUNT)
    bucket_keys: tuple[str, ...] | None = None
    status: AdviceStatus | None = None
    task_ref: str | None = None
    capability_ref: str | None = Field(default=None, pattern=CAPABILITY_REF_PATTERN)
    classification: DataClassification | None = None
    plan_binding_hash: str | None = Field(default=None, pattern=_SHA256)
    context_features: ContextFeatures = ()
    context_hash: str | None = Field(default=None, pattern=_SHA256)
    candidates: tuple[LoggedCandidate, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    advice: tuple[LoggedAdvice, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    pruned: PruneMarker | None = None

    @field_validator("advisor_version")
    @classmethod
    def version_text(cls, value: str | None) -> str | None:
        if value is not None and not _ADVISOR_VERSION.fullmatch(value):
            raise ValueError("ADVICE_LOG_VERSION_INVALID")
        return value

    @field_validator("task_ref")
    @classmethod
    def task_digest(cls, value: str | None) -> str | None:
        if value is not None and not _TASK_REF.fullmatch(value):
            raise ValueError("ADVICE_LOG_TASK_REF_INVALID")
        return value

    @field_validator("bucket_keys")
    @classmethod
    def known_bucket_keys(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and validate_bucket_keys(value) != value:
            raise ValueError("ADVICE_LOG_BUCKET_KEYS_NOT_CANONICAL")
        return value

    @field_validator("context_features", mode="before")
    @classmethod
    def closed_features(cls, value: Any) -> ContextFeatures:
        return validate_context_features(value)

    @field_validator("logged_at")
    @classmethod
    def aware(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("ADVICE_LOG_TIMESTAMP_NOT_AWARE")
        return value

    @model_validator(mode="after")
    def kind_consistent(self) -> AdviceRecordBody:
        if self.kind is EntryKind.PRUNED:
            advice_fields = (self.advisor_version, self.config_hash, self.min_samples, self.bucket_keys,
                             self.status, self.task_ref, self.capability_ref, self.classification,
                             self.plan_binding_hash, self.context_hash)
            if (self.pruned is None or self.advice or self.candidates or self.context_features
                    or any(item is not None for item in advice_fields)):
                raise ValueError("PRUNED_MARKER_SHAPE_INVALID")
            return self
        if (self.pruned is not None or self.status is None or self.config_hash is None
                or self.advisor_version is None or self.min_samples is None):
            raise ValueError("ADVICE_ENTRY_SHAPE_INVALID")
        if self.config_hash != config_hash_of(advisor_version=self.advisor_version, min_samples=self.min_samples,
                                              bucket_keys=self.bucket_keys):
            raise ValueError("ADVICE_CONFIG_HASH_MISMATCH")
        for item in self.advice:
            if item.snapshot.snapshot_hash != item.snapshot_hash or item.adapter_ref != item.snapshot.adapter_ref:
                raise ValueError("ADVICE_SNAPSHOT_HASH_MISMATCH")
        return self


class AdviceLogEntry(AdviceRecordBody):
    seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=_SHA256)
    entry_hash: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def sealed(self) -> AdviceLogEntry:
        if self.entry_hash != _seal_hash(self.model_dump(mode="json")):
            raise ValueError("ADVICE_LOG_ENTRY_HASH_MISMATCH")
        if self.pruned is not None and (self.seq != self.pruned.prior_head.seq + 1
                                        or self.prev_hash != self.pruned.prior_head.entry_hash):
            raise ValueError("PRUNED_MARKER_NOT_CHAINED_TO_PRIOR_HEAD")
        return self

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.logged_at)

    @property
    def link(self) -> LinkRef:
        return LinkRef(seq=self.seq, entry_hash=self.entry_hash)

    @classmethod
    def seal(cls, body: AdviceRecordBody, *, seq: int, prev_hash: str) -> AdviceLogEntry:
        data = {**body.model_dump(mode="json"), "seq": seq, "prev_hash": prev_hash}
        return cls.model_validate({**data, "entry_hash": _seal_hash(data)})


def _seal_hash(data: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != "entry_hash"})


ADVICE_LOG_KEYS = frozenset(AdviceLogEntry.model_fields)


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    code: str
    entries: int
    first_seq: int | None
    last_seq: int | None
    future_dated: int
    anchor: AnchorStatus = AnchorStatus.UNANCHORED


def _minimized(entry: AdviceLogEntry) -> dict[str, Any]:
    record = entry.model_dump(mode="json")
    assert_minimized(record, allowed_keys=ADVICE_LOG_KEYS, max_string=128, max_items=64,
                     max_total_bytes=_LOG_LINE_BYTES - 1024)
    return record


def _latest_marker(entries: Sequence[AdviceLogEntry]) -> PruneMarker | None:
    return next((entry.pruned for entry in reversed(entries) if entry.pruned is not None), None)


def _continuity(entries: Sequence[AdviceLogEntry]) -> str | None:
    """One hash-linked chain from genesis to the head; every gap bridged by a range of the latest marker."""
    marker_seq = next((entry.seq for entry in reversed(entries) if entry.pruned is not None), 0)
    latest = _latest_marker(entries)
    ranges = {} if latest is None else {item.through_seq: item for item in latest.ranges}
    used: set[int] = set()
    first = entries[0]
    if not (first.seq == 1 and first.prev_hash == GENESIS):
        head = ranges.get(first.seq - 1)
        if head is None or head.from_seq != 1 or head.before_hash != GENESIS or head.through_hash != first.prev_hash:
            return "ADVICE_LOG_HEAD_UNDOCUMENTED"
        used.add(head.through_seq)
    for prev, entry in pairwise(entries):
        if entry.seq == prev.seq + 1 and entry.prev_hash == prev.entry_hash:
            continue
        bridge = ranges.get(entry.seq - 1)
        if (bridge is not None and entry.seq <= marker_seq and bridge.from_seq == prev.seq + 1
                and bridge.before_hash == prev.entry_hash and bridge.through_hash == entry.prev_hash):
            used.add(bridge.through_seq)
            continue
        return "ADVICE_LOG_CHAIN_BROKEN" if entry.prev_hash != prev.entry_hash else "ADVICE_LOG_SEQUENCE_GAP"
    if used != set(ranges):
        return "ADVICE_LOG_PRUNE_RANGE_INVALID"  # a claimed removal that bridges nothing
    if latest is not None:
        direct = {entry.seq: entry.entry_hash for entry in entries}
        if any(seq in direct and direct[seq] != digest for seq, digest in latest.commitments().items()):
            return "ADVICE_LOG_ANCESTRY_CONFLICT"
    return None


def _anchor(entries: Sequence[AdviceLogEntry], checkpoint: Checkpoint,
            anchored_markers: Collection[Checkpoint] | None) -> tuple[str | None, AnchorStatus]:
    if checkpoint.seq > entries[-1].seq:
        return "ADVICE_LOG_TRUNCATED", AnchorStatus.UNANCHORED
    direct = {entry.seq: entry.entry_hash for entry in entries}
    if checkpoint.seq in direct:
        if direct[checkpoint.seq] != checkpoint.entry_hash:
            return "ADVICE_LOG_REWRITTEN", AnchorStatus.UNANCHORED
        return None, AnchorStatus.ANCHORED
    marker = next((entry for entry in reversed(entries) if entry.pruned is not None), None)
    committed = {} if marker is None or marker.pruned is None else marker.pruned.commitments()
    if marker is None or checkpoint.seq not in committed:
        return "ADVICE_LOG_ANCHOR_UNVERIFIABLE", AnchorStatus.UNANCHORED
    if committed[checkpoint.seq] != checkpoint.entry_hash:
        return "ADVICE_LOG_REWRITTEN", AnchorStatus.UNANCHORED
    if anchored_markers is None or Checkpoint(marker.seq, marker.entry_hash) not in anchored_markers:
        return "ADVICE_LOG_MARKER_UNANCHORED", AnchorStatus.UNANCHORED  # a marker the anchor never recorded
    return None, AnchorStatus.ANCHORED_THROUGH_MARKER


def chain_report(entries: Sequence[AdviceLogEntry], *, now: datetime, checkpoint: Checkpoint | None = None,
                 anchored_markers: Collection[Checkpoint] | None = None) -> ChainReport:
    """Continuity (gaps only as bridged by the latest PRUNED marker), future dating and checkpoint ancestry.

    A checkpoint that is no longer retained is accepted only through the latest marker's commitments AND only
    when that marker is in ``anchored_markers`` (the markers the external anchor recorded at prune time)."""
    future = sum(entry.when > now + FUTURE_SKEW for entry in entries)

    def report(ok: bool, code: str, anchor: AnchorStatus = AnchorStatus.UNANCHORED) -> ChainReport:
        return ChainReport(ok, code, len(entries), entries[0].seq if entries else None,
                           entries[-1].seq if entries else None, future, anchor)

    if not entries:
        return report(False, "ADVICE_LOG_TRUNCATED") if checkpoint is not None else report(True, "OK")
    broken = _continuity(entries)
    if broken is not None:
        return report(False, broken)
    anchor = AnchorStatus.UNANCHORED
    if checkpoint is not None:
        failed, anchor = _anchor(entries, checkpoint, anchored_markers)
        if failed is not None:
            return report(False, failed)
    if future:
        return report(False, "ADVICE_LOG_FUTURE_DATED", anchor)
    return report(True, "OK", anchor)


def _gaps(retained: Sequence[AdviceLogEntry], prior: AdviceLogEntry) -> list[RemovedRange]:
    """Every removed run of the logical chain ``1..prior.seq`` given the entries that stay."""
    if not retained:
        return [RemovedRange(from_seq=1, through_seq=prior.seq, before_hash=GENESIS, through_hash=prior.entry_hash)]
    ranges: list[RemovedRange] = []
    first = retained[0]
    if not (first.seq == 1 and first.prev_hash == GENESIS):
        ranges.append(RemovedRange(from_seq=1, through_seq=first.seq - 1, before_hash=GENESIS,
                                   through_hash=first.prev_hash))
    for low, high in pairwise(retained):
        if high.seq != low.seq + 1:
            ranges.append(RemovedRange(from_seq=low.seq + 1, through_seq=high.seq - 1, before_hash=low.entry_hash,
                                       through_hash=high.prev_hash))
    last = retained[-1]
    if last.seq != prior.seq:
        ranges.append(RemovedRange(from_seq=last.seq + 1, through_seq=prior.seq, before_hash=last.entry_hash,
                                   through_hash=prior.entry_hash))
    return ranges


class AdviceLog:
    """Bounded, hash-chained advice log; file-backed (durable) or, for tests only, in memory."""

    def __init__(self, path: Path, *, max_entries: int = DEFAULT_LOG_ENTRIES,
                 retention: RetentionPolicy | None = None, clock: Callable[[], datetime] = utc_now,
                 anchor: ExternalAnchor | None = None) -> None:
        self._setup(Path(path), max_entries, retention, clock, anchor, for_tests=False)

    @classmethod
    def ephemeral_for_tests(cls, *, acknowledgement: str, max_entries: int = DEFAULT_LOG_ENTRIES,
                            retention: RetentionPolicy | None = None, clock: Callable[[], datetime] = utc_now,
                            anchor: ExternalAnchor | None = None) -> AdviceLog:
        """TEST-ONLY in-memory log; production code never references this path (static test). It always
        carries an anchor (an in-memory compare-and-set anchor unless one is given), so anchored issuance
        behaves exactly as in production."""
        if acknowledgement != TEST_ONLY_ACKNOWLEDGEMENT:
            raise ValueError("ADVICE_LOG_EPHEMERAL_IS_TEST_ONLY")
        log = cls.__new__(cls)
        log._setup(None, max_entries, retention, clock, anchor if anchor is not None else _TestMemoryAnchor(),
                   for_tests=True)
        return log

    def _setup(self, path: Path | None, max_entries: int, retention: RetentionPolicy | None,
               clock: Callable[[], datetime], anchor: ExternalAnchor | None, *, for_tests: bool) -> None:
        if not 3 <= max_entries <= 100_000:
            raise ValueError("ADVICE_LOG_BOUND_INVALID")
        self._file = None if path is None else JsonlFile(path, max_bytes=256 * 1024 * 1024,
                                                         max_line_bytes=_LOG_LINE_BYTES)
        self._memory: list[dict[str, Any]] = []
        self._cache: tuple[tuple[int, int, int], tuple[AdviceLogEntry, ...]] | None = None
        self.for_tests = for_tests
        self.max_entries = max_entries
        self.anchor = anchor
        window = (retention or RetentionPolicy()).window(ADVICE_LOG_RETENTION)
        if window is None:  # pragma: no cover - R1 always has a window
            raise ValueError("ADVICE_LOG_RETENTION_WINDOW_MISSING")
        self.window = window
        self._clock = clock

    @property
    def durable(self) -> bool:
        return self._file is not None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._file is None:
            yield
            return
        with self._file.locked():
            yield

    def _stamp(self) -> tuple[int, int, int] | None:
        info = None if self._file is None else self._file.stat()
        return None if info is None else (info.st_ino, info.st_size, info.st_mtime_ns)

    def _load(self) -> list[AdviceLogEntry]:
        """Validated entries (caller holds the lock); a file parse is cached until the file changes."""
        stamp = self._stamp()
        if stamp is not None and self._cache is not None and self._cache[0] == stamp:
            return list(self._cache[1])
        lines = list(self._memory) if self._file is None else self._file.read_lines()
        try:
            for line in lines:
                assert_minimized(line, allowed_keys=ADVICE_LOG_KEYS, max_string=128, max_items=64,
                                 max_total_bytes=_LOG_LINE_BYTES - 1024)
            entries = [AdviceLogEntry.model_validate(line) for line in lines]
        except (ValueError, DataMinimizationViolation) as exc:
            raise LearningStoreError("LEARNING_STORE_CORRUPT") from exc
        self._cache = None if stamp is None else (stamp, tuple(entries))
        return entries

    def _anchor_state(self) -> tuple[Checkpoint | None, frozenset[Checkpoint]] | None:
        """(latest anchored checkpoint, anchored PRUNED markers), or None without an anchor; an unreachable or
        malformed anchor fails closed."""
        if self.anchor is None:
            return None
        try:
            latest, markers = self.anchor.latest(), tuple(self.anchor.markers())
        except Exception as exc:  # noqa: BLE001 - an unreachable anchor fails closed
            raise LearningStoreError("ADVICE_LOG_ANCHOR_UNAVAILABLE") from exc
        if not (latest is None or isinstance(latest, Checkpoint)) or not all(
                isinstance(item, Checkpoint) for item in markers):
            raise LearningStoreError("ADVICE_LOG_ANCHOR_UNAVAILABLE")
        return latest, frozenset(markers)

    def _validated(self, now: datetime) -> tuple[list[AdviceLogEntry], Checkpoint | None]:
        """Entries of a log whose chain verifies AND, with an anchor, whose head IS the anchored checkpoint
        (caller holds the lock). Runs before every mutation; future-dated entries are allowed (about to be
        pruned). Anything else fails closed: a broken or substituted chain is never pruned or appended to."""
        entries = self._load()
        state = self._anchor_state()
        if state is None:
            report = chain_report(entries, now=now)
        else:
            latest, markers = state
            if latest is None and entries:
                raise LearningStoreError("ADVICE_LOG_NOT_ANCHORED")
            report = chain_report(entries, now=now, checkpoint=latest, anchored_markers=markers)
        if not report.ok and report.code != "ADVICE_LOG_FUTURE_DATED":
            raise LearningStoreError(report.code)
        anchored = None if state is None else state[0]
        if state is not None and entries and _point(entries[-1]) != anchored:
            raise LearningStoreError("ADVICE_LOG_HEAD_NOT_ANCHORED")  # an unanchored suffix is never adopted
        return entries, anchored

    def _write(self, entries: Sequence[AdviceLogEntry], *, rewrite: bool) -> None:
        if rewrite:
            lines = [_minimized(entry) for entry in entries]
            if self._file is None:
                self._memory = lines
            else:
                self._file.rewrite(lines)
        else:
            line = _minimized(entries[-1])
            if self._file is None:
                self._memory.append(line)
            else:
                self._file.append_line(line)
        stamp = self._stamp()
        self._cache = None if stamp is None else (stamp, tuple(entries))

    def _commit(self, before: list[AdviceLogEntry], after: list[AdviceLogEntry], *, rewrite: bool,
                anchored: Checkpoint | None, marker: bool) -> None:
        """Write ``after`` (one new entry beyond the validated ``before``), then advance the anchor by
        compare-and-set from the validated head. A refused or unreachable anchor restores ``before`` (so the
        anchor still holds the trusted checkpoint and the file matches it) and fails closed."""
        self._write(after, rewrite=rewrite)
        if self.anchor is None:
            return
        try:
            self.anchor.record(anchored, _point(after[-1]), marker=marker)
        except Exception as exc:  # noqa: BLE001 - the anchor decides; any failure keeps the previous state
            self._write(before, rewrite=True)
            refused = isinstance(exc, AnchorError) and exc.code in {"ANCHOR_CONFLICT", "ANCHOR_NOT_CONTINUING"}
            raise LearningStoreError("ADVICE_LOG_ANCHOR_REFUSED" if refused
                                     else "ADVICE_LOG_ANCHOR_UNAVAILABLE") from exc

    def _pruned(self, entries: list[AdviceLogEntry], now: datetime, *, room: int) -> list[AdviceLogEntry] | None:
        """Retained entries plus a PRUNED marker, or None when nothing must go. ``room`` reserves slots for
        entries the caller appends next."""
        reasons: dict[int, PruneReason] = {}
        for index, entry in enumerate(entries):
            if entry.when > now + FUTURE_SKEW:
                reasons[index] = PruneReason.FUTURE_DATED
            elif entry.when < now - self.window:
                reasons[index] = PruneReason.EXPIRED
        if not reasons and len(entries) + room <= self.max_entries:
            return None
        kept = [index for index in range(len(entries)) if index not in reasons]
        capacity = self.max_entries - room - 1  # the marker takes one slot
        while True:
            while len(kept) > capacity:
                reasons[kept.pop(0)] = PruneReason.CAPACITY
            retained = [entries[index] for index in kept]
            ranges = _gaps(retained, entries[-1])
            if len(ranges) <= MAX_RANGES:
                break
            reasons[kept.pop(0)] = PruneReason.CAPACITY  # too many holes: drop the oldest until bounded
        return [*retained, self._marker(entries, retained, ranges, set(reasons.values()), now)]

    @staticmethod
    def _marker(entries: list[AdviceLogEntry], retained: list[AdviceLogEntry], ranges: list[RemovedRange],
                reasons: set[PruneReason], now: datetime) -> AdviceLogEntry:
        prior = entries[-1]
        previous = _latest_marker(entries)
        current = {entry.seq for entry in retained} | {item.through_seq for item in ranges} | {prior.seq}
        carried = {} if previous is None else previous.commitments()
        ancestry = sorted((LinkRef(seq=seq, entry_hash=digest) for seq, digest in carried.items()
                           if seq not in current), key=lambda link: link.seq)[-MAX_ANCESTRY:]
        marker = PruneMarker(prior_head=prior.link, ranges=tuple(ranges), ancestry=tuple(ancestry),
                             removed=len(entries) - len(retained),
                             reasons=tuple(reason for reason in PruneReason if reason in reasons))
        body = AdviceRecordBody(kind=EntryKind.PRUNED, logged_at=now.isoformat(), pruned=marker)
        return AdviceLogEntry.seal(body, seq=prior.seq + 1, prev_hash=prior.entry_hash)

    def record(self, body: AdviceRecordBody) -> AdviceLogEntry:
        """Append ``body`` as the next chained entry, validated against the anchor first and anchored by
        compare-and-set. Nothing is ever deleted here (CR-11d OL-14: no autonomous deletion): a full log refuses
        with ``ADVICE_LOG_FULL_PRUNE_REQUIRED`` until the operator runs ``prune()``."""
        if body.kind is not EntryKind.ADVICE:
            raise ValueError("ADVICE_LOG_MARKERS_ARE_WRITTEN_BY_THE_LOG")
        now = self._clock()
        if datetime.fromisoformat(body.logged_at) > now + FUTURE_SKEW:
            raise LearningStoreError("ADVICE_LOG_ENTRY_FUTURE_DATED")
        with self._locked():
            entries, anchored = self._validated(now)
            if len(entries) + 1 > self.max_entries:
                raise LearningStoreError("ADVICE_LOG_FULL_PRUNE_REQUIRED")
            last = entries[-1] if entries else None
            entry = AdviceLogEntry.seal(body, seq=last.seq + 1 if last else 1,
                                        prev_hash=last.entry_hash if last else GENESIS)
            self._commit(entries, [*entries, entry], rewrite=False, anchored=anchored, marker=False)
            return entry

    def _live(self, entry: AdviceLogEntry, now: datetime) -> bool:
        return now - self.window <= entry.when <= now + FUTURE_SKEW

    def _read(self) -> tuple[list[AdviceLogEntry], datetime]:
        """Anchor-validated stored entries (non-destructive: reads never delete, CR-11d OL-14)."""
        now = self._clock()
        with self._locked():
            return self._validated(now)[0], now

    def entries(self, *, include_markers: bool = False) -> tuple[AdviceLogEntry, ...]:
        """Chain-verified entries within the retention window. Expired and future-dated entries are HIDDEN, not
        deleted: physical deletion happens only through the operator-invoked ``prune()``."""
        stored, now = self._read()
        return tuple(entry for entry in stored
                     if (include_markers or entry.kind is EntryKind.ADVICE) and self._live(entry, now))

    def find(self, plan_binding_hash: str) -> tuple[AdviceLogEntry, ...]:
        """The retained advice entries recorded for one plan binding (plan -> audit entry linkage)."""
        return tuple(entry for entry in self.entries() if entry.plan_binding_hash == plan_binding_hash)

    def anchored_view(self) -> AnchoredView:
        """The retained entries as proven by the external anchor, in one locked step: pruned (anchored),
        then ``verify()``-equivalent against ``anchor.latest()`` with ``ANCHORED`` status and the head equal to
        the anchored checkpoint, so every returned entry lies within the anchored history. Fails closed
        (``LearningStoreError``) without an anchor or on any rewritten, truncated, unanchored or unverifiable
        history."""
        if self.anchor is None:
            raise LearningStoreError("ADVICE_LOG_ANCHOR_REQUIRED")
        now = self._clock()
        with self._locked():
            entries, _ = self._validated(now)  # non-destructive: nothing is pruned on a read
            state = self._anchor_state()
            latest, markers = (None, frozenset[Checkpoint]()) if state is None else state
            report = chain_report(entries, now=now, checkpoint=latest, anchored_markers=markers)
        if latest is None or not report.ok or report.anchor is not AnchorStatus.ANCHORED:
            raise LearningStoreError(report.code if not report.ok else "ADVICE_LOG_NOT_ANCHORED")
        if _point(entries[-1]) != latest:
            raise LearningStoreError("ADVICE_LOG_HEAD_NOT_ANCHORED")
        live = tuple(entry for entry in entries if entry.kind is not EntryKind.ADVICE or self._live(entry, now))
        return AnchoredView(live, latest, report)

    def prune(self) -> int:
        """Operator-invoked (the ONLY deletion path): physically delete expired, future-dated and over-capacity
        entries now, with an anchored PRUNED marker recorded by compare-and-set; returns how many were removed."""
        now = self._clock()
        with self._locked():
            entries, anchored = self._validated(now)
            pruned = self._pruned(entries, now, room=1)  # leave room for the next append
            if pruned is None:
                return 0
            self._commit(entries, pruned, rewrite=True, anchored=anchored, marker=True)
            marker = pruned[-1].pruned
            return 0 if marker is None else marker.removed

    def verify(self, *, checkpoint: Checkpoint | None = None) -> ChainReport:
        """Chain continuity, documented-only gaps, future dating and checkpoint ancestry (never modifies the
        log). Without ``checkpoint`` the configured ``ExternalAnchor``'s latest checkpoint is used, and the head
        must then BE that checkpoint (``ADVICE_LOG_HEAD_NOT_ANCHORED`` for an unanchored suffix). A pruned
        checkpoint verifies only through a marker the anchor recorded."""
        with self._locked():
            try:
                entries = self._load()
            except LearningStoreError as exc:
                return ChainReport(False, exc.code, 0, None, None, 0)
            try:
                state = self._anchor_state()
            except LearningStoreError as exc:
                return ChainReport(False, exc.code, len(entries), None, None, 0)
        latest, markers = (None, None) if state is None else state
        point = checkpoint if checkpoint is not None else latest
        report = chain_report(entries, now=self._clock(), checkpoint=point, anchored_markers=markers)
        if state is None or checkpoint is not None or not report.ok:
            return report
        if latest is None and entries:
            return replace(report, ok=False, code="ADVICE_LOG_NOT_ANCHORED")
        if entries and _point(entries[-1]) != latest:
            return replace(report, ok=False, code="ADVICE_LOG_HEAD_NOT_ANCHORED", anchor=AnchorStatus.UNANCHORED)
        return report

    def checkpoint(self) -> Checkpoint | None:
        """The current head (never modifies the log)."""
        with self._locked():
            entries = self._load()
        return _point(entries[-1]) if entries else None


@dataclass(frozen=True)
class AnchoredView:
    entries: tuple[AdviceLogEntry, ...]
    checkpoint: Checkpoint
    report: ChainReport


def _point(entry: AdviceLogEntry) -> Checkpoint:
    return Checkpoint(entry.seq, entry.entry_hash)


class _TestMemoryAnchor:
    """In-memory compare-and-set anchor attached ONLY to ``AdviceLog.ephemeral_for_tests`` logs."""

    def __init__(self) -> None:
        self._latest: Checkpoint | None = None
        self._markers: tuple[Checkpoint, ...] = ()

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        if previous != self._latest:
            raise AnchorError("ANCHOR_CONFLICT")
        if not continues(previous, new):
            raise AnchorError("ANCHOR_NOT_CONTINUING")
        self._latest = new
        if marker:
            self._markers = (*self._markers, new)[-64:]

    def latest(self) -> Checkpoint | None:
        return self._latest

    def markers(self) -> tuple[Checkpoint, ...]:
        return self._markers
