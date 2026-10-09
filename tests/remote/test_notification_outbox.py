"""Connectivity Closure C: durable notification delivery."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.remote.config import RemoteConfig
from pados.remote.notifications import (
    PUSH_ALLOWED_KEYS,
    DeliveryStatus,
    Notification,
    NotificationCenter,
    NotificationKind,
)
from pados.remote.outbox import RECORD_KEYS, DurableNotificationOutbox


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Push:
    def __init__(self) -> None:
        self.up = True
        self.sent: list[dict[str, Any]] = []
        self.attempts = 0

    def __call__(self, payload: dict[str, Any]) -> None:
        self.attempts += 1
        if not self.up:
            raise ConnectionError("push provider unreachable")
        self.sent.append(payload)


def _note(subject: str = "run-1", project: str = "proj-secret-name") -> Notification:
    return Notification(NotificationKind.APPROVAL_REQUIRED, project, subject,
                        detail={"goal": "Refactor the payroll module"}, created_at="2026-10-01T00:00:00+00:00")


def _outbox(tmp_path: Path, clock: Clock, **kwargs: Any) -> DurableNotificationOutbox:
    return DurableNotificationOutbox(tmp_path / "runtime" / "notification-outbox.json", clock=clock, **kwargs)


def test_pending_push_survives_home_base_restart(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    push.up = False
    center = NotificationCenter(push=push, durable=_outbox(tmp_path, clock))
    assert center.emit(_note()) is DeliveryStatus.PENDING and center.pending_count() == 1
    del center  # Home Base restart: in-memory state is gone
    push.up = True
    clock.tick(60)
    restarted = NotificationCenter(push=push, durable=_outbox(tmp_path, clock))
    assert restarted.pending_count() == 1  # loaded on construction
    assert restarted.flush() == 1 and restarted.pending_count() == 0
    assert len(push.sent) == 1 and set(push.sent[0]) <= PUSH_ALLOWED_KEYS


def test_retry_uses_bounded_exponential_backoff(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    push.up = False
    outbox = _outbox(tmp_path, clock, base_backoff=timedelta(seconds=30), max_backoff=timedelta(seconds=120))
    center = NotificationCenter(push=push, durable=outbox)
    nid = _note().notification_id
    center.emit(_note())
    gaps = []
    for _ in range(5):
        record = outbox.get(nid)
        assert record is not None
        gap = datetime.fromisoformat(record["next_attempt_at"]) - datetime.fromisoformat(record["last_attempt_at"])
        gaps.append(gap.total_seconds())
        assert center.flush() == 0  # not due yet: no attempt
        clock.tick(gap.total_seconds())
        center.flush()
    assert gaps == [30, 60, 120, 120, 120]
    record = outbox.get(nid)
    assert record is not None and record["attempts"] == 6 and push.attempts == 6


def test_delivery_is_idempotent_by_notification_id(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    outbox = _outbox(tmp_path, clock)
    center = NotificationCenter(push=push, durable=outbox)
    assert center.emit(_note()) is DeliveryStatus.DELIVERED
    assert center.emit(_note()) is DeliveryStatus.DELIVERED  # same notification re-emitted
    restarted = NotificationCenter(push=push, durable=_outbox(tmp_path, clock))
    assert restarted.emit(_note()) is DeliveryStatus.DELIVERED
    for _ in range(3):
        clock.tick(3600)
        restarted.flush()
    assert len(push.sent) == 1 and push.attempts == 1  # never pushed twice
    record = outbox.get(_note().notification_id)
    assert record is not None and record["delivery_status"] == "DELIVERED"


def test_expired_records_are_purged_and_marked(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    push.up = False
    outbox = _outbox(tmp_path, clock, retention=timedelta(hours=2))
    center = NotificationCenter(push=push, durable=outbox)
    center.emit(_note())
    clock.tick(3 * 3600)
    push.up = True
    assert center.flush() == 0 and push.sent == []  # expired: never delivered late
    assert center.inbox[0]["delivery_status"] == "EXPIRED"
    assert outbox.records() == [] and json.loads(outbox.path.read_text())["records"] == []


def test_default_expiry_is_r1_and_never_longer(tmp_path: Path) -> None:
    clock = Clock()
    assert _outbox(tmp_path, clock).retention == timedelta(days=14)
    assert _outbox(tmp_path, clock, retention=timedelta(days=90)).retention == timedelta(days=14)
    record = _outbox(tmp_path, clock).enqueue(_note().notification_id, _note().push_metadata())
    assert datetime.fromisoformat(record["expires_at"]) - clock.now == timedelta(days=14)


def test_delivered_records_are_purged_after_expiry(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    outbox = _outbox(tmp_path, clock)
    center = NotificationCenter(push=push, durable=outbox)
    center.emit(_note())
    clock.tick(15 * 24 * 3600)
    center.flush()
    assert outbox.records() == []


def test_outbox_file_holds_metadata_only(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    push.up = False
    outbox = _outbox(tmp_path, clock)
    center = NotificationCenter(push=push, durable=outbox)
    for subject in ("run-1", "run-2"):
        center.emit(_note(subject))
    text = outbox.path.read_text()
    for leaked in ("proj-secret-name", "run-1", "run-2", "payroll", "Refactor"):
        assert leaked not in text
    document = json.loads(text)
    assert document["kind"] == "NOTIFICATION_DELIVERY" and document["retention_class"] == "R1_OPERATIONAL"
    assert all(set(record) == set(RECORD_KEYS) for record in document["records"])
    if os.name != "nt":
        assert outbox.path.stat().st_mode & 0o777 == 0o600
    names = sorted(p.name for p in outbox.path.parent.iterdir())
    assert names == ["notification-outbox.json", "notification-outbox.json.lock"]  # lock sidecar is empty
    assert (outbox.path.parent / "notification-outbox.json.lock").read_bytes() == b""


def test_outbox_refuses_non_metadata_records(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path, Clock())
    with pytest.raises(ValueError):
        outbox.enqueue("not-an-id", _note().push_metadata())
    with pytest.raises(ValueError):
        outbox.enqueue(_note().notification_id, {**_note().push_metadata(), "ref": "proj-secret-name"})
    with pytest.raises(ValueError):  # kind must be a NotificationKind (CR-03 CC-09)
        outbox.enqueue(_note().notification_id, {**_note().push_metadata(), "kind": "sk-" + "a" * 24})
    assert not outbox.path.exists()


def test_corrupt_outbox_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "notification-outbox.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert DurableNotificationOutbox(path).records() == []
    path.write_text(json.dumps({"records": [{"notification_id": "nf-x", "goal": "leak"}]}), encoding="utf-8")
    assert DurableNotificationOutbox(path).records() == []


def test_delivery_status_never_touches_job_state(tmp_path: Path) -> None:
    clock, push = Clock(), Push()
    push.up = False
    job_state = {"run-1": "WAITING_APPROVAL"}
    center = NotificationCenter(push=push, durable=_outbox(tmp_path, clock))
    center.emit(_note())
    clock.tick(30)
    center.flush()
    assert job_state == {"run-1": "WAITING_APPROVAL"}


def test_remote_config_outbox_path_default_and_override(tmp_path: Path) -> None:
    base = {"bind_host": "100.64.0.10", "staging_root": str(tmp_path / "staging"),
            "ledger_path": str(tmp_path / "ledger" / "intents.jsonl"), "actors": []}
    assert RemoteConfig.from_mapping(base).notification_outbox_path == tmp_path / "ledger" / "notification-outbox.json"
    custom = RemoteConfig.from_mapping({**base, "notification_outbox_path": str(tmp_path / "o.json")})
    assert custom.notification_outbox_path == tmp_path / "o.json"
    with pytest.raises(ValueError):
        RemoteConfig.from_mapping({**base, "notification_outbox_path": 5})


def test_remote_config_origins_require_exact_secure_origin(tmp_path: Path) -> None:
    base = {"bind_host": "100.64.0.10", "staging_root": str(tmp_path / "staging"),
            "ledger_path": str(tmp_path / "ledger" / "intents.jsonl"), "actors": []}
    config = RemoteConfig.from_mapping({**base, "allowed_origins": ["https://jimmy0307.github.io"]})
    assert config.allowed_origins == ("https://jimmy0307.github.io",)
    with pytest.raises(ValueError, match="REMOTE_CONFIG_ALLOWED_ORIGIN_INVALID"):
        RemoteConfig.from_mapping({**base, "allowed_origins": ["*"]})
    with pytest.raises(ValueError, match="REMOTE_CONFIG_ALLOWED_ORIGIN_INVALID"):
        RemoteConfig.from_mapping({**base, "allowed_origins": ["http://pados.home"]})
    with pytest.raises(ValueError, match="REMOTE_CONFIG_ALLOWED_ORIGIN_INVALID"):
        RemoteConfig.from_mapping({**base, "allowed_origins": ["https://example.test/path"]})
