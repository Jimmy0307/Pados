"""Codex CR-03c regressions: legacy outbox records survive the upgrade; a stale completion is not a delivery."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pados.remote.notifications import Notification, NotificationCenter, NotificationKind
from pados.remote.outbox import DurableNotificationOutbox, OutboxStatus


def _legacy_row(nid: str, status: str, now: datetime) -> dict[str, object]:
    return {"notification_id": nid, "kind": "JOB_FAILED", "ref": "0123456789abcdef",
            "created_at": now.isoformat(), "attempts": 1, "last_attempt_at": now.isoformat(),
            "next_attempt_at": now.isoformat(), "delivery_status": status,
            "expires_at": (now + timedelta(days=1)).isoformat(), "provider_ref": "push"}


def test_cr03c_new04_legacy_records_are_upgraded_not_dropped(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    path = tmp_path / "outbox.json"
    path.write_text(json.dumps({"records": [_legacy_row("nf-" + "a" * 32, "PENDING", now),
                                            _legacy_row("nf-" + "b" * 32, "IN_FLIGHT", now)]}), encoding="utf-8")
    outbox = DurableNotificationOutbox(path)
    records = {r["notification_id"]: r for r in outbox.records()}
    assert outbox.corrupt_dropped == 0
    assert set(records) == {"nf-" + "a" * 32, "nf-" + "b" * 32}
    assert all(r["delivery_status"] == OutboxStatus.PENDING.value and r["attempt_id"] is None
               for r in records.values())


def test_cr03c_new05_stale_completion_is_not_reported_as_delivered(tmp_path: Path, monkeypatch) -> None:
    from pados.remote.outbox import AttemptOutcome

    now = [datetime.now(UTC)]
    outbox = DurableNotificationOutbox(tmp_path / "outbox.json", clock=lambda: now[0])
    sent: list[dict[str, object]] = []
    up = [False]

    def push(payload: dict[str, object]) -> None:
        if not up[0]:
            raise ConnectionError("push provider unreachable")
        sent.append(payload)

    center = NotificationCenter(push, durable=outbox)
    center.emit(Notification(NotificationKind.JOB_COMPLETED, "p", "r"))
    assert center.inbox[-1]["delivery_status"] == "PENDING"
    up[0], now[0] = True, now[0] + timedelta(hours=2)
    # A newer attempt took ownership while this send was in progress.
    monkeypatch.setattr(outbox, "complete", lambda nid, attempt_id: AttemptOutcome.STALE_ATTEMPT)
    assert center.flush() == 0
    assert len(sent) == 1
    assert center.inbox[-1]["delivery_status"] != "DELIVERED"
