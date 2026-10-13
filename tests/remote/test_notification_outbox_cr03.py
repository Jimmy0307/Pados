"""Regression tests for Codex review CR-03 (notification outbox): CC-03, CC-04, CC-05, CC-09, CC-10."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.connectivity.storage import StoreLockTimeout, store_lock
from pados.remote.notifications import (
    PUSH_ALLOWED_KEYS,
    DeliveryStatus,
    Notification,
    NotificationCenter,
    NotificationKind,
)
from pados.remote.outbox import RECORD_KEYS, DurableNotificationOutbox

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Receiver:
    """A push receiver that deduplicates on the idempotency key."""

    def __init__(self) -> None:
        self.up = True
        self.raw: list[dict[str, Any]] = []
        self.unique: dict[str, dict[str, Any]] = {}

    def __call__(self, payload: dict[str, Any]) -> None:
        if not self.up:
            raise ConnectionError("push provider unreachable")
        self.raw.append(payload)
        self.unique.setdefault(payload["notification_id"], payload)


def _note(clock: Clock, subject: str = "run-1") -> Notification:
    return Notification(NotificationKind.APPROVAL_REQUIRED, "proj-secret-name", subject,
                        created_at=clock.now.isoformat())


def _outbox(path: Path, clock: Clock, **kwargs: Any) -> DurableNotificationOutbox:
    return DurableNotificationOutbox(path, clock=clock, **kwargs)


# --- CC-03: idempotency key, atomic claim, crash between send and ack ------------------------------

def test_cr03_cc03_push_payload_carries_opaque_idempotency_key(tmp_path: Path) -> None:
    clock, receiver = Clock(), Receiver()
    center = NotificationCenter(push=receiver, durable=_outbox(tmp_path / "o.json", clock))
    note = _note(clock)
    center.emit(note)
    payload = receiver.raw[0]
    assert set(payload) <= PUSH_ALLOWED_KEYS and payload["notification_id"] == note.notification_id
    assert payload["notification_id"].startswith("nf-") and "proj-secret-name" not in json.dumps(payload)
    plain: list[dict[str, Any]] = []
    NotificationCenter(push=plain.append).emit(note)  # in-memory path carries the same key
    assert plain[0]["notification_id"] == note.notification_id


def test_cr03_cc03_crash_after_send_resends_same_key_after_lease(tmp_path: Path) -> None:
    clock, receiver = Clock(), Receiver()
    path = tmp_path / "o.json"
    outbox = _outbox(path, clock)
    note = _note(clock)
    outbox.enqueue(note.notification_id, note.push_metadata())
    claimed = outbox.claim()
    assert claimed is not None
    receiver(outbox.push_payload(claimed))  # the push succeeded ...
    del outbox  # ... and the Home Base crashed before recording DELIVERED
    restarted = NotificationCenter(push=receiver, durable=_outbox(path, clock))
    assert restarted.flush() == 0 and len(receiver.raw) == 1  # claim lease still running: no resend
    clock.tick(121)  # past the (2 min) lease
    assert restarted.flush() == 1
    assert len(receiver.raw) == 2 and len(receiver.unique) == 1  # at-least-once, deduplicated by key
    assert receiver.raw[0]["notification_id"] == receiver.raw[1]["notification_id"]
    clock.tick(3600)
    assert restarted.flush() == 0 and len(receiver.raw) == 2  # DELIVERED is never pushed again


def test_cr03_cc03_concurrent_claims_send_once(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    note = _note(clock)
    _outbox(path, clock).enqueue(note.notification_id, note.push_metadata())
    instances = [_outbox(path, clock) for _ in range(8)]
    barrier = threading.Barrier(len(instances))
    claimed: list[dict[str, Any] | None] = []
    lock = threading.Lock()

    def race(outbox: DurableNotificationOutbox) -> None:
        barrier.wait()
        result = outbox.claim()
        with lock:
            claimed.append(result)

    threads = [threading.Thread(target=race, args=(o,)) for o in instances]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sum(1 for result in claimed if result is not None) == 1


# --- CC-05: emit honours next_attempt_at and expires_at --------------------------------------------

def test_cr03_cc05_repeated_emit_respects_backoff(tmp_path: Path) -> None:
    clock, receiver = Clock(), Receiver()
    receiver.up = False
    attempts: list[int] = []

    def push(payload: dict[str, Any]) -> None:
        attempts.append(1)
        receiver(payload)

    center = NotificationCenter(push=push, durable=_outbox(tmp_path / "o.json", clock))
    note = _note(clock)
    for _ in range(10):
        assert center.emit(note) is DeliveryStatus.PENDING
    assert len(attempts) == 1  # re-emits inside the backoff window never send
    clock.tick(31)
    center.emit(note)
    assert len(attempts) == 2


def test_cr03_cc05_expired_notification_is_never_sent(tmp_path: Path) -> None:
    clock, receiver = Clock(), Receiver()
    receiver.up = False
    center = NotificationCenter(push=receiver, durable=_outbox(tmp_path / "o.json", clock,
                                                               retention=timedelta(hours=2)))
    note = _note(clock)
    center.emit(note)
    clock.tick(3 * 3600)
    receiver.up = True
    assert center.emit(note) is DeliveryStatus.EXPIRED and receiver.raw == []
    assert center.flush() == 0 and receiver.raw == []


# --- CC-04: cross-process lock; no lost updates between instances ----------------------------------

def test_cr03_cc04_two_instances_never_lose_each_others_updates(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    first, second = _outbox(path, clock), _outbox(path, clock)
    a, b = _note(clock, "run-a"), _note(clock, "run-b")
    first.enqueue(a.notification_id, a.push_metadata())
    second.enqueue(b.notification_id, b.push_metadata())  # second never saw "a" in memory
    claimed_a = first.claim(a.notification_id)
    assert claimed_a is not None
    second.complete(a.notification_id, claimed_a["attempt_id"])  # ack recorded through the other instance
    first.claim(b.notification_id)
    statuses = {r["notification_id"]: r["delivery_status"] for r in first.records()}
    assert statuses == {a.notification_id: "DELIVERED", b.notification_id: "IN_FLIGHT"}
    assert claimed_a["attempts"] == 1


def test_cr03_cc04_lock_excludes_another_process(tmp_path: Path) -> None:
    path = tmp_path / "o.json"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\nfrom pathlib import Path\nfrom pados.connectivity.storage import store_lock\n"
         f"with store_lock(Path({str(path)!r})):\n    print('locked', flush=True)\n    sys.stdin.readline()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(StoreLockTimeout):
            with store_lock(path, timeout=0.3):
                pass
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(10) == 0
        with store_lock(path, timeout=5):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()


def test_cr03_cc04_other_process_enqueue_is_visible(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    mine, theirs = _note(clock, "run-mine"), _note(clock, "run-theirs")
    outbox = _outbox(path, clock)
    outbox.enqueue(mine.notification_id, mine.push_metadata())
    code = (
        "import json, sys\nfrom pathlib import Path\nfrom pados.remote.outbox import DurableNotificationOutbox\n"
        "nid, payload = json.loads(sys.argv[1])\n"
        f"DurableNotificationOutbox(Path({str(path)!r})).enqueue(nid, payload)\n"
    )
    arg = json.dumps([theirs.notification_id, theirs.push_metadata()])
    assert subprocess.run([sys.executable, "-c", code, arg], cwd=ROOT, timeout=30).returncode == 0
    outbox.claim(mine.notification_id)  # reloads under the lock: does not drop the other record
    assert {r["notification_id"] for r in outbox.records()} == {mine.notification_id, theirs.notification_id}


# --- CC-09: strict validation on enqueue and reload ------------------------------------------------

def _valid_row(clock: Clock) -> dict[str, Any]:
    note = _note(clock, "run-valid")
    return {"notification_id": note.notification_id, "kind": "APPROVAL_REQUIRED", "ref": "0123456789abcdef",
            "created_at": clock.now.isoformat(), "attempts": 0, "last_attempt_at": None,
            "next_attempt_at": clock.now.isoformat(), "delivery_status": "PENDING",
            "expires_at": (clock.now + timedelta(days=1)).isoformat(), "provider_ref": "push",
            "attempt_id": None, "lease_until": None}


@pytest.mark.parametrize("forged", [
    {"kind": "Refactor the payroll module"},
    {"ref": "proj-secret-name"},
    {"ref": "0123456789ABCDEF"},
    {"notification_id": "nf-short"},
    {"created_at": "2026-10-02T08:00:00+08:00"},
    {"created_at": "2026-10-02T08:00:00"},
    {"created_at": "yesterday"},
    {"expires_at": "2099-01-01T00:00:00+00:00"},
    {"attempts": True},
    {"attempts": 10**9},
    {"attempts": "3"},
    {"delivery_status": "SENT"},
    {"delivery_status": "EXPIRED"},
    {"last_attempt_at": "garbage"},
    {"next_attempt_at": "2099-01-01T00:00:00+00:00"},
    {"provider_ref": "https://push.example/secret"},
    {"goal": "leak"},
])
def test_cr03_cc09_forged_records_are_dropped_on_reload_and_never_forwarded(tmp_path: Path,
                                                                           forged: dict[str, Any]) -> None:
    clock, receiver = Clock(), Receiver()
    path = tmp_path / "o.json"
    good, bad = _valid_row(clock), {**_valid_row(clock), **forged}
    bad["notification_id"] = forged.get("notification_id", "nf-" + "f" * 32)
    path.write_text(json.dumps({"records": [good, bad]}), encoding="utf-8")
    outbox = _outbox(path, clock)
    assert outbox.corrupt_dropped == 1
    center = NotificationCenter(push=receiver, durable=outbox)
    assert center.flush() == 1 and [p["notification_id"] for p in receiver.raw] == [good["notification_id"]]
    stored = json.loads(path.read_text())["records"]
    assert [r["notification_id"] for r in stored] == [good["notification_id"]]
    assert all(set(r) == set(RECORD_KEYS) for r in stored)


def test_cr03_cc09_non_list_store_is_counted_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"records": "Refactor the payroll module"}), encoding="utf-8")
    outbox = _outbox(path, Clock())
    assert outbox.corrupt_dropped == 1 and outbox.records() == []
    assert "payroll" not in path.read_text()


@pytest.mark.parametrize("change", [
    {"kind": "NOT_A_KIND"}, {"created_at": "2026-10-02T08:00:00+08:00"}, {"created_at": "2026-10-02T08:00:00"},
    {"created_at": "2999-01-01T00:00:00+00:00"}, {"extra": "x"}, {"notification_id": "nf-" + "0" * 32},
    {"ref": 7},
])
def test_cr03_cc09_enqueue_rejects_invalid_metadata(tmp_path: Path, change: dict[str, Any]) -> None:
    clock = Clock()
    note = _note(clock)
    outbox = _outbox(tmp_path / "o.json", clock)
    with pytest.raises(ValueError):
        outbox.enqueue(note.notification_id, {**note.push_metadata(), **change})
    assert outbox.records() == []


def test_cr03_cc09_expiry_is_bounded_by_created_at_plus_r1(tmp_path: Path) -> None:
    clock = Clock()
    note = Notification(NotificationKind.JOB_FAILED, "p", "r",
                        created_at=(clock.now - timedelta(days=13)).isoformat())
    record = _outbox(tmp_path / "o.json", clock).enqueue(note.notification_id, note.push_metadata())
    assert record is not None
    created, expires = datetime.fromisoformat(record["created_at"]), datetime.fromisoformat(record["expires_at"])
    assert expires - created == timedelta(days=14)
    stale = Notification(NotificationKind.JOB_FAILED, "p", "r2",
                         created_at=(clock.now - timedelta(days=15)).isoformat())
    assert _outbox(tmp_path / "o.json", clock).enqueue(stale.notification_id, stale.push_metadata()) is None


# --- CC-10: retention enforced on open and on read --------------------------------------------------

def test_cr03_cc10_quiet_outbox_purges_expired_records_on_open_and_read(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    outbox = _outbox(path, clock)
    for subject in ("run-1", "run-2"):
        note = _note(clock, subject)
        outbox.enqueue(note.notification_id, note.push_metadata())
        claimed = outbox.claim(note.notification_id)
        assert claimed is not None
        outbox.complete(note.notification_id, claimed["attempt_id"])
    clock.tick(15 * 24 * 3600)  # nothing else happens on the system
    _outbox(path, clock)  # Home Base start
    assert json.loads(path.read_text())["records"] == []
    for subject in ("run-3",):
        note = _note(clock, subject)
        outbox.enqueue(note.notification_id, note.push_metadata())
    clock.tick(15 * 24 * 3600)
    assert outbox.records() == [] and json.loads(path.read_text())["records"] == []
