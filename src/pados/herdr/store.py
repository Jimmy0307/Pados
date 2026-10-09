# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded Herdr telemetry store (DataKind.HERDR_TELEMETRY, R1_OPERATIONAL).

One JSON file holding ``pados.herdr.telemetry.v1`` rows plus a bounded,
content-free collection-outcome log (``{at, outcome, code}``, CR-06 M01) and the
keyed digest of the last collected document. It is rewritten atomically (temp
file + fsync + replace, mode 0600) under the cross-process store lock.

Bounds (CR-06 M02): the file is size-checked before it is parsed (an oversized
or non-regular file reads as empty and is replaced on the next write); stored
arrays are truncated deterministically to the newest ``max_records`` rows /
``MAX_COLLECTIONS`` outcomes before validation; an incoming batch is bounded
with ``islice`` before accumulation; every write prunes by R1 age, untrusted
time, ``max_records`` and ``max_bytes``. Every row passes ``assert_minimized``
with the explicit telemetry schema and ``assert_no_authority``.

Ref key (CR-06 H01): a per-store random 32-byte key in ``<store>.refkey`` (0600,
created on first use under the store lock, never logged or projected) keys the
session/pane digests and the document digest.

Freshness (CR-06 H02/M01): a repeated document (same keyed digest) is recorded as
UNCHANGED and appends nothing, so it cannot refresh freshness. ``latest`` shows
a row live only when its ``source_observed_at`` is proven and within
``stale_after_seconds``, the latest collection outcome is OK and its health is not
UNREACHABLE; otherwise it is ``stale: true`` with state UNKNOWN and a closed
``stale_reason``. TERMINAL_HISTORY_DEFAULT is NOT_PERSISTED. This store is
operational telemetry, never semantic, Memory or audit state.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pados.connectivity.storage import atomic_write_json, store_lock
from pados.herdr.adapter import MAX_ENTRIES, REF_KEY_BYTES, STALE_AFTER_DEFAULT, STALE_AFTER_MAX, CollectResult
from pados.herdr.boundary import (
    MEANING_SCOPE,
    HerdrAuthorityViolation,
    assert_no_authority,
    telemetry_meaning,
)
from pados.herdr.contract import (
    CLOCK_SKEW,
    SCHEMA_ID,
    SEMANTIC_AUTHORITY_NONE,
    HerdrAgentState,
    HerdrHealth,
    HerdrTelemetryRecord,
    assert_record_minimized,
    parse_aware,
)
from pados.herdr.diagnostics import (
    COLLECTION_OK,
    HerdrCollectionOutcome,
    HerdrDiagnostic,
    HerdrStaleReason,
)
from pados.lifecycle.fswalk import fsync_directory
from pados.lifecycle.locking import ensure_durable_dir
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import DataKind, PayloadState, RetentionPolicy, policy_for

TERMINAL_HISTORY_DEFAULT = PayloadState.NOT_PERSISTED
DEFAULT_MAX_RECORDS = 2_000
MAX_RECORDS_LIMIT = 10_000
DEFAULT_MAX_BYTES = 1024 * 1024
MIN_MAX_BYTES = 8 * 1024
MAX_BYTES_LIMIT = 16 * 1024 * 1024
MAX_COLLECTIONS = 16
MAX_BATCH = MAX_ENTRIES
DOCUMENT_KEYS = ("schema_id", "kind", "retention_class", "semantic_authority", "records", "collections",
                 "last_document_digest")
COLLECTION_KEYS = ("at", "outcome", "code")
REF_KEY_SUFFIX = ".refkey"
_DIGEST = re.compile(r"^[0-9a-f]{32}$")
_OUTCOMES = {outcome.value: outcome for outcome in HerdrCollectionOutcome}
_SNAPSHOT_OUTCOMES = frozenset({HerdrCollectionOutcome.COLLECTED, HerdrCollectionOutcome.COLLECTED_PARTIAL})
_CODES = frozenset(code.value for code in HerdrDiagnostic)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
# Reads never block on a FIFO or device swapped in for the store or key file.
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0)


class HerdrStoreError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def observation_age_seconds(observed_at: object, now: datetime) -> float | None:
    """Age of an observation, or ``None`` (=> stale) when its time cannot be trusted."""
    stamp = parse_aware(observed_at)
    if stamp is None or now.tzinfo is None:
        return None
    age = (now - stamp).total_seconds()
    if age < -CLOCK_SKEW.total_seconds():
        return None
    return max(0.0, age)


def validate_stale_after(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= STALE_AFTER_MAX:
        raise HerdrStoreError("HERDR_STALE_AFTER_SECONDS_INVALID")
    return value


def _checked_row(record: HerdrTelemetryRecord) -> dict[str, Any]:
    if not isinstance(record, HerdrTelemetryRecord):
        raise HerdrStoreError("HERDR_STORE_RECORD_TYPE_INVALID")
    row = record.to_row()
    assert_record_minimized(row)
    assert_no_authority(row)
    return row


def _canonical(row: object) -> dict[str, Any] | None:
    """The row if it is exactly a valid telemetry record, else ``None``."""
    if not isinstance(row, dict):
        return None
    try:
        canonical = _checked_row(HerdrTelemetryRecord.model_validate(row))
    except (ValidationError, DataMinimizationViolation, HerdrAuthorityViolation, HerdrStoreError, TypeError,
            ValueError):
        return None
    return canonical if canonical == row else None


def _collection(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict) or set(row) != set(COLLECTION_KEYS):
        return None
    at, outcome, code = row["at"], row["outcome"], row["code"]
    if parse_aware(at) is None or not isinstance(outcome, str) or outcome not in _OUTCOMES:
        return None
    if code is not None and (not isinstance(code, str) or code not in _CODES):
        return None
    return {"at": at, "outcome": outcome, "code": code}


def _size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True).encode("utf-8"))


class HerdrTelemetryStore:
    def __init__(self, path: str | Path, *, max_records: int = DEFAULT_MAX_RECORDS,
                 max_bytes: int = DEFAULT_MAX_BYTES, retention: timedelta | None = None,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        if isinstance(max_records, bool) or not isinstance(max_records, int) \
                or not 1 <= max_records <= MAX_RECORDS_LIMIT:
            raise HerdrStoreError("HERDR_STORE_MAX_RECORDS_OUT_OF_RANGE")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
                or not MIN_MAX_BYTES <= max_bytes <= MAX_BYTES_LIMIT:
            raise HerdrStoreError("HERDR_STORE_MAX_BYTES_OUT_OF_RANGE")
        window = RetentionPolicy().r1
        if retention is not None and retention <= timedelta(0):
            raise HerdrStoreError("HERDR_STORE_RETENTION_INVALID")
        self.path = Path(path)
        self.key_path = self.path.with_name(f"{self.path.name}{REF_KEY_SUFFIX}")
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.retention = min(retention, window) if retention is not None else window
        self._clock = clock

    # --- ref key ---------------------------------------------------------------------------

    def ref_key(self) -> bytes:
        """The per-store digest key, created (0600) on first use. Never logged or projected."""
        ensure_durable_dir(self.path.parent)
        with store_lock(self.path):
            try:
                return self._read_key()
            except FileNotFoundError:
                pass
            key = secrets.token_bytes(REF_KEY_BYTES)
            descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            try:
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            fsync_directory(self.path.parent)
            return key

    def _read_key(self) -> bytes:
        if self.key_path.is_symlink():
            raise HerdrStoreError("HERDR_REF_KEY_INVALID")
        descriptor = os.open(self.key_path, _READ_FLAGS)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise HerdrStoreError("HERDR_REF_KEY_INVALID")
            if sys.platform != "win32" and info.st_mode & 0o077:
                raise HerdrStoreError("HERDR_REF_KEY_PERMISSIONS")
            key = os.read(descriptor, REF_KEY_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(key) != REF_KEY_BYTES:
            raise HerdrStoreError("HERDR_REF_KEY_INVALID")
        return key

    # --- reads (pure: never write) ---------------------------------------------------------

    def _read_document(self) -> dict[str, Any] | None:
        """The parsed store document, size-checked before parsing; anything unexpected is ``None``."""
        if self.path.is_symlink():
            return None
        try:
            descriptor = os.open(self.path, _READ_FLAGS)
        except OSError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                return None
            chunks: list[bytes] = []
            size = 0
            while size <= self.max_bytes:
                chunk = os.read(descriptor, self.max_bytes + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        if len(raw) > self.max_bytes:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        return data if isinstance(data, dict) else None

    def _load(self, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        data = self._read_document() or {}
        cutoff = now - self.retention
        raw_rows = data.get("records")
        rows: list[dict[str, Any]] = []
        for raw in raw_rows[-self.max_records:] if isinstance(raw_rows, list) else []:
            row = _canonical(raw)
            stamp = parse_aware(row["collected_at"]) if row is not None else None
            if row is not None and stamp is not None and stamp >= cutoff:
                rows.append(row)
        raw_collections = data.get("collections")
        collections: list[dict[str, Any]] = []
        for raw in raw_collections[-MAX_COLLECTIONS:] if isinstance(raw_collections, list) else []:
            entry = _collection(raw)
            stamp = parse_aware(entry["at"]) if entry is not None else None
            if entry is not None and stamp is not None and stamp >= cutoff:
                collections.append(entry)
        digest = data.get("last_document_digest")
        return rows, collections, digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None

    def records(self) -> list[dict[str, Any]]:
        """Retained, schema-valid rows in append order (never more than ``max_records``)."""
        return self._load(self._clock())[0]

    def collections(self) -> list[dict[str, Any]]:
        """The bounded, content-free collection-outcome log (oldest first)."""
        return self._load(self._clock())[1]

    def latest_collection(self) -> dict[str, Any] | None:
        collections = self.collections()
        return collections[-1] if collections else None

    def latest(self, *, stale_after_seconds: int = STALE_AFTER_DEFAULT,
               not_installed: bool = False) -> list[dict[str, Any]]:
        """Latest observation per (machine_ref, session_ref, pane_ref); stale rows never look live."""
        stale_after = validate_stale_after(stale_after_seconds)
        now = self._clock()
        rows, collections, _ = self._load(now)
        last = collections[-1] if collections else None
        # Membership of the most recent snapshot that delivered rows (an UNCHANGED repeat keeps it):
        # a pane a newer snapshot no longer reports is never shown live (CR-06b N01).
        snapshot_at = next((parse_aware(c["at"]) for c in reversed(collections)
                            if _OUTCOMES[c["outcome"]] in _SNAPSHOT_OUTCOMES), None)
        latest: dict[tuple[str, str, str], tuple[datetime, dict[str, Any]]] = {}
        for row in rows:
            key = (row["machine_ref"], row["session_ref"], row["pane_ref"])
            stamp = parse_aware(row["collected_at"])
            current = latest.get(key)
            if stamp is not None and (current is None or stamp >= current[0]):
                latest[key] = (stamp, row)
        return [self._project(latest[key][1], now, stale_after, last, not_installed, snapshot_at)
                for key in sorted(latest)]

    @staticmethod
    def _stale_reason(row: dict[str, Any], age: float | None, stale_after: int, last: dict[str, Any] | None,
                      not_installed: bool, snapshot_at: datetime | None = None) -> HerdrStaleReason | None:
        if not_installed:
            return HerdrStaleReason.SOURCE_NOT_INSTALLED
        if last is None:
            return HerdrStaleReason.NOT_COLLECTED
        if _OUTCOMES[last["outcome"]] not in COLLECTION_OK:
            return HerdrStaleReason.LATEST_COLLECTION_UNHEALTHY
        if snapshot_at is not None and parse_aware(row["collected_at"]) != snapshot_at:
            return HerdrStaleReason.NOT_IN_LATEST_SNAPSHOT
        if row["health"] == HerdrHealth.UNREACHABLE.value:
            return HerdrStaleReason.SOURCE_HEALTH_UNREACHABLE
        if row["source_observed_at"] is None or age is None:
            return HerdrStaleReason.SOURCE_TIME_UNPROVEN
        if age > stale_after:
            return HerdrStaleReason.SOURCE_TIME_EXPIRED
        return None

    @classmethod
    def _project(cls, row: dict[str, Any], now: datetime, stale_after: int, last: dict[str, Any] | None,
                 not_installed: bool, snapshot_at: datetime | None = None) -> dict[str, Any]:
        age = observation_age_seconds(row["source_observed_at"], now)
        reason = cls._stale_reason(row, age, stale_after, last, not_installed, snapshot_at)
        stale = reason is not None
        observed = HerdrAgentState(row["state"])
        shown = HerdrAgentState.UNKNOWN if stale else observed
        return {
            "machine_ref": row["machine_ref"], "session_ref": row["session_ref"], "pane_ref": row["pane_ref"],
            "agent_kind": row["agent_kind"], "state": shown.value, "health": None if stale else row["health"],
            "last_observed_state": observed.value, "last_observed_health": row["health"],
            "telemetry_meaning": telemetry_meaning(shown).value, "meaning_scope": MEANING_SCOPE,
            "collected_at": row["collected_at"], "source_observed_at": row["source_observed_at"],
            "last_transition_at": row["last_transition_at"],
            "source_age_seconds": None if age is None else round(age, 1),
            "stale": stale, "stale_reason": None if reason is None else reason.value,
            "project_ref": row["project_ref"], "work_package_ref": row["work_package_ref"],
            "semantic_authority": SEMANTIC_AUTHORITY_NONE,
        }

    # --- writes ----------------------------------------------------------------------------

    def record_collection(self, result: CollectResult) -> HerdrCollectionOutcome:
        """Persist one collection: its outcome always, its records unless the document repeated."""
        if not isinstance(result, CollectResult):
            raise HerdrStoreError("HERDR_STORE_RESULT_TYPE_INVALID")
        batch = list(islice(result.records, MAX_BATCH + 1))
        if len(batch) > MAX_BATCH:
            raise HerdrStoreError("HERDR_STORE_BATCH_TOO_LARGE")
        new_rows = [_checked_row(record) for record in batch]
        code = result.error_code or result.source_time_code or next(iter(result.refusal_codes), None)
        entry = {"at": result.collected_at.isoformat(), "outcome": result.outcome.value,
                 "code": None if code is None else code.value}
        ensure_durable_dir(self.path.parent)
        with store_lock(self.path):
            now = self._clock()
            rows, collections, digest = self._load(now)
            outcome = result.outcome
            if outcome in COLLECTION_OK and result.document_digest is not None and result.document_digest == digest:
                outcome, new_rows = HerdrCollectionOutcome.UNCHANGED, []
                entry["outcome"] = outcome.value
            if outcome in {HerdrCollectionOutcome.COLLECTED, HerdrCollectionOutcome.COLLECTED_PARTIAL}:
                digest = result.document_digest
            checked = _collection(entry)
            if checked is None:
                raise HerdrStoreError("HERDR_STORE_COLLECTION_INVALID")
            self._persist(rows + new_rows, [*collections, checked], digest, now)
            return outcome

    def prune(self) -> int:
        """Rewrite without expired, untrustworthy, malformed or over-bound rows; returns rows removed."""
        if not self.path.exists():
            return 0
        with store_lock(self.path):
            now = self._clock()
            data = self._read_document()
            raw = data.get("records") if data is not None else None
            total = len(raw) if isinstance(raw, list) else 1
            rows, collections, digest = self._load(now)
            kept = self._persist(rows, collections, digest, now)
            return total - kept

    def _persist(self, rows: list[dict[str, Any]], collections: list[dict[str, Any]], digest: str | None,
                 now: datetime) -> int:
        cutoff, horizon = now - self.retention, now + CLOCK_SKEW

        def fresh(stamp: object) -> bool:
            parsed = parse_aware(stamp)
            return parsed is not None and cutoff <= parsed <= horizon

        rows = [row for row in rows if fresh(row["collected_at"])][-self.max_records:]
        collections = [c for c in collections if fresh(c["at"])][-MAX_COLLECTIONS:]
        sizes = [_size(row) for row in rows]
        total = _size(self._document([], collections, digest)) + sum(sizes) + 2 * max(0, len(rows) - 1)
        start = 0
        while start < len(rows) and total > self.max_bytes:
            total -= sizes[start] + (2 if len(rows) - start > 1 else 0)
            start += 1
        rows = rows[start:]
        for row in rows:
            assert_record_minimized(row)
            assert_no_authority(row)
        for entry in collections:
            assert_minimized(entry, allowed_keys=COLLECTION_KEYS, max_string=128, max_items=len(COLLECTION_KEYS))
        atomic_write_json(self.path, self._document(rows, collections, digest))
        return len(rows)

    @staticmethod
    def _document(rows: list[dict[str, Any]], collections: list[dict[str, Any]],
                  digest: str | None) -> dict[str, Any]:
        policy = policy_for(DataKind.HERDR_TELEMETRY)
        return {"schema_id": SCHEMA_ID, "kind": policy.kind.value, "retention_class": policy.retention.value,
                "semantic_authority": SEMANTIC_AUTHORITY_NONE, "records": rows, "collections": collections,
                "last_document_digest": digest}
