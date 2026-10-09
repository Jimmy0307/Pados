import ast
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import pados.lifecycle.fswalk as fswalk
import pados.lifecycle.purge as purge_module
from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.lifecycle.accounting import PurgeUnit, build_locations
from pados.lifecycle.purge import (
    MAX_STAGED_MEMBERS,
    ItemStatus,
    PurgeError,
    PurgeItem,
    PurgeScope,
    RecoveryStatus,
    apply_purge,
    plan_purge,
    recover_pending,
    scopes_from_locations,
)
from pados.lifecycle.rejection import RejectionEvidence, StaticRejectionAuthority
from pados.lifecycle.retention import DataKind, PurgeRule, RetentionClass, RetentionPolicy
from pados.lifecycle.tombstones import TombstoneLedger
from pados.remote.staging import StagingArea
from pados.state_proof.object_store import ObjectStore
from tests.windows_links import create_link_or_skip

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _write(path: Path, data: bytes = b"x" * 10, *, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _age(path, age)
    return path


def _age(path: Path, age: timedelta) -> None:
    stamp = (NOW - age).timestamp()
    try:
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    except NotImplementedError:
        if path.is_symlink():
            return  # this test does not depend on a link's own timestamp
        os.utime(path, (stamp, stamp))


@pytest.fixture(params=["descriptor", "path_fallback"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Every purge test runs on the POSIX descriptor backend and on the Windows path fallback."""
    if request.param == "descriptor" and sys.platform == "win32":
        pytest.skip("POSIX descriptor backend requires dir_fd; Windows path_fallback cases cover semantics")
    if request.param == "path_fallback":
        monkeypatch.setattr(fswalk, "FD_SAFE", False)
    return str(request.param)


@pytest.fixture
def logs(tmp_path: Path, backend: str) -> PurgeScope:
    root = tmp_path / "logs"
    _write(root / "old.log", age=timedelta(days=20))
    _write(root / "nested" / "older.log", age=timedelta(days=25))
    _write(root / "fresh.log", age=timedelta(days=2))
    return PurgeScope("runtime_logs", root, DataKind.OPERATIONAL_LOG)


def test_plan_is_deterministic_dry_run(logs: PurgeScope) -> None:
    plan = plan_purge([logs], now=NOW)
    assert plan.plan_hash == plan_purge([logs], now=NOW).plan_hash == plan.compute_hash()
    assert [i.relpath for i in plan.items] == ["nested/older.log", "old.log"]
    assert all(i.rule is PurgeRule.DELETE_RECORD and i.retention is RetentionClass.R1_OPERATIONAL
               and i.identity is not None for i in plan.items)
    assert plan.items[1].age_seconds == 20 * 86400
    data = plan.to_dict()
    assert data["dry_run"] is True and data["autonomous_deletion"] is False
    assert data["summary"] == {"items": 2, "bytes": 20, "by_kind": {"OPERATIONAL_LOG": {"items": 2, "bytes": 20}}}
    json.dumps(data)
    assert (logs.root / "old.log").exists()
    assert plan_purge([logs], now=NOW + timedelta(seconds=1)).plan_hash != plan.plan_hash


@pytest.mark.parametrize("kind", [DataKind.RELEASE_TEST_EVIDENCE, DataKind.PROJECT_SOURCE, DataKind.ACTIVE_ARTIFACT,
                                  DataKind.REMOTE_INTENT_LEDGER, DataKind.AUDIT_CHAIN,
                                  DataKind.REJECTED_ARTIFACT_PAYLOAD, DataKind.PAYLOAD_TOMBSTONE])
def test_r2_and_r3_scopes_are_refused(tmp_path: Path, kind: DataKind) -> None:
    with pytest.raises(PurgeError, match="PURGE_SCOPE_NOT_PURGEABLE"):
        plan_purge([PurgeScope("evidence", tmp_path, kind)], now=NOW)


def test_scopes_may_not_overlap_protected_or_each_other(tmp_path: Path) -> None:
    objects = tmp_path / "objects"
    objects.mkdir()
    with pytest.raises(PurgeError, match="OVERLAPS_PROTECTED"):
        plan_purge([PurgeScope("cache", objects / "tmp", DataKind.CACHE_ENTRY)], now=NOW, object_root=objects)
    with pytest.raises(PurgeError, match="OVERLAPS_PROTECTED"):
        plan_purge([PurgeScope("cache", tmp_path, DataKind.CACHE_ENTRY)], now=NOW, protected=[objects])
    with pytest.raises(PurgeError, match="PURGE_SCOPES_OVERLAP"):
        plan_purge([PurgeScope("a", tmp_path, DataKind.CACHE_ENTRY),
                    PurgeScope("b", tmp_path / "x", DataKind.CACHE_ENTRY)], now=NOW)


def test_configured_root_that_is_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write(real / "c.bin", age=timedelta(days=3))
    create_link_or_skip(real, tmp_path / "linked", directory=True)
    with pytest.raises(PurgeError, match="PURGE_SCOPE_ROOT_IS_SYMLINK"):
        plan_purge([PurgeScope("cache", tmp_path / "linked", DataKind.CACHE_ENTRY)], now=NOW)
    with pytest.raises(PurgeError, match="OBJECT_ROOT_IS_SYMLINK"):
        plan_purge([], now=NOW, object_root=tmp_path / "linked")


def test_apply_requires_the_exact_approved_hash(logs: PurgeScope) -> None:
    plan = plan_purge([logs], now=NOW)
    for wrong in ("", "0" * 64, plan.plan_hash.upper(), plan.plan_hash[:-1], "é" * 64):
        with pytest.raises(PurgeError, match="PLAN_HASH_NOT_APPROVED"):
            apply_purge(plan, approved_plan_hash=wrong, now=NOW)
    tampered = replace(plan, items=plan.items[:1])
    with pytest.raises(PurgeError, match="PLAN_HASH_MISMATCH"):
        apply_purge(tampered, approved_plan_hash=plan.plan_hash, now=NOW)
    assert (logs.root / "old.log").exists() and (logs.root / "nested" / "older.log").exists()


def test_apply_deletes_expired_items_and_is_idempotent(logs: PurgeScope) -> None:
    plan = plan_purge([logs], now=NOW)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert [o.status for o in result.outcomes] == [ItemStatus.DELETED, ItemStatus.DELETED]
    assert result.bytes_freed == 20
    assert not (logs.root / "old.log").exists() and (logs.root / "fresh.log").exists()
    again = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert {o.status for o in again.outcomes} == {ItemStatus.ALREADY_ABSENT} and again.bytes_freed == 0


def test_apply_never_deletes_r2_or_r3_even_in_a_hash_consistent_plan(logs: PurgeScope) -> None:
    plan = plan_purge([logs], now=NOW)
    target = logs.root / "old.log"
    ident = next(i.identity for i in plan.items if i.relpath == "old.log")
    forged = [
        PurgeItem(logs.scope_id, "old.log", DataKind.ACTIVE_ARTIFACT, RetentionClass.R2_PROJECT,
                  PurgeRule.DELETE_RECORD, 10, 0, 9999999, identity=ident),
        PurgeItem(logs.scope_id, "old.log", DataKind.AUDIT_CHAIN, RetentionClass.R3_AUTHORITY,
                  PurgeRule.DELETE_RECORD, 10, 0, 9999999, identity=ident),
        PurgeItem("rejected_payloads", "old.log", DataKind.RELEASE_TEST_EVIDENCE, RetentionClass.R2_PROJECT,
                  PurgeRule.PURGE_PAYLOAD_KEEP_METADATA, 10, 0, 9999999, identity=ident),
    ]
    bad = replace(plan, items=tuple(forged))
    bad = replace(bad, plan_hash=bad.compute_hash())
    result = apply_purge(bad, approved_plan_hash=bad.plan_hash, now=NOW)
    assert {o.status for o in result.outcomes} == {ItemStatus.REFUSED_AUTHORITATIVE}
    assert target.exists()


# --- CR-02 regressions: filesystem races and identity binding -----------------------


def test_cr02_f01_intermediate_directory_swapped_for_link_before_apply(logs: PurgeScope, tmp_path: Path) -> None:
    plan = plan_purge([logs], now=NOW)
    outside = tmp_path / "outside"
    _write(outside / "older.log", age=timedelta(days=25))
    (logs.root / "nested").rename(logs.root / "nested-moved")
    create_link_or_skip(outside, logs.root / "nested", directory=True)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    statuses = {o.relpath: o.status for o in result.outcomes}
    assert statuses["nested/older.log"] is ItemStatus.REFUSED_OUTSIDE_ROOT
    assert (outside / "older.log").exists()


def test_cr02_f01_swap_after_validation_cannot_redirect_unlink(logs: PurgeScope, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    plan = plan_purge([logs], now=NOW)
    outside = tmp_path / "outside"
    _write(outside / "older.log", age=timedelta(days=25))
    target_class = fswalk._PathDir if backend == "path_fallback" else fswalk._FdDir
    original = target_class.unlink

    def racing_unlink(self: Any, name: str) -> None:
        if name == "older.log" and not (logs.root / "nested-moved").exists():
            (logs.root / "nested").rename(logs.root / "nested-moved")  # swap after the identity check
            create_link_or_skip(outside, logs.root / "nested", directory=True)
        original(self, name)

    monkeypatch.setattr(target_class, "unlink", racing_unlink)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert (outside / "older.log").exists()  # never the external file
    status = {o.relpath: o.status for o in result.outcomes}["nested/older.log"]
    if backend == "descriptor":
        assert status is ItemStatus.DELETED and not (logs.root / "nested-moved" / "older.log").exists()
    else:
        assert status is ItemStatus.REFUSED_OUTSIDE_ROOT  # ACCEPTED_RISK_WINDOWS fallback detects the move


def test_cr02_f05_apply_uses_the_plans_bound_policy(logs: PurgeScope) -> None:
    long_policy = RetentionPolicy.configured(r1_days=30)
    plan = plan_purge([logs], now=NOW, policy=RetentionPolicy.configured(r1_days=7))
    assert plan.policy().r1 == timedelta(days=7)
    with pytest.raises(PurgeError, match="POLICY_MISMATCH"):
        apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, policy=long_policy)
    assert len(plan_purge([logs], now=NOW, policy=long_policy).items) == 0
    shorter = plan_purge([logs], now=NOW, policy=RetentionPolicy.configured(r1_days=7))
    result = apply_purge(shorter, approved_plan_hash=shorter.plan_hash, now=NOW)
    assert {o.status for o in result.outcomes} == {ItemStatus.DELETED}


def test_cr02_f07_replaced_file_with_restored_mtime_is_not_deleted(logs: PurgeScope) -> None:
    plan = plan_purge([logs], now=NOW)
    target = logs.root / "old.log"
    mtime = os.lstat(target).st_mtime_ns
    replacement = logs.root / "replacement.tmp"
    replacement.write_bytes(b"new project content")
    os.utime(replacement, ns=(mtime, mtime))
    os.replace(replacement, target)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert {o.relpath: o.status for o in result.outcomes}["old.log"] is ItemStatus.SKIPPED_CHANGED
    assert target.read_bytes() == b"new project content"


# --- staged records ------------------------------------------------------------------


def _staging(tmp_path: Path) -> tuple[StagingArea, str, str, PurgeScope]:
    area = StagingArea(tmp_path / "staging", project_ceiling=lambda _: DataClassification.CONFIDENTIAL)
    staged = area.stage(project_id="p", actor_id="a", name="notes.txt", content=b"hello", classification="INTERNAL")
    quarantined = area.stage(project_id="p", actor_id="a", name="tool.exe", content=b"MZ", classification="INTERNAL")
    for record in (staged, quarantined):
        _age(tmp_path / "staging" / record.staged_id / "record.json", timedelta(days=2))
    scope = PurgeScope("remote_staging", tmp_path / "staging", DataKind.TEMPORARY_UPLOAD, PurgeUnit.STAGED_RECORD)
    return area, staged.staged_id, quarantined.staged_id, scope


def test_staging_records_follow_their_own_class(tmp_path: Path, backend: str) -> None:
    area, staged, quarantined, scope = _staging(tmp_path)
    plan = plan_purge([scope], now=NOW)
    # R0 temporary upload (24h) is due; the R1 quarantined upload (14d) is not.
    assert [(i.relpath, i.data_kind) for i in plan.items] == [(staged, DataKind.TEMPORARY_UPLOAD)]
    assert [name for name, _ in plan.items[0].members] == ["content.bin", "record.json"]
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert result.outcomes[0].status is ItemStatus.DELETED and result.bytes_freed > 0
    assert area.get(staged) is None and area.get(quarantined) is not None


def test_cr02_f06_unapproved_file_added_to_a_staged_record_is_never_deleted(tmp_path: Path, backend: str,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    _, staged, _, scope = _staging(tmp_path)
    plan = plan_purge([scope], now=NOW)
    added = tmp_path / "staging" / staged / "added-later.bin"
    added.write_bytes(b"unapproved")
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert result.outcomes[0].status is ItemStatus.SKIPPED_CHANGED
    assert added.exists() and (tmp_path / "staging" / staged / "content.bin").exists()
    added.unlink()

    # An entry that appears after the membership check survives as well.
    target_class = fswalk._PathDir if backend == "path_fallback" else fswalk._FdDir
    original = target_class.unlink

    def adding_unlink(self: Any, name: str) -> None:
        original(self, name)
        if name == "record.json":
            added.write_bytes(b"late")

    monkeypatch.setattr(target_class, "unlink", adding_unlink)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert result.outcomes[0].status is ItemStatus.PARTIAL_DIRECTORY_RETAINED
    assert added.read_bytes() == b"late"


def test_cr02_f11_staged_records_are_bounded_and_read_without_following_links(tmp_path: Path, backend: str) -> None:
    _, staged, _, scope = _staging(tmp_path)
    directory = tmp_path / "staging" / staged
    for index in range(MAX_STAGED_MEMBERS):
        (directory / f"extra-{index}.bin").write_bytes(b"x")
    assert plan_purge([scope], now=NOW).items == ()  # too many members: not a clean record
    for index in range(MAX_STAGED_MEMBERS):
        (directory / f"extra-{index}.bin").unlink()
    record = directory / "record.json"
    record.write_text(json.dumps({"state": "STAGED", "pad": "y" * (70 * 1024)}), encoding="utf-8")
    _age(record, timedelta(days=2))
    assert plan_purge([scope], now=NOW).items == ()  # record above the hard byte cap
    record.unlink()
    if os.name == "nt":
        # File symlink creation may require an elevated privilege; a junction is
        # an unprivileged Windows reparse point and exercises the same no-follow
        # boundary through the actual _PathDir implementation.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        create_link_or_skip(elsewhere, record, directory=True)
    else:
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps({"state": "STAGED"}), encoding="utf-8")
        create_link_or_skip(elsewhere, record, directory=False)
        _age(record, timedelta(days=2))
    assert plan_purge([scope], now=NOW).items == ()  # linked record is never read


def test_cr02_f11_top_level_enumeration_is_budgeted(tmp_path: Path, backend: str) -> None:
    root = tmp_path / "cache"
    for index in range(50):
        _write(root / f"f{index:02d}.bin", age=timedelta(days=3))
    plan = plan_purge([PurgeScope("cache", root, DataKind.CACHE_ENTRY)], now=NOW, max_entries=5)
    assert plan.truncated and len(plan.items) == 5
    stats = fswalk.WalkStats(max_entries=5)
    assert len(list(fswalk.walk_files(root, max_entries=5, stats=stats))) == 5
    assert stats.entries == 5 and stats.truncated


# --- rejected payloads ----------------------------------------------------------------


def _payload(tmp_path: Path, *, decided: timedelta = timedelta(days=20)) -> tuple[ObjectStore, Path,
                                                                                 RejectionEvidence]:
    store = ObjectStore(tmp_path / "objects")
    ref = store.put_bytes(b"rejected candidate bytes", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    evidence = RejectionEvidence(ref.content_hash, "REJECTED", (NOW - decided).isoformat(),
                                 "state_proof_results:r-7", "state_proof_results:r-7:FAILED")
    return store, Path(ref.storage_uri), evidence


def test_rejected_payload_purge_is_two_phase_and_idempotent(tmp_path: Path, backend: str) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    (item,) = plan.items
    assert item.evidence == evidence and item.retention is RetentionClass.R3_AUTHORITY
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.PAYLOAD_PURGED and result.tombstones_written == 2
    assert not payload.exists() and not list(payload.parent.glob("*.purge-pending"))
    phases = [e["record"]["phase"] for e in ledger.entries()]
    assert phases == ["PENDING", "COMPLETED"]
    tombstone = ledger.latest(evidence.artifact_hash)
    assert tombstone is not None
    assert {k: tombstone[k] for k in ("artifact_hash", "status", "timestamp", "receipt_ref",
                                      "decision_evidence_ref", "purged_at", "payload_state", "plan_hash")} == {
        "artifact_hash": evidence.artifact_hash, "status": "REJECTED", "timestamp": evidence.decided_at,
        "receipt_ref": evidence.receipt_ref, "decision_evidence_ref": evidence.decision_evidence_ref,
        "purged_at": NOW.isoformat(), "payload_state": "METADATA_RETAINED_PAYLOAD_PURGED",
        "plan_hash": plan.plan_hash,
    }
    again = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert again.outcomes[0].status is ItemStatus.ALREADY_PURGED and again.tombstones_written == 0


def test_cr02_f03_caller_cannot_assert_rejection(tmp_path: Path) -> None:
    store, payload, evidence = _payload(tmp_path)
    nobody = StaticRejectionAuthority()
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=nobody)
    assert plan.items == () and plan.refused == ((evidence.artifact_hash, "ARTIFACT_NOT_PROVEN_REJECTED"),)
    # A hand-built plan carrying forged evidence is re-checked against the authority at apply time.
    st = os.lstat(payload)
    forged_item = PurgeItem("rejected_payloads", "/".join((evidence.artifact_hash[:2], evidence.artifact_hash[2:4],
                                                          evidence.artifact_hash)),
                            DataKind.REJECTED_ARTIFACT_PAYLOAD, RetentionClass.R3_AUTHORITY,
                            PurgeRule.PURGE_PAYLOAD_KEEP_METADATA, st.st_size, st.st_mtime_ns, 99,
                            identity=fswalk.FileIdentity.of(st), evidence=evidence)
    forged = replace(plan, items=(forged_item,))
    forged = replace(forged, plan_hash=forged.compute_hash())
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    result = apply_purge(forged, approved_plan_hash=forged.plan_hash, now=NOW, authority=nobody, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.REFUSED_NOT_REJECTED
    assert payload.exists() and ledger.entries() == []
    with pytest.raises(PurgeError, match="REQUIRES_OBJECT_ROOT_AND_AUTHORITY"):
        plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root)


def test_referenced_and_recent_payloads_are_refused_at_plan(tmp_path: Path) -> None:
    store, payload, evidence = _payload(tmp_path)
    referenced = StaticRejectionAuthority([evidence], referenced=[evidence.artifact_hash])
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=referenced)
    assert plan.refused == ((evidence.artifact_hash, "PAYLOAD_REFERENCED_BY_ACCEPTED_STATE"),)
    recent = replace(evidence, decided_at=(NOW - timedelta(days=3)).isoformat())
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root,
                      authority=StaticRejectionAuthority([recent]))
    assert plan.refused == ((evidence.artifact_hash, "PAYLOAD_RETENTION_NOT_ELAPSED"),)
    assert payload.exists()


def test_payload_accepted_after_planning_is_refused_at_apply(tmp_path: Path) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    authority.referenced.add(evidence.artifact_hash)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.PAYLOAD_REFERENCED_BY_ACCEPTED_STATE
    assert payload.exists() and ledger.entries() == []
    with pytest.raises(PurgeError, match="REQUIRES_AUTHORITY_AND_TOMBSTONES"):
        apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority)


class _FlipAuthority(StaticRejectionAuthority):
    """Becomes referenced once the payload has been reserved (acceptance racing the purge)."""

    def __init__(self, evidence: RejectionEvidence, payload: Path) -> None:
        super().__init__([evidence])
        self.payload = payload

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        return not self.payload.exists()  # True only after the canonical name was renamed away


def test_cr02_f04_reference_appearing_after_reserve_rolls_back(tmp_path: Path, backend: str) -> None:
    store, payload, evidence = _payload(tmp_path)
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root,
                      authority=StaticRejectionAuthority([evidence]))
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                         authority=_FlipAuthority(evidence, payload), tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.REFUSED_REFERENCED_AFTER_RESERVE
    assert payload.read_bytes() == b"rejected candidate bytes"
    assert not list(payload.parent.glob("*.purge-pending"))
    assert [e["record"]["phase"] for e in ledger.entries()] == ["PENDING", "ABORTED"]
    assert ledger.latest(evidence.artifact_hash)["payload_state"] == "PRESENT"  # type: ignore[index]


def test_tampered_payload_bytes_are_not_purged(tmp_path: Path) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    with payload.open("r+b") as handle:  # same inode, same size, same mtime, different bytes
        handle.write(b"R")
    item = plan.items[0]
    os.utime(payload, ns=(item.mtime_ns, item.mtime_ns))
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.REFUSED_HASH_MISMATCH
    assert payload.exists() and ledger.entries() == []


def test_cr02_f08_forged_tombstone_does_not_authorize_purge(tmp_path: Path) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    forged = {"prev_hash": "0" * 64, "entry_hash": "f" * 64,
              "record": {"artifact_hash": evidence.artifact_hash, "phase": "COMPLETED"}}
    ledger.path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert result.outcomes[0].status is ItemStatus.REFUSED_TOMBSTONE_CHAIN_INVALID
    assert payload.exists()


def test_cr02_f10_interrupted_purge_requires_explicit_recovery(tmp_path: Path, backend: str,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    target_class = fswalk._PathDir if backend == "path_fallback" else fswalk._FdDir
    original = target_class.unlink

    def crash(self: Any, name: str) -> None:
        if name.endswith(".purge-pending"):
            raise OSError("simulated crash before the reserved copy is unlinked")
        original(self, name)

    monkeypatch.setattr(target_class, "unlink", crash)
    first = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert first.outcomes[0].status is ItemStatus.FAILED_IO
    reserved = payload.parent / f".{evidence.artifact_hash}.purge-pending"
    assert reserved.exists() and not payload.exists()
    assert [r["artifact_hash"] for r in ledger.pending()] == [evidence.artifact_hash]
    monkeypatch.setattr(target_class, "unlink", original)
    again = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    assert again.outcomes[0].status is ItemStatus.PENDING_RECOVERY_REQUIRED and reserved.exists()
    assert recover_pending(ledger, object_root=store.root, authority=authority, approved_plan_hash="0" * 64,
                           now=NOW)[0].status is RecoveryStatus.NOT_APPROVED
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=authority,
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERED_COMPLETED
    assert not reserved.exists() and ledger.pending() == [] and ledger.verify()
    assert ledger.latest(evidence.artifact_hash)["phase"] == "COMPLETED"  # type: ignore[index]


def test_cr02_f10_recovery_rolls_back_when_now_referenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, payload, evidence = _payload(tmp_path)
    authority = StaticRejectionAuthority([evidence])
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    plan = plan_purge([], now=NOW, candidates=[evidence.artifact_hash], object_root=store.root, authority=authority)
    with fswalk.open_root(payload.parent) as pinned:
        directory_type = type(pinned)
    original = directory_type.unlink

    def crash(self: Any, name: str) -> None:
        if name.endswith(".purge-pending"):
            raise OSError("simulated crash before the reserved copy is unlinked")
        original(self, name)

    monkeypatch.setattr(directory_type, "unlink", crash)
    apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW, authority=authority, tombstones=ledger)
    monkeypatch.setattr(directory_type, "unlink", original)
    authority.referenced.add(evidence.artifact_hash)
    (outcome,) = recover_pending(ledger, object_root=store.root, authority=authority,
                                 approved_plan_hash=plan.plan_hash, now=NOW)
    assert outcome.status is RecoveryStatus.RECOVERED_ABORTED
    assert payload.read_bytes() == b"rejected candidate bytes"
    assert ledger.latest(evidence.artifact_hash)["phase"] == "ABORTED"  # type: ignore[index]


# --- registry integration ---------------------------------------------------------------


def test_tombstone_ledger_is_never_inside_a_purge_scope(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write(root / "c.bin", age=timedelta(days=3))
    plan = plan_purge([PurgeScope("cache", root, DataKind.CACHE_ENTRY)], now=NOW)
    with pytest.raises(PurgeError, match="OVERLAPS_PROTECTED"):
        apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW,
                    tombstones=TombstoneLedger(root / "tombstones.jsonl"))


def test_scopes_from_accounting_locations(tmp_path: Path) -> None:
    remote = tmp_path / "remote.json"
    remote.write_text(json.dumps({"bind_host": "100.64.0.10", "staging_root": str(tmp_path / "staging"),
                                  "ledger_path": str(tmp_path / "ledger.jsonl"), "actors": []}), encoding="utf-8")
    settings = Settings(_env_file=None, PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "objects"),  # type: ignore[call-arg]
                        PADOS_REMOTE_CONFIG=str(remote))
    scopes, protected = scopes_from_locations(build_locations(settings, runtime_root=tmp_path / "runtime"))
    assert {s.scope_id: s.unit for s in scopes} == {"remote_staging": PurgeUnit.STAGED_RECORD,
                                                    "runtime_logs": PurgeUnit.FILE, "runtime_cache": PurgeUnit.FILE}
    assert set(protected) == {tmp_path / "objects", tmp_path / "ledger.jsonl",
                              tmp_path / "runtime" / "artifacts" / "reports"}
    assert plan_purge(scopes, now=NOW, protected=protected).items == ()


def test_injected_sandbox_and_single_file_locations(tmp_path: Path) -> None:
    connectivity = tmp_path / "connectivity.json"
    connectivity.write_text(json.dumps({"snapshot_path": str(tmp_path / "conn.json")}), encoding="utf-8")
    (tmp_path / "conn.json").write_text("{}", encoding="utf-8")
    settings = Settings(_env_file=None, PADOS_CONNECTIVITY_CONFIG=str(connectivity))  # type: ignore[call-arg]
    locations = build_locations(settings, runtime_root=tmp_path / "runtime", sandbox_roots=[tmp_path / "sbx"],
                                tombstone_ledger=tmp_path / "tombstones.jsonl")
    scopes, protected = scopes_from_locations(locations)
    assert "connectivity_snapshot" not in {s.scope_id for s in scopes}
    assert {s.scope_id: s.kind for s in scopes}["sandbox_0"] is DataKind.SANDBOX_WORKING_DATA
    assert tmp_path / "tombstones.jsonl" in protected
    with pytest.raises(PurgeError, match="ROOT_NOT_DIRECTORY"):
        plan_purge([PurgeScope("snapshot", tmp_path / "conn.json", DataKind.CONNECTIVITY_HISTORY)], now=NOW)


def test_io_failure_is_reported_per_item(logs: PurgeScope, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    plan = plan_purge([logs], now=NOW)
    target_class = fswalk._PathDir if backend == "path_fallback" else fswalk._FdDir

    def refuse(self: Any, name: str) -> None:
        raise PermissionError("EACCES")

    monkeypatch.setattr(target_class, "unlink", refuse)
    result = apply_purge(plan, approved_plan_hash=plan.plan_hash, now=NOW)
    assert {o.status for o in result.outcomes} == {ItemStatus.FAILED_IO} and result.bytes_freed == 0


def test_purge_module_has_no_scheduler() -> None:
    tree = ast.parse(Path(purge_module.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"threading", "sched", "asyncio", "signal", "subprocess", "multiprocessing"}
