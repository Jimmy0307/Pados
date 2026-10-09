# SPDX-License-Identifier: AGPL-3.0-only
"""Durable notification outbox (DataKind.NOTIFICATION_DELIVERY, R1_OPERATIONAL).

A PADOS-owned file (atomic write + fsync + replace, mode 0600) holding push
delivery metadata only: notification id, kind, the opaque push ``ref`` hash,
timestamps, attempt counters, delivery status, expiry and an opaque provider
label. It never holds project names, goals, run ids, context or payload text.
Every record is strictly validated on insert AND on reload (invalid records are
dropped and counted, never forwarded) and passes the minimization guard.

Concurrency: every operation is reload + modify + persist under a cross-process
store lock, so several processes may share one outbox. Each individual send is
immediately preceded by its own atomic claim, which re-checks expiry and backoff:
status IN_FLIGHT, attempts + 1, a random ``attempt_id`` and a lease
(``lease_until``, independent of the retry backoff and renewable by the owner).
``complete``/``fail`` must present the matching ``attempt_id``; a stale owner's
outcome is a no-op reported STALE_ATTEMPT, so it can never reset another
attempt's claim.

Delivery is AT-LEAST-ONCE with a stable idempotency key. A crash (or a send
outliving its lease) lets the record be re-claimed after ``lease_until`` and
re-sent with the same opaque ``notification_id``. The push provider contract
MUST deduplicate on ``notification_id``; PADOS does not (and cannot) enforce
deduplication inside the provider, so without it a duplicate metadata-only push
is an ACCEPTED_RISK. A record already DELIVERED is never pushed again. Delivery status is independent of job state: the outbox
only produces metadata-only payloads (``PUSH_ALLOWED_KEYS``) and can trigger no
semantic operation. Retention is enforced on open, on every operation and on
read; expired records are removed (pending ones are reported EXPIRED).
"""
from __future__ import annotations

import re
import secrets
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.connectivity.storage import assert_record_schema, atomic_write_json, read_json, store_lock
from pados.lifecycle.retention import DataKind, RetentionPolicy, policy_for
from pados.remote.notifications import PUSH_ALLOWED_KEYS, NotificationKind

RECORD_KEYS = ("notification_id", "kind", "ref", "created_at", "attempts", "last_attempt_at",
               "next_attempt_at", "delivery_status", "expires_at", "provider_ref", "attempt_id", "lease_until")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
_ID = re.compile(r"nf-[0-9a-f]{32}")
_REF = re.compile(r"[0-9a-f]{16}")
_PROVIDER_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_KINDS = frozenset(kind.value for kind in NotificationKind)
_CLOCK_SKEW = timedelta(minutes=5)
_EARLIEST = datetime(2020, 1, 1, tzinfo=UTC)
MAX_ATTEMPTS = 10_000
MAX_LEASE = timedelta(hours=1)


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    DELIVERED = "DELIVERED"
    EXPIRED = "EXPIRED"  # reported only; expired records are removed, never stored


_STORED = frozenset({OutboxStatus.PENDING.value, OutboxStatus.IN_FLIGHT.value, OutboxStatus.DELIVERED.value})
_OPEN = frozenset({OutboxStatus.PENDING.value, OutboxStatus.IN_FLIGHT.value})


class AttemptOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    RENEWED = "RENEWED"
    STALE_ATTEMPT = "STALE_ATTEMPT"  # the presented attempt_id no longer owns the record: no-op
    NOT_FOUND = "NOT_FOUND"  # expired/purged meanwhile: no-op


_LEGACY_KEYS = frozenset(RECORD_KEYS) - {"attempt_id", "lease_until"}


def _upgrade_legacy(row: Any) -> Any:
    """Records written before attempt ownership existed are kept, not dropped.

    A legacy IN_FLIGHT claim has no owner to settle it, so it is converted to PENDING and
    retried (at-least-once with the stable ``notification_id``); every other field is then
    validated exactly like a current record.
    """
    if not isinstance(row, dict) or set(row) != _LEGACY_KEYS:
        return row
    status = row.get("delivery_status")
    upgraded = {**row, "attempt_id": None, "lease_until": None}
    if status == OutboxStatus.IN_FLIGHT.value:
        upgraded["delivery_status"] = OutboxStatus.PENDING.value
    return upgraded


def _member(value: object, allowed: frozenset[str]) -> bool:
    """Membership only for strings: unhashable JSON values (lists, objects) never raise."""
    return isinstance(value, str) and value in allowed


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: object) -> datetime | None:
    """Strict ISO-8601 UTC timestamp (offset must be zero)."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    return parsed if offset is not None and offset == timedelta(0) else None


def _at(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


class DurableNotificationOutbox:
    def __init__(self, path: Path, *, retention: timedelta | None = None, provider_ref: str = "push",
                 clock: Callable[[], datetime] = _utc_now, max_records: int = 500,
                 base_backoff: timedelta = timedelta(seconds=30),
                 max_backoff: timedelta = timedelta(hours=1),
                 lease: timedelta = timedelta(minutes=2)) -> None:
        if not _PROVIDER_REF.fullmatch(provider_ref):
            raise ValueError("OUTBOX_PROVIDER_REF_INVALID")
        if not 1 <= max_records <= 10_000 or not timedelta(0) < base_backoff <= max_backoff <= timedelta(days=1):
            raise ValueError("OUTBOX_BOUNDS_INVALID")
        if not timedelta(seconds=1) <= lease <= MAX_LEASE:
            raise ValueError("OUTBOX_LEASE_INVALID")
        self.window = RetentionPolicy().r1
        self.path = Path(path)
        self.retention = min(retention, self.window) if retention is not None else self.window
        self.provider_ref = provider_ref
        self.max_records = max_records
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.lease = lease
        self._clock = clock
        self.corrupt_dropped = 0
        self.expired: deque[str] = deque(maxlen=1000)  # R0: ids of undelivered records that expired
        self.maintain()

    # --- validation -----------------------------------------------------------------------------

    def _valid(self, row: object, now: datetime) -> bool:
        if not isinstance(row, dict) or set(row) != set(RECORD_KEYS):
            return False
        text_ok = bool(isinstance(row["notification_id"], str) and _ID.fullmatch(row["notification_id"])
                       and _member(row["kind"], _KINDS) and isinstance(row["ref"], str)
                       and _REF.fullmatch(row["ref"]) and _member(row["delivery_status"], _STORED)
                       and isinstance(row["provider_ref"], str) and _PROVIDER_REF.fullmatch(row["provider_ref"]))
        attempts = row["attempts"]
        if not text_ok or isinstance(attempts, bool) or not isinstance(attempts, int) \
                or not 0 <= attempts <= MAX_ATTEMPTS:
            return False
        created, expires, due = _utc(row["created_at"]), _utc(row["expires_at"]), _utc(row["next_attempt_at"])
        last = row["last_attempt_at"]
        last_at = None if last is None else _utc(last)
        if created is None or expires is None or due is None or (last is not None and last_at is None):
            return False
        if not _EARLIEST <= created <= now + _CLOCK_SKEW:
            return False
        if not created < expires <= created + self.window or due > expires:
            return False
        if last_at is not None and not created - _CLOCK_SKEW <= last_at <= now + _CLOCK_SKEW:
            return False
        attempt_id, lease = row["attempt_id"], row["lease_until"]
        if row["delivery_status"] != OutboxStatus.IN_FLIGHT.value:
            return attempt_id is None and lease is None  # only an in-flight claim has an owner
        lease_at = _utc(lease)
        return (isinstance(attempt_id, str) and bool(_ATTEMPT_ID.fullmatch(attempt_id)) and lease_at is not None
                and lease_at <= now + MAX_LEASE + _CLOCK_SKEW)

    def _load(self, now: datetime) -> tuple[dict[str, dict[str, Any]], bool]:
        """Validated records plus whether the file must be rewritten (corrupt/expired rows)."""
        data = read_json(self.path)
        rows = data.get("records") if isinstance(data, dict) else None
        if (data is not None or self.path.exists()) and not isinstance(rows, list):
            self.corrupt_dropped += 1
            return {}, True
        records: dict[str, dict[str, Any]] = {}
        dirty = False
        for row in rows or []:
            upgraded = _upgrade_legacy(row)
            if upgraded is not row:
                row, dirty = upgraded, True
            if not self._valid(row, now) or row["notification_id"] in records:
                self.corrupt_dropped += 1
                dirty = True
                continue
            records[row["notification_id"]] = dict(row)
        for nid in [nid for nid, row in records.items() if now >= _at(row["expires_at"])]:
            if records.pop(nid)["delivery_status"] in _OPEN:
                self.expired.append(nid)
            dirty = True
        return records, dirty

    def _persist(self, records: dict[str, dict[str, Any]]) -> None:
        for record in records.values():
            assert_record_schema(record, RECORD_KEYS)
        atomic_write_json(self.path, {
            "kind": DataKind.NOTIFICATION_DELIVERY.value,
            "retention_class": policy_for(DataKind.NOTIFICATION_DELIVERY).retention.value,
            "records": list(records.values()),
        })

    # --- operations (each one transaction under the store lock) ---------------------------------

    def records(self) -> list[dict[str, Any]]:
        """Retained, valid records; enforces retention and drops corrupt rows on read."""
        with store_lock(self.path):
            records, dirty = self._load(self._clock())
            if dirty:
                self._persist(records)
            return [dict(record) for record in records.values()]

    def maintain(self) -> int:
        """Enforce retention and drop corrupt records now; returns the number of records kept."""
        return len(self.records())

    def get(self, notification_id: str) -> dict[str, Any] | None:
        for record in self.records():
            if record["notification_id"] == notification_id:
                return record
        return None

    def enqueue(self, notification_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Persist a PENDING record before the first send; idempotent by ``notification_id``.

        Returns ``None`` when the notification is already past its retention window."""
        now = self._clock()
        created = _utc(payload.get("created_at"))
        if (not isinstance(notification_id, str) or not _ID.fullmatch(notification_id)
                or set(payload) - PUSH_ALLOWED_KEYS
                or payload.get("notification_id", notification_id) != notification_id
                or not _member(payload.get("kind"), _KINDS) or not isinstance(payload.get("ref"), str)
                or not _REF.fullmatch(payload["ref"]) or created is None
                or not _EARLIEST <= created <= now + _CLOCK_SKEW):
            raise ValueError("OUTBOX_RECORD_INVALID")
        expires = created + self.retention
        record: dict[str, Any] = {
            "notification_id": notification_id, "kind": str(payload["kind"]), "ref": payload["ref"],
            "created_at": created.isoformat(), "attempts": 0, "last_attempt_at": None,
            "next_attempt_at": max(min(now, expires), created).isoformat(),
            "delivery_status": OutboxStatus.PENDING.value, "expires_at": expires.isoformat(),
            "provider_ref": self.provider_ref, "attempt_id": None, "lease_until": None,
        }
        assert_record_schema(record, RECORD_KEYS)
        with store_lock(self.path):
            records, dirty = self._load(now)
            existing = records.get(notification_id)
            if existing is None and now < expires:
                records[notification_id] = record
                open_ids = sorted((nid for nid, row in records.items() if row["delivery_status"] in _OPEN),
                                  key=lambda nid: str(records[nid]["created_at"]))
                for nid in open_ids[: max(0, len(open_ids) - self.max_records)]:
                    del records[nid]
                dirty = True
            if dirty:
                self._persist(records)
            stored = records.get(notification_id)
            return dict(stored) if stored is not None else None

    def _backoff(self, attempts: int) -> timedelta:
        backoff: timedelta = self.base_backoff * (2 ** min(attempts - 1, 20))
        return min(backoff, self.max_backoff)

    def _due(self, row: dict[str, Any], now: datetime) -> bool:
        if now >= _at(row["expires_at"]):
            return False
        if row["delivery_status"] == OutboxStatus.PENDING.value:
            return _at(row["next_attempt_at"]) <= now
        # IN_FLIGHT whose owner's lease elapsed (crashed or overran): reclaimable.
        return row["delivery_status"] == OutboxStatus.IN_FLIGHT.value and _at(row["lease_until"]) <= now

    def claim(self, notification_id: str | None = None) -> dict[str, Any] | None:
        """Atomically claim ONE due record for one send, immediately before that send.

        Expiry and backoff are re-checked under the store lock at claim time. The
        claim (IN_FLIGHT, attempts + 1, fresh ``attempt_id``, ``lease_until`` =
        now + lease, ``next_attempt_at`` = now + backoff for a later retry) is
        persisted before the caller sends. Without an id the oldest due record wins."""
        with store_lock(self.path):
            now = self._clock()
            records, dirty = self._load(now)
            candidates = sorted((row for nid, row in records.items()
                                 if (notification_id is None or nid == notification_id) and self._due(row, now)),
                                key=lambda row: (str(row["next_attempt_at"]), str(row["notification_id"])))
            claimed = None
            if candidates:
                row = candidates[0]
                expires = _at(row["expires_at"])
                row["attempts"] = min(int(row["attempts"]) + 1, MAX_ATTEMPTS)
                row["last_attempt_at"] = now.isoformat()
                row["delivery_status"] = OutboxStatus.IN_FLIGHT.value
                row["attempt_id"] = secrets.token_hex(16)
                row["lease_until"] = min(now + self.lease, expires).isoformat()
                row["next_attempt_at"] = min(now + self._backoff(row["attempts"]), expires).isoformat()
                claimed = dict(row)
                dirty = True
            if dirty:
                self._persist(records)
            return claimed

    def _settle(self, notification_id: str, attempt_id: str, outcome: AttemptOutcome) -> AttemptOutcome:
        with store_lock(self.path):
            now = self._clock()
            records, dirty = self._load(now)
            record = records.get(notification_id)
            if record is None:
                result = AttemptOutcome.NOT_FOUND
            elif record["delivery_status"] != OutboxStatus.IN_FLIGHT.value or record["attempt_id"] != attempt_id:
                result = AttemptOutcome.STALE_ATTEMPT
            else:
                result = outcome
                if outcome is AttemptOutcome.RENEWED:
                    record["lease_until"] = min(now + self.lease, _at(record["expires_at"])).isoformat()
                else:
                    record["delivery_status"] = (OutboxStatus.DELIVERED if outcome is AttemptOutcome.DELIVERED
                                                 else OutboxStatus.PENDING).value
                    record["attempt_id"] = record["lease_until"] = None
                dirty = True
            if dirty:
                self._persist(records)
            return result

    def complete(self, notification_id: str, attempt_id: str) -> AttemptOutcome:
        """The owning attempt delivered: DELIVERED (never pushed again)."""
        return self._settle(notification_id, attempt_id, AttemptOutcome.DELIVERED)

    def fail(self, notification_id: str, attempt_id: str) -> AttemptOutcome:
        """The owning attempt failed: back to PENDING until ``next_attempt_at``."""
        return self._settle(notification_id, attempt_id, AttemptOutcome.RETRY_SCHEDULED)

    def renew(self, notification_id: str, attempt_id: str) -> AttemptOutcome:
        """Extend the owning attempt's lease (long sends); a stale owner cannot renew."""
        return self._settle(notification_id, attempt_id, AttemptOutcome.RENEWED)

    def drain_expired(self) -> list[str]:
        drained = list(self.expired)
        self.expired.clear()
        return drained

    @staticmethod
    def push_payload(record: dict[str, Any]) -> dict[str, Any]:
        """Metadata-only push body; ``notification_id`` is the receiver's idempotency key."""
        return {"kind": record["kind"], "ref": record["ref"], "count": 1, "created_at": record["created_at"],
                "notification_id": record["notification_id"]}
