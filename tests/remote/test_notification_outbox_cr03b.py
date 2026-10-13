"""Regression tests for Codex verification CR-03b (outbox): NEW-01, NEW-02, NEW-03, CC-03, CC-09, CC-10."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.remote import outbox as outbox_module
from pados.remote.notifications import Notification, NotificationCenter, NotificationKind
from pados.remote.outbox import AttemptOutcome, DurableNotificationOutbox

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _note(clock: Clock, subject: str) -> Notification:
    return Notification(NotificationKind.JOB_COMPLETED, "proj", subject, created_at=clock.now.isoformat())


def _box(path: Path, clock: Clock, **kwargs: Any) -> DurableNotificationOutbox:
    return DurableNotificationOutbox(path, clock=clock, **kwargs)


def _enqueue(box: DurableNotificationOutbox, note: Notification) -> str:
    box.enqueue(note.notification_id, note.push_metadata())
    return note.notification_id


def _row(path: Path, nid: str) -> dict[str, Any]:
    rows = {r["notification_id"]: r for r in json.loads(path.read_text())["records"]}
    return dict(rows[nid])


# --- NEW-02: claim immediately before each individual send ---------------------------------------

def test_cr03b_new02_flush_claims_one_record_at_a_time(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    box = _box(path, clock)
    ids = [_enqueue(box, _note(clock, f"run-{i}")) for i in range(4)]
    in_flight_during_send: list[int] = []

    def push(payload: dict[str, Any]) -> None:
        rows = json.loads(path.read_text())["records"]
        in_flight_during_send.append(sum(1 for r in rows if r["delivery_status"] == "IN_FLIGHT"))

    assert NotificationCenter(push=push, durable=box).flush() == len(ids)
    assert in_flight_during_send == [1, 1, 1, 1]  # never a pre-claimed batch


def test_cr03b_new02_expiry_rechecked_at_each_claim(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    box = _box(path, clock, retention=timedelta(hours=2))
    first = _enqueue(box, _note(clock, "run-1"))
    clock.tick(3600)
    second = _enqueue(box, _note(clock, "run-2"))  # expires one hour after the first
    sent: list[str] = []

    def slow_push(payload: dict[str, Any]) -> None:
        sent.append(payload["notification_id"])
        clock.tick(3 * 3600)  # this send is so slow that everything else expires meanwhile

    NotificationCenter(push=slow_push, durable=box).flush()
    assert sent == [first] and second not in sent  # the second record was never claimed late
    assert box.records() == []


def test_cr03b_new02_lease_is_independent_of_backoff_and_renewable(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    owner = _box(path, clock, base_backoff=timedelta(seconds=30), lease=timedelta(seconds=120))
    other = _box(path, clock, base_backoff=timedelta(seconds=30), lease=timedelta(seconds=120))
    nid = _enqueue(owner, _note(clock, "run-1"))
    claim = owner.claim(nid)
    assert claim is not None
    clock.tick(31)  # past the retry backoff, but the in-flight lease still holds
    assert other.claim(nid) is None
    clock.tick(69)
    assert owner.renew(nid, claim["attempt_id"]) is AttemptOutcome.RENEWED  # long send: renew at 100 s
    clock.tick(30)
    assert other.claim(nid) is None  # 130 s: would have been reclaimable without the renewal
    clock.tick(91)
    stolen = other.claim(nid)  # renewed lease (100 s + 120 s) elapsed: owner presumed dead
    assert stolen is not None and stolen["attempt_id"] != claim["attempt_id"]
    assert stolen["attempts"] == 2


def test_cr03b_new02_failed_attempt_retries_after_backoff_not_lease(tmp_path: Path) -> None:
    clock = Clock()
    box = _box(tmp_path / "o.json", clock, base_backoff=timedelta(seconds=30), lease=timedelta(minutes=10))
    nid = _enqueue(box, _note(clock, "run-1"))
    claim = box.claim(nid)
    assert claim is not None and box.fail(nid, claim["attempt_id"]) is AttemptOutcome.RETRY_SCHEDULED
    clock.tick(29)
    assert box.claim(nid) is None
    clock.tick(2)
    assert box.claim(nid) is not None  # 31 s, not the 10 min lease


# --- NEW-03: attempt ownership -----------------------------------------------------------------

def test_cr03b_new03_stale_owner_cannot_reset_a_newer_claim(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    box_a, box_b = _box(path, clock), _box(path, clock)
    nid = _enqueue(box_a, _note(clock, "run-1"))
    attempt_a = box_a.claim(nid)
    assert attempt_a is not None
    clock.tick(121)  # A's lease elapses while A is still (slowly) sending
    attempt_b = box_b.claim(nid)
    assert attempt_b is not None and attempt_b["attempt_id"] != attempt_a["attempt_id"]
    assert box_a.fail(nid, attempt_a["attempt_id"]) is AttemptOutcome.STALE_ATTEMPT  # no-op
    row = _row(path, nid)
    assert row["delivery_status"] == "IN_FLIGHT" and row["attempt_id"] == attempt_b["attempt_id"]
    assert box_a.claim(nid) is None  # B's claim was not reset: no third concurrent attempt
    assert box_a.complete(nid, attempt_a["attempt_id"]) is AttemptOutcome.STALE_ATTEMPT
    assert box_a.renew(nid, attempt_a["attempt_id"]) is AttemptOutcome.STALE_ATTEMPT
    assert box_b.complete(nid, attempt_b["attempt_id"]) is AttemptOutcome.DELIVERED
    assert box_b.fail(nid, attempt_b["attempt_id"]) is AttemptOutcome.STALE_ATTEMPT  # already settled
    assert _row(path, nid)["delivery_status"] == "DELIVERED"


def test_cr03b_new03_attempt_ids_are_random_and_cleared_when_settled(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    box = _box(path, clock, base_backoff=timedelta(seconds=1))
    nid = _enqueue(box, _note(clock, "run-1"))
    seen = set()
    for _ in range(5):
        claim = box.claim(nid)
        assert claim is not None and len(claim["attempt_id"]) == 32
        seen.add(claim["attempt_id"])
        box.fail(nid, claim["attempt_id"])
        row = _row(path, nid)
        assert row["attempt_id"] is None and row["lease_until"] is None
        clock.tick(3600)
    assert len(seen) == 5
    assert box.complete(nid, "0" * 32) is AttemptOutcome.STALE_ATTEMPT
    assert box.complete("nf-" + "e" * 32, "0" * 32) is AttemptOutcome.NOT_FOUND


# --- NEW-01 / CC-09: malformed JSON types never raise ------------------------------------------

def _valid(clock: Clock, nid: str) -> dict[str, Any]:
    return {"notification_id": nid, "kind": "JOB_COMPLETED", "ref": "0123456789abcdef",
            "created_at": clock.now.isoformat(), "attempts": 0, "last_attempt_at": None,
            "next_attempt_at": clock.now.isoformat(), "delivery_status": "PENDING",
            "expires_at": (clock.now + timedelta(days=1)).isoformat(), "provider_ref": "push",
            "attempt_id": None, "lease_until": None}


@pytest.mark.parametrize("forged", [
    {"kind": []}, {"kind": {}}, {"delivery_status": {}}, {"delivery_status": []}, {"notification_id": []},
    {"ref": {}}, {"provider_ref": []}, {"created_at": []}, {"attempts": []}, {"attempt_id": []},
    {"lease_until": {}}, {"last_attempt_at": [1]},
    {"delivery_status": "IN_FLIGHT"},  # in flight without an owner
    {"attempt_id": "a" * 32},  # an owner on a record that is not in flight
    {"delivery_status": "IN_FLIGHT", "attempt_id": "XYZ", "lease_until": "2026-10-02T00:00:00+00:00"},
])
def test_cr03b_new01_unhashable_or_inconsistent_fields_are_dropped(tmp_path: Path, forged: dict[str, Any]) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    good = _valid(clock, "nf-" + "1" * 32)
    bad = {**_valid(clock, "nf-" + "2" * 32), **forged}
    path.write_text(json.dumps({"records": [good, bad]}), encoding="utf-8")
    box = _box(path, clock)  # must not raise
    assert box.corrupt_dropped == 1
    assert [r["notification_id"] for r in box.records()] == [good["notification_id"]]
    sent: list[dict[str, Any]] = []
    assert NotificationCenter(push=sent.append, durable=box).flush() == 1
    assert [p["notification_id"] for p in sent] == [good["notification_id"]]


@pytest.mark.parametrize("kind", [[], {}, 5, None])
def test_cr03b_new01_enqueue_rejects_unhashable_kind_with_value_error(tmp_path: Path, kind: object) -> None:
    clock = Clock()
    note = _note(clock, "run-1")
    with pytest.raises(ValueError):
        _box(tmp_path / "o.json", clock).enqueue(note.notification_id, {**note.push_metadata(), "kind": kind})


# --- CC-10: flush removes expired records from disk even when nothing is due ---------------------

def test_cr03b_cc10_idle_flush_removes_expired_records_from_disk(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "o.json"
    box = _box(path, clock)
    nid = _enqueue(box, _note(clock, "run-1"))
    claim = box.claim(nid)
    assert claim is not None
    box.complete(nid, claim["attempt_id"])
    clock.tick(15 * 24 * 3600)
    center = NotificationCenter(push=lambda _p: None, durable=box)
    assert center.flush() == 0
    assert json.loads(path.read_text())["records"] == []


# --- CC-03: receiver dedupe contract is documented -----------------------------------------------

def test_cr03b_cc03_dedupe_contract_is_documented() -> None:
    doc = outbox_module.__doc__ or ""
    assert "MUST deduplicate on ``notification_id``" in doc and "ACCEPTED_RISK" in doc
    closure = (ROOT / "docs" / "remote" / "CONNECTIVITY_CLOSURE_V1.md").read_text(encoding="utf-8")
    assert "MUST deduplicate" in closure and "ACCEPTED_RISK" in closure
