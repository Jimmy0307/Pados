"""Regressions for the Codex CR-02c verification findings (N05-N07, F03/N04 trigger coverage)."""
import errno
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

import pados.lifecycle.fswalk as fswalk
import pados.lifecycle.purge as purge_module
from pados.execution.context import canonical_hash
from pados.lifecycle.purge import LOCK_DIR, ItemStatus, RecoveryStatus, apply_purge, plan_purge, recover_pending
from pados.lifecycle.rejection import ProofRejectionAuthority, RejectionEvidence, StaticRejectionAuthority
from pados.lifecycle.tombstones import (
    LEGACY_TOMBSTONE_SCHEMA_V2,
    Phase,
    TombstoneError,
    TombstoneLedger,
    tombstone_record,
)
from pados.state_proof.object_store import ObjectStore
from tests.lifecycle.test_rejection_authority import HASH, _Engine, _rejected_state

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
CONTENT = b"rejected candidate bytes"


def _setup(tmp_path: Path) -> tuple[ObjectStore, Path, RejectionEvidence, TombstoneLedger, Any]:
    store = ObjectStore(tmp_path / "objects")
    ref = store.put_bytes(CONTENT, media_type="text/plain", object_kind="RESULT_PAYLOAD")
    evidence = RejectionEvidence(ref.content_hash, "REJECTED", (NOW - timedelta(days=20)).isoformat(),
                                 "state_proof_results:r-7", "state_proof_results:r-7:FAILED")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root,
                      authority=StaticRejectionAuthority([evidence]))
    return store, Path(ref.storage_uri), evidence, TombstoneLedger(tmp_path / "ledger" / "t.jsonl"), plan


def _active_dir_type(root: Path) -> type[fswalk.PinnedDir]:
    with fswalk.open_root(root) as pinned:
        return type(pinned)


def _interrupt(store: ObjectStore, evidence: RejectionEvidence, ledger: TombstoneLedger, plan: Any,
               monkeypatch: pytest.MonkeyPatch) -> Path:
    directory_type = _active_dir_type(store.root)
    original = directory_type.unlink

    def crash(self: Any, name: str) -> None:
        if name.endswith(".purge-pending"):
            raise OSError("simulated crash")
        original(self, name)

    monkeypatch.setattr(directory_type, "unlink", crash)
    apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=StaticRejectionAuthority([evidence]),
                tombstones=ledger)
    monkeypatch.setattr(directory_type, "unlink", original)
    reserved = store.root / evidence.artifact_hash[:2] / evidence.artifact_hash[2:4] / \
        f".{evidence.artifact_hash}.purge-pending"
    assert reserved.exists()
    return reserved


def _reappend(ledger: TombstoneLedger, record: dict[str, Any], phase: Phase, plan_hash: str) -> None:
    ledger.append(tombstone_record(
        phase=phase, artifact_hash=record["artifact_hash"], status=record["status"], timestamp=record["timestamp"],
        receipt_ref=record["receipt_ref"], decision_evidence_ref=record["decision_evidence_ref"],
        recorded_at=NOW.isoformat(), reason="CONCURRENT_OPERATION", plan_hash=plan_hash,
        reserved=(record["reserved_dev"], record["reserved_ino"], record["reserved_size"])))


@pytest.mark.parametrize("other_plan", [True, False], ids=["other_plan", "same_plan_new_entry"])
def test_cr02c_n05_recovery_rechecks_the_record_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               other_plan: bool) -> None:
    store, _, evidence, ledger, plan = _setup(tmp_path)
    reserved = _interrupt(store, evidence, ledger, plan, monkeypatch)
    original_lock = purge_module.artifact_lock

    @contextmanager
    def racing_lock(object_root: Path, artifact_hash: str) -> Iterator[None]:
        with original_lock(object_root, artifact_hash):
            # While recovery waited for the lock, another operation replaced the PENDING record.
            (pending,) = ledger.pending()
            _reappend(ledger, pending, Phase.ABORTED, pending["plan_hash"])
            _reappend(ledger, pending, Phase.PENDING, "e" * 64 if other_plan else pending["plan_hash"])
            yield

    monkeypatch.setattr(purge_module, "artifact_lock", racing_lock)
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=StaticRejectionAuthority([evidence]),
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERY_PLAN_MISMATCH
    assert reserved.read_bytes() == CONTENT  # nothing touched
    assert ledger.latest(evidence.artifact_hash)["phase"] == "PENDING"  # type: ignore[index]


class _FlipAuthority(StaticRejectionAuthority):
    def __init__(self, evidence: RejectionEvidence, payload: Path) -> None:
        super().__init__([evidence])
        self.payload = payload

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        return not self.payload.exists()


def test_cr02c_n06_restore_runs_under_the_artifact_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence, ledger, plan = _setup(tmp_path)
    lock_file = store.root / LOCK_DIR / f"{evidence.artifact_hash}.lock"
    directory_type = _active_dir_type(store.root)
    original = directory_type.link
    observed: list[bool] = []

    def probing_link(self: Any, src: str, dst: str) -> None:
        fd = os.open(lock_file, os.O_RDWR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(False)  # lock was free: mutation outside the lock
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            busy = {errno.EWOULDBLOCK, errno.EAGAIN}
            if sys.platform == "win32":
                busy.update({errno.EACCES, errno.EDEADLK})
            observed.append(exc.errno in busy or getattr(exc, "winerror", None) == 33)
        finally:
            os.close(fd)
        original(self, src, dst)

    monkeypatch.setattr(directory_type, "link", probing_link)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=_FlipAuthority(evidence, payload), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.REFUSED_REFERENCED_AFTER_RESERVE
    assert observed == [True, True]  # reservation link and restore link both under the lock
    assert payload.read_bytes() == CONTENT


def test_cr02c_n06_restore_refuses_a_pending_replaced_before_link(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence, ledger, plan = _setup(tmp_path)
    directory_type = _active_dir_type(store.root)
    original = directory_type.link
    reserved = payload.parent / f".{evidence.artifact_hash}.purge-pending"

    def swapping_link(self: Any, src: str, dst: str) -> None:
        if dst == evidence.artifact_hash:  # restore: swap the pending name after it was verified
            replacement = payload.parent / "replacement.tmp"
            replacement.write_bytes(b"unrelated bytes")
            os.replace(replacement, reserved)
        original(self, src, dst)

    monkeypatch.setattr(directory_type, "link", swapping_link)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=_FlipAuthority(evidence, payload), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.RESTORE_CONFLICT
    assert not payload.exists()  # unrelated bytes were never left published under the artifact hash
    assert reserved.read_bytes() == b"unrelated bytes"
    assert ledger.latest(evidence.artifact_hash)["phase"] == "PENDING"  # type: ignore[index]


def test_cr02c_n06_failed_cleanup_keeps_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence, ledger, plan = _setup(tmp_path)
    directory_type = _active_dir_type(store.root)
    original = directory_type.unlink_verified
    state = {"restoring": False}
    original_link = directory_type.link

    def marking_link(self: Any, src: str, dst: str) -> None:
        state["restoring"] = dst == evidence.artifact_hash
        original_link(self, src, dst)

    def failing_cleanup(self: Any, name: str, expected: Any) -> bool:
        if state["restoring"] and name.endswith(".purge-pending"):
            return False
        return bool(original(self, name, expected))

    monkeypatch.setattr(directory_type, "link", marking_link)
    monkeypatch.setattr(directory_type, "unlink_verified", failing_cleanup)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=_FlipAuthority(evidence, payload), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.RESTORE_CLEANUP_FAILED
    assert payload.read_bytes() == CONTENT
    assert [e["record"]["phase"] for e in ledger.entries()] == ["PENDING"]  # no ABORTED for a failed step


def _v2_record(artifact_hash: str, phase: Phase) -> dict[str, Any]:
    record = tombstone_record(phase=phase, artifact_hash=artifact_hash, status="REJECTED",
                              timestamp="2026-09-01T00:00:00+00:00", receipt_ref="state_proof_results:r-1",
                              decision_evidence_ref="state_proof_results:r-1:FAILED",
                              recorded_at="2026-09-20T00:00:00+00:00", reason="REJECTED_PAYLOAD_RETENTION_ELAPSED",
                              plan_hash="d" * 64, reserved=(0, 0, 0))
    for key in ("reserved_dev", "reserved_ino", "reserved_size", "reserved_sha256"):
        del record[key]
    record["schema"] = LEGACY_TOMBSTONE_SCHEMA_V2
    return record


def _write_chain(path: Path, records: list[dict[str, Any]]) -> None:
    prev = "0" * 64
    lines = []
    for record in records:
        entry: dict[str, Any] = {"prev_hash": prev, "record": record}
        entry["entry_hash"] = canonical_hash(entry)
        prev = entry["entry_hash"]
        lines.append(json.dumps(entry, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cr02c_n07_v2_history_is_readable_and_legacy_pending_is_never_auto_recovered(
        tmp_path: Path) -> None:
    store, payload, evidence, ledger, plan = _setup(tmp_path)
    other = "a" * 64
    _write_chain(ledger.path, [_v2_record(other, Phase.PENDING), _v2_record(other, Phase.COMPLETED),
                               _v2_record(evidence.artifact_hash, Phase.PENDING)])
    assert ledger.verify() and len(ledger.entries()) == 3
    with pytest.raises(TombstoneError, match="LEGACY_SCHEMA_READ_ONLY"):
        ledger.append(_v2_record(other, Phase.ABORTED))
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=StaticRejectionAuthority([evidence]),
                                 approved_plan_hash="d" * 64, now=NOW)
    assert outcome.status is RecoveryStatus.LEGACY_PENDING_REQUIRES_OPERATOR
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=StaticRejectionAuthority([evidence]), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.PENDING_RECOVERY_REQUIRED
    assert payload.read_bytes() == CONTENT
    # New writes continue the verified chain as v3.
    ledger.append(tombstone_record(phase=Phase.ABORTED, artifact_hash=other, status="REJECTED",
                                   timestamp="2026-09-01T00:00:00+00:00", receipt_ref="state_proof_results:r-1",
                                   decision_evidence_ref="state_proof_results:r-1:FAILED",
                                   recorded_at=NOW.isoformat(), reason="OPERATOR_REVIEWED", plan_hash="d" * 64,
                                   reserved=(0, 0, 0)))
    assert ledger.verify() and ledger.entries()[-1]["record"]["schema"].endswith(".v3")


def test_cr02c_n07_malformed_v2_records_are_still_refused(tmp_path: Path) -> None:
    ledger = TombstoneLedger(tmp_path / "t.jsonl")
    record = _v2_record("a" * 64, Phase.COMPLETED)
    _write_chain(ledger.path, [{**record, "reserved_ino": 5}])  # v2 schema with v3-only keys
    assert not ledger.verify()
    _write_chain(ledger.path, [{**record, "payload_state": "PRESENT"}])
    assert not ledger.verify()


def test_cr02c_f03_trigger_must_cover_update_and_delete_on_the_queried_table() -> None:
    engine = _Engine(_rejected_state())
    assert ProofRejectionAuthority(engine).rejection_evidence(HASH) is not None
    (trigger_sql,) = [s for c in engine.connections for s in c.statements if "pg_trigger" in s]
    for clause in ("c.relname = 'state_proof_results'", "ns.nspname = current_schema()",
                   "t.tgname = 'state_proof_results_immutable'", "NOT t.tgisinternal", "t.tgenabled IN ('O', 'A')",
                   "(t.tgtype::int & 1) = 1", "(t.tgtype::int & 2) = 2", "(t.tgtype::int & 8) = 8",
                   "(t.tgtype::int & 16) = 16", "p.proname = 'reject_state_proof_immutable_mutation'"):
        assert clause in trigger_sql, clause
    refused = ProofRejectionAuthority(_Engine(_rejected_state(immutable_trigger=0)))
    assert refused.refusal_code(HASH) == "REJECTION_TIME_UNKNOWN"
