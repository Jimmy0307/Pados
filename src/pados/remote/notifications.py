# SPDX-License-Identifier: AGPL-3.0-only
"""Notification abstraction.

In-app notifications may carry the bounded projection row. Anything sent to an
external push provider carries metadata only (kind, opaque reference, count):
no goal text, file names, project names, context or results.

With a ``DurableNotificationOutbox`` pending pushes survive a Home Base
restart and delivery is at-least-once with the opaque ``notification_id`` as
idempotency key; without one the original in-memory behaviour is unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pados.execution.context import canonical_hash

if TYPE_CHECKING:
    from pados.remote.outbox import DurableNotificationOutbox


class NotificationKind(StrEnum):
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    JOB_FAILED = "JOB_FAILED"
    JOB_COMPLETED = "JOB_COMPLETED"
    UPLOAD_QUARANTINED = "UPLOAD_QUARANTINED"


# ``notification_id`` is an opaque hash used by receivers as the idempotency key.
PUSH_ALLOWED_KEYS = frozenset({"kind", "ref", "count", "created_at", "notification_id"})


@dataclass(frozen=True)
class Notification:
    kind: NotificationKind
    project_id: str
    subject_id: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def in_app(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "project_id": self.project_id,
            "subject_id": self.subject_id,
            "detail": dict(self.detail),
            "created_at": self.created_at,
        }

    @property
    def notification_id(self) -> str:
        """Deterministic opaque id: the same notification always maps to the same id."""
        return "nf-" + canonical_hash({"k": self.kind.value, "p": self.project_id, "s": self.subject_id,
                                       "c": self.created_at})[:32]

    def push_metadata(self) -> dict[str, Any]:
        # Opaque reference: the client resolves it in-app after authenticating.
        return {
            "kind": self.kind.value,
            "ref": canonical_hash({"p": self.project_id, "s": self.subject_id})[:16],
            "count": 1,
            "created_at": self.created_at,
            "notification_id": self.notification_id,
        }


PushSender = Callable[[dict[str, Any]], None]


class DeliveryStatus(StrEnum):
    """NOTIFICATION_DELIVERY_STATUS: independent of, and never affecting, JOB_STATUS."""

    IN_APP_ONLY = "IN_APP_ONLY"
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    EXPIRED = "EXPIRED"


class NotificationCenter:
    """In-app inbox plus an optional metadata-only push outbox.

    When the push provider (or WAN) is unavailable the push stays PENDING locally
    and is retried by ``flush()`` after recovery. Delivery failure is recorded on
    the notification only; it is never a job failure. With ``durable`` the
    pending pushes are persisted, reloaded on construction (restart), retried
    with bounded backoff and dropped on expiry.
    """

    def __init__(self, push: PushSender | None = None, *, max_pending: int = 500,
                 durable: DurableNotificationOutbox | None = None) -> None:
        self._push = push
        self._max_pending = max_pending
        self._durable = durable
        self._entries: dict[str, dict[str, Any]] = {}
        self.inbox: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        if durable is not None:
            durable.maintain()

    def _send(self, payload: dict[str, Any]) -> bool:
        if set(payload) - PUSH_ALLOWED_KEYS:
            raise ValueError("PUSH_PAYLOAD_NOT_METADATA_ONLY")
        if self._push is None:
            return False
        try:
            self._push(payload)
        except (OSError, ConnectionError, TimeoutError):
            return False
        return True

    def emit(self, notification: Notification) -> DeliveryStatus:
        entry = {**notification.in_app(), "delivery_status": DeliveryStatus.IN_APP_ONLY.value}
        self.inbox.append(entry)
        if self._push is None:
            return DeliveryStatus.IN_APP_ONLY
        payload = notification.push_metadata()
        if self._durable is not None:
            return self._emit_durable(self._durable, notification.notification_id, payload, entry)
        if self._send(payload):
            entry["delivery_status"] = DeliveryStatus.DELIVERED.value
            return DeliveryStatus.DELIVERED
        entry["delivery_status"] = DeliveryStatus.PENDING.value
        self.outbox.append({"payload": payload, "entry": entry})
        del self.outbox[: max(0, len(self.outbox) - self._max_pending)]
        return DeliveryStatus.PENDING

    def _emit_durable(self, durable: DurableNotificationOutbox, notification_id: str,
                      payload: dict[str, Any], entry: dict[str, Any]) -> DeliveryStatus:
        self._entries[notification_id] = entry
        if durable.enqueue(notification_id, payload) is not None:  # persisted before any send
            record = durable.claim(notification_id)  # re-checks next_attempt_at / expires_at
            if record is not None:
                self._deliver(durable, record)
        status = self._status_of(durable.get(notification_id))
        entry["delivery_status"] = status.value
        self._mark_expired(durable)
        return status

    @staticmethod
    def _status_of(record: dict[str, Any] | None) -> DeliveryStatus:
        if record is None:
            return DeliveryStatus.EXPIRED
        return DeliveryStatus.DELIVERED if record["delivery_status"] == "DELIVERED" else DeliveryStatus.PENDING

    def _deliver(self, durable: DurableNotificationOutbox, record: dict[str, Any]) -> bool:
        notification_id, attempt_id = str(record["notification_id"]), str(record["attempt_id"])
        delivered = self._send(durable.push_payload(record))
        if not delivered:
            durable.fail(notification_id, attempt_id)
            return False
        from pados.remote.outbox import AttemptOutcome  # runtime import: outbox imports this module

        outcome = durable.complete(notification_id, attempt_id)
        if outcome is not AttemptOutcome.DELIVERED:
            # A newer attempt owns the record, or it expired meanwhile: the durable record,
            # not this send, decides what is reported, so nothing is counted as delivered here.
            return False
        if (entry := self._entries.get(notification_id)) is not None:
            entry["delivery_status"] = DeliveryStatus.DELIVERED.value
        return True

    def _mark_expired(self, durable: DurableNotificationOutbox) -> None:
        for notification_id in durable.drain_expired():
            entry = self._entries.pop(notification_id, None)
            if entry is not None:
                entry["delivery_status"] = DeliveryStatus.EXPIRED.value

    def _flush_durable(self, durable: DurableNotificationOutbox) -> int:
        """One record at a time: claim immediately before each send, never a batch."""
        delivered = 0
        for _ in range(durable.max_records + 1):
            record = durable.claim()
            if record is None:
                break
            delivered += self._deliver(durable, record)
        durable.maintain()  # removes expired records from disk even when nothing was due
        self._mark_expired(durable)
        return delivered

    def pending_count(self) -> int:
        if self._durable is not None:
            return sum(1 for r in self._durable.records() if r["delivery_status"] != "DELIVERED")
        return len(self.outbox)

    def flush(self) -> int:
        """Retry pending pushes (e.g. after WAN recovery); returns how many were delivered."""
        if self._durable is not None:
            return self._flush_durable(self._durable)
        remaining, delivered = [], 0
        for item in self.outbox:
            if self._send(item["payload"]):
                item["entry"]["delivery_status"] = DeliveryStatus.DELIVERED.value
                delivered += 1
            else:
                remaining.append(item)
        self.outbox = remaining
        return delivered
