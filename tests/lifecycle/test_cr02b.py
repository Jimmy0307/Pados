"""Regressions for the Codex CR-02b verification findings (N01-N04, F06/F07, F10)."""
import os
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import pados.lifecycle.fswalk as fswalk
import pados.lifecycle.locking as locking
from pados.lifecycle.purge import (
    LOCK_DIR,
    ItemStatus,
    RecoveryStatus,
    apply_purge,
    artifact_lock,
    plan_purge,
    recover_pending,
)
from pados.lifecycle.rejection import ProofRejectionAuthority, RejectionEvidence, StaticRejectionAuthority
from pados.lifecycle.tombstones import TombstoneLedger
from pados.state_proof.object_store import ObjectStore
from tests.lifecycle.test_rejection_authority import HASH, OBJECT_ID, RESULT_ID, _Engine, _rejected_state
from tests.windows_links import create_link_or_skip

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CONTENT = b"rejected candidate bytes"


def _setup(tmp_path: Path) -> tuple[ObjectStore, Path, RejectionEvidence, TombstoneLedger]:
    store = ObjectStore(tmp_path / "objects")
    ref = store.put_bytes(CONTENT, media_type="text/plain", object_kind="RESULT_PAYLOAD")
    evidence = RejectionEvidence(ref.content_hash, "REJECTED", (NOW - timedelta(days=20)).isoformat(),
                                 "state_proof_results:r-7", "state_proof_results:r-7:FAILED")
    return store, Path(ref.storage_uri), evidence, TombstoneLedger(tmp_path / "ledger" / "tombstones.jsonl")


def _plan(store: ObjectStore, evidence: RejectionEvidence) -> Any:
    return plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root,
                      authority=StaticRejectionAuthority([evidence]))


def _active_dir_type(root: Path) -> type[fswalk.PinnedDir]:
    with fswalk.open_root(root) as pinned:
        return type(pinned)


class _RepublishingAuthority(StaticRejectionAuthority):
    """After reservation, a writer re-publishes the canonical object and acceptance references it."""

    def __init__(self, evidence: RejectionEvidence, payload: Path, content: bytes) -> None:
        super().__init__([evidence])
        self.payload, self.content, self.inode = payload, content, -1

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        if self.payload.exists():
            return False
        self.payload.write_bytes(self.content)
        self.inode = os.lstat(self.payload).st_ino
        return True


@pytest.mark.parametrize("backend", ["descriptor", "path_fallback"])
def test_cr02b_n01_rollback_never_clobbers_a_republished_object(tmp_path: Path, backend: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    if backend == "path_fallback":
        monkeypatch.setattr(fswalk, "FD_SAFE", False)
    store, payload, evidence, ledger = _setup(tmp_path)
    plan = _plan(store, evidence)
    authority = _RepublishingAuthority(evidence, payload, CONTENT)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.RESTORE_CANONICAL_ALREADY_PRESENT
    assert os.lstat(payload).st_ino == authority.inode  # the writer's object, not our reserved inode
    assert payload.read_bytes() == CONTENT and not list(payload.parent.glob("*.purge-pending"))
    assert [e["record"]["phase"] for e in ledger.entries()] == ["PENDING", "ABORTED"]


def test_cr02b_n01_conflicting_canonical_keeps_both_copies(tmp_path: Path) -> None:
    store, payload, evidence, ledger = _setup(tmp_path)
    plan = _plan(store, evidence)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=_RepublishingAuthority(evidence, payload, b"something else"), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.RESTORE_CONFLICT
    assert payload.read_bytes() == b"something else"
    (reserved,) = payload.parent.glob("*.purge-pending")
    assert reserved.read_bytes() == CONTENT


def test_cr02b_n02_artifact_lock_serializes_purges(tmp_path: Path) -> None:
    store, payload, evidence, _ = _setup(tmp_path)
    plan = _plan(store, evidence)
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder() -> None:
        with artifact_lock(store.root, evidence.artifact_hash):
            held.set()
            release.wait(5)

    outcomes: list[Any] = []

    def purger() -> None:
        ledger = TombstoneLedger(tmp_path / "ledger" / "tombstones.jsonl")
        outcomes.append(apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                                    authority=StaticRejectionAuthority([evidence]), tombstones=ledger))
        done.set()

    first = threading.Thread(target=holder)
    first.start()
    assert held.wait(5)
    second = threading.Thread(target=purger)
    second.start()
    time.sleep(0.3)
    assert not done.is_set() and payload.exists()  # blocked on the per-artifact lock
    release.set()
    first.join()
    second.join()
    assert outcomes[0].outcomes[0].status is ItemStatus.PAYLOAD_PURGED
    assert (store.root / LOCK_DIR / f"{evidence.artifact_hash}.lock").is_file()


def test_cr02b_n02_concurrent_applies_purge_exactly_once(tmp_path: Path) -> None:
    store, payload, evidence, _ = _setup(tmp_path)
    plan = _plan(store, evidence)
    statuses: list[ItemStatus] = []

    def run() -> None:
        ledger = TombstoneLedger(tmp_path / "ledger" / "tombstones.jsonl")
        result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                             authority=StaticRejectionAuthority([evidence]), tombstones=ledger)
        statuses.append(result.outcomes[0].status)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == sorted([ItemStatus.PAYLOAD_PURGED] + [ItemStatus.ALREADY_PURGED] * 3)
    ledger = TombstoneLedger(tmp_path / "ledger" / "tombstones.jsonl")
    assert [e["record"]["phase"] for e in ledger.entries()] == ["PENDING", "COMPLETED"]


def test_cr02b_n02_reservation_is_no_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence, ledger = _setup(tmp_path)
    plan = _plan(store, evidence)
    directory_type = _active_dir_type(payload.parent)
    original = directory_type.link
    squatter = payload.parent / f".{evidence.artifact_hash}.purge-pending"

    def racing_link(self: Any, src: str, dst: str) -> None:
        if dst.endswith(".purge-pending"):
            squatter.write_bytes(b"another reservation")  # appears after the pending check
        original(self, src, dst)

    monkeypatch.setattr(directory_type, "link", racing_link)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=StaticRejectionAuthority([evidence]), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.PENDING_RECOVERY_REQUIRED
    assert squatter.read_bytes() == b"another reservation" and payload.read_bytes() == CONTENT
    assert [e["record"]["phase"] for e in ledger.entries()] == ["PENDING", "ABORTED"]


def _interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ObjectStore, Path, Path,
                                                                             RejectionEvidence, TombstoneLedger,
                                                                             Any]:
    store, payload, evidence, ledger = _setup(tmp_path)
    plan = _plan(store, evidence)
    directory_type = _active_dir_type(payload.parent)
    original = directory_type.unlink

    def crash(self: Any, name: str) -> None:
        if name.endswith(".purge-pending"):
            raise OSError("simulated crash")
        original(self, name)

    monkeypatch.setattr(directory_type, "unlink", crash)
    apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=StaticRejectionAuthority([evidence]),
                tombstones=ledger)
    monkeypatch.setattr(directory_type, "unlink", original)
    reserved = payload.parent / f".{evidence.artifact_hash}.purge-pending"
    assert reserved.exists() and not payload.exists()
    return store, payload, reserved, evidence, ledger, plan


def test_cr02b_n03_recovery_is_bound_to_the_reserved_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, reserved, evidence, ledger, plan = _interrupted(tmp_path, monkeypatch)
    (pending,) = ledger.pending()
    assert pending["reserved_ino"] == os.lstat(reserved).st_ino
    replacement = reserved.with_name("replacement.tmp")
    replacement.write_bytes(CONTENT)  # identical bytes, different inode
    os.replace(replacement, reserved)
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=StaticRejectionAuthority([evidence]),
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERY_CONFLICT
    assert reserved.read_bytes() == CONTENT and ledger.pending()  # nothing deleted, still PENDING


def test_cr02b_n03_recovery_completes_for_the_recorded_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, reserved, evidence, ledger, plan = _interrupted(tmp_path, monkeypatch)
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=StaticRejectionAuthority([evidence]),
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERED_COMPLETED and not reserved.exists()
    assert ledger.latest(evidence.artifact_hash)["phase"] == "COMPLETED"  # type: ignore[index]


def test_cr02b_n03_interrupted_reservation_recovers_without_deleting(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence, ledger = _setup(tmp_path)
    plan = _plan(store, evidence)
    directory_type = _active_dir_type(payload.parent)
    original = directory_type.unlink

    def crash(self: Any, name: str) -> None:
        if not name.endswith(".purge-pending"):
            raise OSError("simulated crash after linking, before the canonical name is removed")
        original(self, name)

    monkeypatch.setattr(directory_type, "unlink", crash)
    apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=StaticRejectionAuthority([evidence]),
                tombstones=ledger)
    monkeypatch.setattr(directory_type, "unlink", original)
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=StaticRejectionAuthority([evidence]),
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERED_ABORTED
    assert payload.read_bytes() == CONTENT and not list(payload.parent.glob("*.purge-pending"))


def test_cr02b_n04_rejection_time_is_the_terminal_instant_in_utc() -> None:
    taipei = timezone(timedelta(hours=8))
    early = datetime(2026, 8, 1, 9, 0, tzinfo=taipei)
    late = datetime(2026, 9, 1, 9, 0, tzinfo=taipei)
    state = _rejected_state(results=[
        {"id": RESULT_ID, "status": "FAILED", "created_at": early, "produced": True},
        {"id": "33333333-3333-3333-3333-333333333333", "status": "INVALID_SCHEMA", "created_at": late,
         "produced": True},
    ])
    evidence = ProofRejectionAuthority(_Engine(state)).rejection_evidence(HASH)
    assert evidence is not None
    assert evidence.decided_at == "2026-09-01T01:00:00+00:00"  # latest rejection, normalized to UTC
    assert evidence.receipt_ref == "state_proof_results:33333333-3333-3333-3333-333333333333"
    assert OBJECT_ID


@pytest.mark.parametrize("state", [
    _rejected_state(immutable_trigger=0),
    _rejected_state(results=[{"id": RESULT_ID, "status": "FAILED", "created_at": datetime(2026, 9, 1),
                              "produced": True}]),
    _rejected_state(results=[{"id": RESULT_ID, "status": "FAILED", "created_at": None, "produced": True}]),
], ids=["no_immutability_trigger", "naive_timestamp", "missing_timestamp"])
def test_cr02b_n04_unknown_rejection_time_is_refused(state: dict[str, Any], tmp_path: Path) -> None:
    authority = ProofRejectionAuthority(_Engine(state))
    assert authority.rejection_evidence(HASH) is None
    assert authority.refusal_code(HASH) == "REJECTION_TIME_UNKNOWN"
    (tmp_path / "objects").mkdir()
    plan = plan_purge([], now=NOW, candidates=[HASH], object_root=tmp_path / "objects", authority=authority)
    assert plan.refused == ((HASH, "REJECTION_TIME_UNKNOWN"),)


def test_cr02b_n04_retention_window_compares_utc_instants(tmp_path: Path) -> None:
    store, _, evidence, _ = _setup(tmp_path)
    # 14 days minus one minute before NOW, expressed at +08:00: not yet eligible.
    inside = (NOW - timedelta(days=14) + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8)))
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root,
                      authority=StaticRejectionAuthority([RejectionEvidence(
                          evidence.artifact_hash, "REJECTED", inside.isoformat(), "r:1", "r:1:FAILED")]))
    assert plan.refused == ((evidence.artifact_hash, "PAYLOAD_RETENTION_NOT_ELAPSED"),)


@pytest.mark.skipif(os.name == "nt", reason="post-open name verification requires POSIX dir_fd backend")
def test_cr02b_f07_unlink_verified_refuses_a_swap_after_open(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    target = root / "old.log"
    target.write_bytes(b"old")
    expected = fswalk.FileIdentity.of(os.lstat(target))
    real_open = fswalk.os.open

    def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if path == "old.log":
            replacement = root / "new.tmp"
            replacement.write_bytes(b"new content")
            os.replace(replacement, target)  # swapped after open, before the name check
        return fd

    with fswalk.open_root(root) as pinned:
        monkeypatch.setattr(fswalk.os, "open", swapping_open)
        assert pinned.unlink_verified("old.log", expected) is False
    monkeypatch.undo()
    assert target.read_bytes() == b"new content"
    with fswalk.open_root(root) as pinned:
        assert pinned.unlink_verified("old.log", expected) is False  # identity no longer matches
        assert pinned.unlink_verified("old.log", fswalk.FileIdentity.of(os.lstat(target))) is True
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path-fallback counterpart is Windows-specific")
def test_cr02b_f07_path_fallback_refuses_a_replacement_before_unlink(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    target = root / "old.log"
    target.write_bytes(b"old")
    expected = fswalk.FileIdentity.of(os.lstat(target))
    replacement = root / "new.tmp"
    replacement.write_bytes(b"new content")
    os.replace(replacement, target)
    with fswalk.open_root(root) as pinned:
        assert isinstance(pinned, fswalk._PathDir)
        assert pinned.unlink_verified("old.log", expected) is False
    assert target.read_bytes() == b"new content"


def test_cr02b_f10_new_ancestor_directories_are_made_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(locking, "fsync_directory", lambda path: synced.append(Path(path)))
    TombstoneLedger(tmp_path / "a" / "b" / "c" / "tombstones.jsonl")
    assert synced == [tmp_path, tmp_path / "a", tmp_path / "a" / "b"]
    assert (tmp_path / "a" / "b" / "c").is_dir()
    create_link_or_skip(tmp_path / "a", tmp_path / "linked", directory=True)
    with pytest.raises(fswalk.FsPolicyError, match="LINK"):
        locking.ensure_durable_dir(tmp_path / "linked")
