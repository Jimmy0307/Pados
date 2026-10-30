# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only JSONL stores for outcome and evaluation records (operational, never authority).

* ``OutcomeStore`` keeps authenticated ``OutcomeRecord`` lines under R1_OPERATIONAL retention
  (``RetentionPolicy.r1``, default 14 days; DataKind ``OUTCOME_RECORD``). Every append is authenticated
  through an ``OutcomeAuthenticator`` (``LEARNING_OUTCOME_UNAUTHENTICATED`` otherwise) and every read
  re-authenticates: records whose receipt no longer resolves, or whose facts no longer match the trusted
  receipt / plan context / governed verdict, are excluded and counted. Retention ages a record from its
  TRUSTED execution time (``executed_at``, the receipt's ``finished_at``), never from its ingestion time; a
  record whose execution is already outside the window is refused. Reads, writes and the operator-invoked
  ``prune()`` delete expired and future-dated (beyond ``FUTURE_SKEW``) records physically under the lock; the
  work is bounded because the file is bounded by record count and bytes (oldest execution first out; R1
  records are DELETE_RECORD data). There is no timer: an idle store is pruned on its next access or when the
  operator calls ``prune()`` (the lifecycle authority recommends; it never deletes autonomously).
* Ingestion identity: a bounded ingestion ledger next to the store (``<name>.ingest``, same lock, same file
  checks) retains the outcome id (derived from the receipt hash) and execution time of every record kept
  within the retention window, so the same receipt can never be ingested twice inside the window even after
  its record was dropped for capacity (``OUTCOME_ALREADY_INGESTED``). When the ledger itself is full, the
  oldest identities are dropped and the ledger floor rises to their newest execution time; anything executed
  at or before the floor is refused (``OUTCOME_BELOW_INGESTION_FLOOR``), so a dropped identity cannot re-enter.
* ``EvaluationStore`` keeps ``EvaluationRecord`` lines under R2_PROJECT retention (``EVALUATION_RECORD``):
  never expired on age or pruned automatically; a full store refuses; future-dated lines are not returned and
  are deleted only by the operator-invoked ``prune()`` (R2 purge is a human decision).

Every line passes ``assert_minimized`` against the record's explicit key schema before it is written and
is re-validated when read; a corrupt or foreign line fails closed (``LEARNING_STORE_CORRUPT``). Every
descriptor opened on a store file is checked with ``fstat``: a regular file, owned by the current user and
not readable or writable by group/other (POSIX), else ``LEARNING_STORE_INSECURE`` - an insecure file is
refused, never silently repaired. Links are refused and read-modify-write runs under the shared
cross-process store lock.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator

from pados.connectivity.storage import store_lock
from pados.lifecycle.fswalk import fsync_directory, is_link
from pados.lifecycle.locking import ensure_durable_dir
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import RetentionClass, RetentionPolicy
from pados.learning.records import (
    CLOCK_SKEW,
    EvaluationRecord,
    IdentityKey,
    OutcomeAuthenticator,
    OutcomeRecord,
    authenticate_outcome,
)

OUTCOME_RETENTION = RetentionClass.R1_OPERATIONAL
EVALUATION_RETENTION = RetentionClass.R2_PROJECT
ADVICE_LOG_RETENTION = RetentionClass.R1_OPERATIONAL
DEFAULT_MAX_RECORDS = 20_000
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
FUTURE_SKEW = CLOCK_SKEW
INGEST_SCHEMA = "pados.learning.ingest.v1"
LEDGER_FACTOR = 4  # ingestion identities retained per record slot

Clock = Callable[[], datetime]
R = TypeVar("R")


def utc_now() -> datetime:
    return datetime.now(UTC)


class LearningStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AppendResult:
    appended: bool
    pruned: int


@dataclass(frozen=True)
class OutcomeReadResult:
    records: tuple[OutcomeRecord, ...]
    unauthenticated: int  # excluded: receipt no longer resolves or facts/verdict no longer match
    pruned: int  # always 0 on reads (reads never delete); expired/future-dated records are hidden


def check_secure(info: os.stat_result) -> None:
    """Regular file, owned by the current user, no group/other access (POSIX); else fail closed."""
    if is_link(info) or not stat.S_ISREG(info.st_mode):
        raise LearningStoreError("LEARNING_STORE_NOT_REGULAR_FILE")
    if sys.platform != "win32":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise LearningStoreError("LEARNING_STORE_INSECURE")


class JsonlFile:
    """Owner-only JSONL file: bounded reads, fsynced appends, atomic rewrites; refuses links and insecure files."""

    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_line_bytes = max_line_bytes

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Cross-process exclusive lock; the owner-only directory is created before the lock file."""
        ensure_durable_dir(self.path.parent)
        with store_lock(self.path):
            yield

    def stat(self) -> os.stat_result | None:
        """``lstat`` of the store file (None when absent), checked like an opened descriptor."""
        ensure_durable_dir(self.path.parent)
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        check_secure(info)
        return info

    def read_lines(self) -> list[dict[str, Any]]:
        if self.stat() is None:
            return []
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(self.path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            check_secure(os.fstat(handle.fileno()))
            payload = handle.read(self.max_bytes + 1)
        if len(payload) > self.max_bytes:
            raise LearningStoreError("LEARNING_STORE_TOO_LARGE")
        lines: list[dict[str, Any]] = []
        for raw in payload.splitlines():
            if not raw.strip():
                continue
            if len(raw) > self.max_line_bytes:
                raise LearningStoreError("LEARNING_STORE_CORRUPT")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise LearningStoreError("LEARNING_STORE_CORRUPT") from exc
            if not isinstance(value, dict):
                raise LearningStoreError("LEARNING_STORE_CORRUPT")
            lines.append(value)
        return lines

    def encode(self, record: Mapping[str, Any]) -> bytes:
        line = json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        if len(line) > self.max_line_bytes:
            raise LearningStoreError("LEARNING_RECORD_TOO_LARGE")
        return line

    def append_line(self, record: Mapping[str, Any]) -> None:
        line = self.encode(record)
        created = self.stat() is None
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0))
        descriptor = os.open(self.path, flags, 0o600)
        try:
            try:
                check_secure(os.fstat(descriptor))
                written = 0
                while written < len(line):
                    count = os.write(descriptor, line[written:])
                    if count <= 0:
                        raise OSError("JSONL_APPEND_SHORT_WRITE")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            # If this call created the file, a failed first append must not leave a visible uncommitted log.
            # The descriptor is already closed here: Windows refuses to delete a file that is still open, which
            # would mask the original failure and leave the partial file visible. Existing-file appends are
            # reconciled by their owning higher-level protocol.
            if created:
                self.path.unlink(missing_ok=True)
            raise
        if created:
            # A newly-created name is not crash-durable until the containing directory is fsynced. If this
            # durability step itself fails, roll the just-created file back before propagating the error so
            # callers never observe a first log entry without their corresponding authority/anchor commit.
            try:
                fsync_directory(self.path.parent)
            except BaseException:
                self.path.unlink(missing_ok=True)
                try:
                    fsync_directory(self.path.parent)
                except BaseException:
                    pass
                raise

    def rewrite(self, records: Sequence[Mapping[str, Any]]) -> None:
        payload = b"".join(self.encode(record) for record in records)
        self.stat()
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0))
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                check_secure(os.fstat(handle.fileno()))
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        fsync_directory(self.path.parent)


class _RecordStore(Generic[R]):
    """Shared validated read / idempotent append over one JSONL file."""

    def __init__(self, path: Path, *, parse: Callable[[Mapping[str, Any]], R], key: Callable[[R], str],
                 digest: Callable[[R], str], encode: Callable[[R], dict[str, Any]], max_records: int,
                 max_bytes: int) -> None:
        if max_records < 1 or max_bytes < MAX_LINE_BYTES:
            raise ValueError("LEARNING_STORE_BOUNDS_INVALID")
        self._file = JsonlFile(path, max_bytes=max_bytes)
        self._parse, self._key, self._digest, self._encode = parse, key, digest, encode
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._cache: tuple[tuple[int, int, int], tuple[R, ...]] | None = None

    @property
    def path(self) -> Path:
        return self._file.path

    def _stamp(self) -> tuple[int, int, int] | None:
        info = self._file.stat()  # also enforces ownership/permissions on cache hits
        return None if info is None else (info.st_ino, info.st_size, info.st_mtime_ns)

    def _load(self) -> list[R]:
        """Validated records (caller holds the lock). Records are immutable, so a parse is cached until the
        file's identity, size or mtime changes (any writer, in any process, changes them)."""
        stamp = self._stamp()
        if stamp is not None and self._cache is not None and self._cache[0] == stamp:
            return list(self._cache[1])
        records: list[R] = []
        for line in self._file.read_lines():
            try:
                records.append(self._parse(line))
            except (ValueError, DataMinimizationViolation) as exc:
                raise LearningStoreError("LEARNING_STORE_CORRUPT") from exc
        self._cache = None if stamp is None else (stamp, tuple(records))
        return records

    def _remember(self, records: list[R]) -> None:
        stamp = self._stamp()
        self._cache = None if stamp is None else (stamp, tuple(records))

    def _append(self, line: Mapping[str, Any], records: list[R]) -> None:
        self._file.append_line(line)
        self._remember(records)

    def _rewrite(self, records: list[R]) -> None:
        self._file.rewrite([self._encode(item) for item in records])
        self._remember(records)

    def _duplicate(self, records: list[R], record: R) -> bool:
        for existing in records:
            if self._key(existing) == self._key(record):
                if self._digest(existing) != self._digest(record):
                    raise LearningStoreError("LEARNING_RECORD_ID_CONFLICT")
                return True
        return False

    def _size(self, records: list[R]) -> int:
        return sum(len(self._file.encode(self._encode(item))) for item in records)


class IngestEntry(BaseModel):
    """One retained ingestion identity, or (``outcome_id=None``) the ledger floor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = INGEST_SCHEMA
    outcome_id: str | None
    executed_at: str

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != INGEST_SCHEMA:
            raise ValueError("INGEST_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("outcome_id")
    @classmethod
    def derived_id(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 43 or not value.startswith("oc-")
                                  or any(c not in "0123456789abcdef" for c in value[3:])):
            raise ValueError("INGEST_OUTCOME_ID_INVALID")
        return value

    @field_validator("executed_at")
    @classmethod
    def aware(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("INGEST_TIMESTAMP_NOT_AWARE")
        return parsed.astimezone(UTC).isoformat()

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.executed_at)

    def to_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json")
        assert_minimized(record, allowed_keys=INGEST_KEYS, max_string=128, max_items=8, max_total_bytes=1024)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> IngestEntry:
        assert_minimized(record, allowed_keys=INGEST_KEYS, max_string=128, max_items=8, max_total_bytes=1024)
        return cls.model_validate(dict(record))


INGEST_KEYS = frozenset(IngestEntry.model_fields)


class IngestionLedger(_RecordStore[IngestEntry]):
    """Bounded ingestion identities within the retention window (the caller holds the outcome store lock)."""

    def __init__(self, path: Path, *, max_entries: int) -> None:
        super().__init__(path, parse=IngestEntry.from_record, key=lambda e: e.outcome_id or "",
                         digest=lambda e: e.executed_at, encode=IngestEntry.to_record, max_records=max_entries,
                         max_bytes=max(MAX_LINE_BYTES, 256 * max_entries))

    def load_locked(self, now: datetime, window: timedelta, *,
                    delete: bool = False) -> tuple[dict[str, datetime], datetime | None]:
        """Live identities and the live floor. Expired identities are hidden; they are deleted only when
        ``delete`` is set by the operator-invoked ``OutcomeStore.prune()`` (CR-11d OL-14)."""
        stored = self._load()
        limit = now - window
        floor = max((e.when for e in stored if e.outcome_id is None), default=None)
        live = [e for e in stored if e.when >= limit and (e.outcome_id is not None or e.when == floor)]
        if delete and len(live) != len(stored):
            self._rewrite(live)
        identities = {e.outcome_id: e.when for e in live if e.outcome_id is not None}
        return identities, (floor if floor is not None and floor >= limit else None)

    def add_locked(self, record: OutcomeRecord, identities: dict[str, datetime], floor: datetime | None) -> None:
        entry = IngestEntry(outcome_id=record.outcome_id, executed_at=record.executed_at)
        stored = self._load()
        if len(stored) + 1 > self.max_records:
            # CR-17 (OL-14 remainder): the identity ledger never folds or deletes on its own either; a full ledger
            # refuses the append until the operator's prune() removes expired identities. A floor written by an
            # earlier version is still honoured on read.
            raise LearningStoreError("OUTCOME_INGESTION_LEDGER_FULL_PRUNE_REQUIRED")
        self._append(entry.to_record(), [*stored, entry])


class OutcomeStore(_RecordStore[OutcomeRecord]):
    """R1 authenticated outcome records aged by trusted execution time; one ingestion per receipt per window."""

    def __init__(self, path: Path, *, authenticator: OutcomeAuthenticator,
                 retention: RetentionPolicy | None = None, max_records: int = DEFAULT_MAX_RECORDS,
                 max_bytes: int = DEFAULT_MAX_BYTES, clock: Clock = utc_now,
                 identity_key: IdentityKey | None = None) -> None:
        super().__init__(path, parse=OutcomeRecord.from_record, key=lambda r: r.outcome_id,
                         digest=lambda r: r.record_hash, encode=lambda r: r.model_dump(mode="json"),
                         max_records=max_records, max_bytes=max_bytes)
        self.authenticator = authenticator
        self._identity_key = identity_key  # never persisted; must equal the record builder's and advisor's key
        self.retention = retention or RetentionPolicy()
        window = self.retention.window(OUTCOME_RETENTION)
        if window is None:  # pragma: no cover - R1 always has a window
            raise ValueError("OUTCOME_RETENTION_WINDOW_MISSING")
        self.window = window
        self._clock = clock
        self.ledger = IngestionLedger(self.path.with_name(self.path.name + ".ingest"),
                                      max_entries=LEDGER_FACTOR * max_records + 1)

    def _in_window(self, record: OutcomeRecord, now: datetime) -> bool:
        return (now - self.window <= record.executed_at_time <= now + FUTURE_SKEW
                and record.recorded_at_time <= now + FUTURE_SKEW)

    def _prune_locked(self, now: datetime) -> tuple[list[OutcomeRecord], int]:
        """Physical deletion: called ONLY by the operator-invoked ``prune()`` (CR-11d OL-14)."""
        stored = self._load()
        live = [item for item in stored if self._in_window(item, now)]
        if len(live) != len(stored):
            self._rewrite(live)
        self.ledger.load_locked(now, self.window, delete=True)
        return live, len(stored) - len(live)

    def read_checked(self) -> OutcomeReadResult:
        """Live records that still authenticate, oldest execution first. Non-destructive: expired and
        future-dated lines are hidden, never deleted on a read."""
        now = self._clock()
        with self._file.locked():
            stored = self._load()
        live = sorted((item for item in stored if self._in_window(item, now)),
                      key=lambda item: (item.executed_at, item.outcome_id))
        trusted = tuple(item for item in live if authenticate_outcome(item, self.authenticator, self._identity_key))
        return OutcomeReadResult(records=trusted, unauthenticated=len(live) - len(trusted), pruned=0)

    def read(self) -> tuple[OutcomeRecord, ...]:
        return self.read_checked().records

    def append(self, record: OutcomeRecord) -> AppendResult:
        line = record.to_record()  # minimization guard before anything else
        now = self._clock()
        if record.recorded_at_time > now + FUTURE_SKEW:
            raise LearningStoreError("OUTCOME_RECORDED_IN_FUTURE")
        if record.executed_at_time > now + FUTURE_SKEW:
            raise LearningStoreError("OUTCOME_EXECUTED_IN_FUTURE")
        if record.executed_at_time < now - self.window:
            raise LearningStoreError("OUTCOME_RECORD_ALREADY_EXPIRED")
        if not authenticate_outcome(record, self.authenticator, self._identity_key):
            raise LearningStoreError("LEARNING_OUTCOME_UNAUTHENTICATED")
        with self._file.locked():
            stored = self._load()
            live = [item for item in stored if self._in_window(item, now)]
            if self._duplicate(live, record):
                return AppendResult(appended=False, pruned=0)
            identities, floor = self.ledger.load_locked(now, self.window)
            if record.outcome_id in identities:
                raise LearningStoreError("OUTCOME_ALREADY_INGESTED")
            if floor is not None and record.executed_at_time <= floor:
                raise LearningStoreError("OUTCOME_BELOW_INGESTION_FLOOR")
            stamp = self._stamp()
            file_bytes = 0 if stamp is None else stamp[1]
            if len(stored) + 1 > self.max_records or file_bytes + len(self._file.encode(line)) > self.max_bytes:
                # CR-11d OL-14: an append never deletes; the operator's prune() frees space.
                raise LearningStoreError("OUTCOME_STORE_FULL_PRUNE_REQUIRED")
            self.ledger.add_locked(record, identities, floor)  # identity BEFORE the record (crash loses, never replays)
            self._append(line, [*stored, record])
            return AppendResult(appended=True, pruned=0)

    def prune(self) -> int:
        """Operator-invoked: delete expired and future-dated records (and expired ingestion identities) now;
        returns how many records were removed."""
        with self._file.locked():
            return self._prune_locked(self._clock())[1]


class EvaluationStore(_RecordStore[EvaluationRecord]):
    """R2 evaluation records: never pruned automatically; a full store refuses (human decision to purge)."""

    def __init__(self, path: Path, *, max_records: int = DEFAULT_MAX_RECORDS,
                 max_bytes: int = DEFAULT_MAX_BYTES, clock: Clock = utc_now) -> None:
        super().__init__(path, parse=EvaluationRecord.from_record, key=lambda r: r.evaluation_id,
                         digest=lambda r: r.record_hash, encode=lambda r: r.model_dump(mode="json"),
                         max_records=max_records, max_bytes=max_bytes)
        self._clock = clock

    def read(self) -> tuple[EvaluationRecord, ...]:
        """Stored results; future-dated lines (beyond the skew) are not returned (R2: never auto-deleted)."""
        limit = self._clock() + FUTURE_SKEW
        with self._file.locked():
            return tuple(item for item in self._load() if datetime.fromisoformat(item.evaluated_at) <= limit)

    def prune(self) -> int:
        """Operator-invoked (R2 purge is a human decision): delete future-dated lines; returns the count.
        R2 has no age window, so nothing is deleted on age."""
        limit = self._clock() + FUTURE_SKEW
        with self._file.locked():
            stored = self._load()
            kept = [item for item in stored if datetime.fromisoformat(item.evaluated_at) <= limit]
            if len(kept) != len(stored):
                self._rewrite(kept)
            return len(stored) - len(kept)

    def append(self, record: EvaluationRecord) -> AppendResult:
        line = record.to_record()
        if datetime.fromisoformat(record.evaluated_at) > self._clock() + FUTURE_SKEW:
            raise LearningStoreError("EVALUATION_RECORDED_IN_FUTURE")
        with self._file.locked():
            stored = self._load()
            if self._duplicate(stored, record):
                return AppendResult(appended=False, pruned=0)
            if (len(stored) + 1 > self.max_records
                    or self._size(stored) + len(self._file.encode(line)) > self.max_bytes):
                raise LearningStoreError("EVALUATION_STORE_FULL")
            self._append(line, [*stored, record])
            return AppendResult(appended=True, pruned=0)
