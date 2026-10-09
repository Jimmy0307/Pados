# SPDX-License-Identifier: AGPL-3.0-only
"""Purge semantics: deterministic dry-run plans, hash-approved application.

``plan_purge`` never deletes. It lists candidate items with their DataKind,
retention class, age, purge rule and on-disk identity (device, inode, size,
mtime; for staged records the exact member manifest) and binds them, the
retention windows and every refusal in a ``plan_hash``. Only two kinds of item
can appear: ``DELETE_RECORD`` items of non-authoritative R0/R1 scopes whose
window elapsed, and ``PURGE_PAYLOAD_KEEP_METADATA`` items for the bytes of an
artifact that the ``RejectionAuthority`` (not the caller) proves rejected and
unreferenced, once ``payload_retention`` elapsed since the rejection.

``apply_purge`` runs only with the exact plan hash (explicit operator approval)
and only under the plan's own retention windows. It re-validates every item
through descriptor-pinned, no-follow access (``pados.lifecycle.fswalk``):
identity unchanged, still expired, still inside its root, still not
authoritative, staged membership unchanged; every unlink is identity-verified
(``unlink_verified``). A rejected payload is re-checked against the authority
and then, under a per-artifact process-safe lock, recorded as a PENDING
tombstone bound to the reserved inode, reserved by a no-clobber hard link to a
purge-pending name in its own directory, re-checked again and only then
unlinked; a reference that appears after reservation is rolled back without
ever overwriting a canonical object (``REFUSED_REFERENCED_AFTER_RESERVE`` /
``RESTORE_CANONICAL_ALREADY_PRESENT`` / ``RESTORE_CONFLICT``). R3 metadata, R2 content,
CAS, ScopeVersion and the proof database are never touched. Nothing here
schedules itself: a purge runs only when explicitly invoked.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.execution.context import canonical_hash
from pados.lifecycle.accounting import PurgeUnit, StorageLocation
from pados.lifecycle.fswalk import (
    DEFAULT_MAX_ENTRIES,
    FileIdentity,
    FsPolicyError,
    PinnedDir,
    WalkStats,
    is_link,
    open_root,
    overlaps,
    read_capped,
    real_root,
    walk_files,
)
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.rejection import RejectionAuthority, RejectionEvidence
from pados.lifecycle.retention import (
    DataKind,
    DataKindPolicy,
    PurgeRule,
    RetentionClass,
    RetentionPolicy,
    policy_for,
)
from pados.lifecycle.tombstones import TOMBSTONE_SCHEMA, Phase, TombstoneError, TombstoneLedger, tombstone_record

PLAN_SCHEMA = "pados.lifecycle.purge-plan.v2"
PAYLOAD_SCOPE_ID = "rejected_payloads"
PENDING_SUFFIX = ".purge-pending"
MAX_STAGED_MEMBERS = 8
_SCOPE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGED_DIR = re.compile(r"^stg-[0-9a-f]{20}$")
_RECORD = "record.json"
_MAX_RECORD_BYTES = 64 * 1024
_STAGED_KIND = {"QUARANTINED": DataKind.QUARANTINED_UPLOAD, "STAGED": DataKind.TEMPORARY_UPLOAD}


class PurgeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ItemStatus(StrEnum):
    DELETED = "DELETED"
    PAYLOAD_PURGED = "PAYLOAD_PURGED"
    ALREADY_ABSENT = "ALREADY_ABSENT"
    ALREADY_PURGED = "ALREADY_PURGED"
    SKIPPED_NOT_EXPIRED = "SKIPPED_NOT_EXPIRED"
    SKIPPED_CHANGED = "SKIPPED_CHANGED"
    PARTIAL_DIRECTORY_RETAINED = "PARTIAL_DIRECTORY_RETAINED"
    REFUSED_AUTHORITATIVE = "REFUSED_AUTHORITATIVE"
    REFUSED_OUTSIDE_ROOT = "REFUSED_OUTSIDE_ROOT"
    REFUSED_NOT_REGULAR = "REFUSED_NOT_REGULAR"
    REFUSED_NOT_REJECTED = "REFUSED_NOT_REJECTED"
    REFUSED_HASH_MISMATCH = "REFUSED_HASH_MISMATCH"
    REFUSED_REFERENCED_AFTER_RESERVE = "REFUSED_REFERENCED_AFTER_RESERVE"
    PAYLOAD_REFERENCED_BY_ACCEPTED_STATE = "PAYLOAD_REFERENCED_BY_ACCEPTED_STATE"
    PENDING_RECOVERY_REQUIRED = "PENDING_RECOVERY_REQUIRED"
    REFUSED_TOMBSTONE_CHAIN_INVALID = "REFUSED_TOMBSTONE_CHAIN_INVALID"
    RESTORE_CANONICAL_ALREADY_PRESENT = "RESTORE_CANONICAL_ALREADY_PRESENT"
    RESTORE_CONFLICT = "RESTORE_CONFLICT"
    RESTORE_CLEANUP_FAILED = "RESTORE_CLEANUP_FAILED"
    FAILED_IO = "FAILED_IO"


class RecoveryStatus(StrEnum):
    RECOVERED_COMPLETED = "RECOVERED_COMPLETED"
    RECOVERED_ABORTED = "RECOVERED_ABORTED"
    RECOVERY_CONFLICT = "RECOVERY_CONFLICT"
    RECOVERY_PLAN_MISMATCH = "RECOVERY_PLAN_MISMATCH"
    RESTORE_CLEANUP_FAILED = "RESTORE_CLEANUP_FAILED"
    LEGACY_PENDING_REQUIRES_OPERATOR = "LEGACY_PENDING_REQUIRES_OPERATOR"
    NOT_APPROVED = "NOT_APPROVED"


@dataclass(frozen=True)
class PurgeScope:
    """A PADOS-owned R0/R1 root whose expired items may be deleted."""

    scope_id: str
    root: Path
    kind: DataKind
    unit: PurgeUnit = PurgeUnit.FILE

    def to_dict(self) -> dict[str, Any]:
        return {"scope_id": self.scope_id, "root": str(real_root(self.root)), "kind": self.kind.value,
                "unit": self.unit.value}


Member = tuple[str, FileIdentity]


@dataclass(frozen=True)
class PurgeItem:
    scope_id: str
    relpath: str
    data_kind: DataKind
    retention: RetentionClass
    rule: PurgeRule
    size_bytes: int
    mtime_ns: int
    age_seconds: int
    identity: FileIdentity | None = None  # file items and payloads
    members: tuple[Member, ...] = ()  # staged records: exact approved manifest
    dir_identity: tuple[int, int] | None = None  # staged records: (st_dev, st_ino)
    evidence: RejectionEvidence | None = None  # payloads: authority-derived

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "relpath": self.relpath,
            "data_kind": self.data_kind.value,
            "retention": self.retention.value,
            "rule": self.rule.value,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "age_seconds": self.age_seconds,
            "identity": None if self.identity is None else self.identity.to_dict(),
            "members": [{"name": name, "identity": ident.to_dict()} for name, ident in self.members],
            "dir_identity": None if self.dir_identity is None else list(self.dir_identity),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
        }


@dataclass(frozen=True)
class PurgePlan:
    generated_at: str
    r0_seconds: int
    r1_seconds: int
    explicit_override: bool
    scopes: tuple[PurgeScope, ...]
    object_root: str | None
    protected: tuple[str, ...]
    items: tuple[PurgeItem, ...]
    refused: tuple[tuple[str, str], ...]  # (artifact hash, reason) of payload candidates not planned
    truncated: bool
    plan_hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "generated_at": self.generated_at,
            "r0_seconds": self.r0_seconds,
            "r1_seconds": self.r1_seconds,
            "explicit_override": self.explicit_override,
            "scopes": [s.to_dict() for s in self.scopes],
            "object_root": self.object_root,
            "protected": list(self.protected),
            "items": [i.to_dict() for i in self.items],
            "refused": [list(r) for r in self.refused],
            "truncated": self.truncated,
        }

    def compute_hash(self) -> str:
        return canonical_hash(self.body())

    def policy(self) -> RetentionPolicy:
        try:
            return RetentionPolicy(r0=timedelta(seconds=self.r0_seconds), r1=timedelta(seconds=self.r1_seconds),
                                   explicit_override=self.explicit_override)
        except ValueError as exc:
            raise PurgeError("PLAN_POLICY_INVALID") from exc

    def to_dict(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, int]] = {}
        for item in self.items:
            row = by_kind.setdefault(item.data_kind.value, {"items": 0, "bytes": 0})
            row["items"] += 1
            row["bytes"] += item.size_bytes
        return {**self.body(), "plan_hash": self.plan_hash, "dry_run": True, "autonomous_deletion": False,
                "summary": {"items": len(self.items), "bytes": sum(i.size_bytes for i in self.items),
                            "by_kind": by_kind}}


@dataclass(frozen=True)
class ItemOutcome:
    scope_id: str
    relpath: str
    data_kind: DataKind
    status: ItemStatus
    bytes_freed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"scope_id": self.scope_id, "relpath": self.relpath, "data_kind": self.data_kind.value,
                "status": self.status.value, "bytes_freed": self.bytes_freed}


@dataclass(frozen=True)
class PurgeResult:
    plan_hash: str
    applied_at: str
    outcomes: tuple[ItemOutcome, ...]
    tombstones_written: int

    @property
    def bytes_freed(self) -> int:
        return sum(o.bytes_freed for o in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash, "applied_at": self.applied_at, "bytes_freed": self.bytes_freed,
                "tombstones_written": self.tombstones_written,
                "outcomes": [o.to_dict() for o in self.outcomes]}


# --- policy helpers ----------------------------------------------------------------


def _deletable(policy: DataKindPolicy) -> bool:
    return (policy.purge_rule is PurgeRule.DELETE_RECORD and not policy.authoritative
            and policy.retention in {RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL})


def _window(kind: DataKind, retention: RetentionPolicy) -> timedelta:
    window = retention.window(policy_for(kind).retention)
    if window is None:
        raise PurgeError("DATA_KIND_HAS_NO_WINDOW")
    return window


def payload_window(retention: RetentionPolicy, kind: DataKind = DataKind.REJECTED_ARTIFACT_PAYLOAD) -> timedelta:
    """Payload retention for a PURGE_PAYLOAD_KEEP_METADATA kind (R1 for rejected payloads)."""
    policy = policy_for(kind)
    if policy.purge_rule is not PurgeRule.PURGE_PAYLOAD_KEEP_METADATA:
        raise PurgeError("DATA_KIND_PAYLOAD_NOT_PURGEABLE")
    window = retention.window(policy.payload_retention or RetentionClass.R1_OPERATIONAL)
    if window is None:
        raise PurgeError("PAYLOAD_RETENTION_HAS_NO_WINDOW")
    return window


def _parse_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def _expired_ns(now: datetime, mtime_ns: int, window: timedelta) -> bool:
    return int(now.timestamp() * 1e9) - mtime_ns > window.total_seconds() * 1e9


def _age_seconds(now: datetime, mtime_ns: int) -> int:
    return (int(now.timestamp() * 1e9) - mtime_ns) // 1_000_000_000


def _lstat_link(path: Path) -> bool:
    try:
        return is_link(os.lstat(path))
    except FileNotFoundError:
        return False


def _validate_scopes(scopes: Sequence[PurgeScope], protected: Sequence[Path]) -> None:
    ids = [s.scope_id for s in scopes]
    if len(ids) != len(set(ids)) or PAYLOAD_SCOPE_ID in ids:
        raise PurgeError("PURGE_SCOPE_DUPLICATE")
    for scope in scopes:
        if not _SCOPE_ID.fullmatch(scope.scope_id):
            raise PurgeError("PURGE_SCOPE_ID_INVALID")
        kinds = tuple(_STAGED_KIND.values()) if scope.unit is PurgeUnit.STAGED_RECORD else (scope.kind,)
        if not all(_deletable(policy_for(kind)) for kind in kinds):
            raise PurgeError("PURGE_SCOPE_NOT_PURGEABLE")
        if _lstat_link(scope.root):
            raise PurgeError("PURGE_SCOPE_ROOT_IS_SYMLINK")
        if scope.root.is_file():
            raise PurgeError("PURGE_SCOPE_ROOT_NOT_DIRECTORY")
        for guarded in protected:
            if overlaps(scope.root, guarded):
                raise PurgeError("PURGE_SCOPE_OVERLAPS_PROTECTED")
    for left in scopes:
        for right in scopes:
            if left is not right and overlaps(left.root, right.root):
                raise PurgeError("PURGE_SCOPES_OVERLAP")


def scopes_from_locations(locations: Iterable[StorageLocation]) -> tuple[list[PurgeScope], list[Path]]:
    """Split accounting locations into purge scopes (R0/R1 directories) and protected roots."""
    scopes: list[PurgeScope] = []
    protected: list[Path] = []
    order = list(RetentionClass)
    for loc in locations:
        if loc.root is None:
            continue
        if loc.purge_unit is PurgeUnit.STAGED_RECORD:
            scopes.append(PurgeScope(loc.location_id, loc.root, DataKind.TEMPORARY_UPLOAD, PurgeUnit.STAGED_RECORD))
        elif loc.kinds and all(_deletable(policy_for(kind)) for kind in loc.kinds):
            # A deletable single file (connectivity snapshot) is overwritten in place, never purged.
            if not loc.single_file:
                # Mixed R0/R1 kinds: the longest-lived kind governs the scope (conservative).
                kind = max(loc.kinds, key=lambda k: order.index(policy_for(k).retention))
                scopes.append(PurgeScope(loc.location_id, loc.root, kind))
        else:
            protected.append(loc.root)
    return scopes, protected


# --- staged records ----------------------------------------------------------------


@dataclass(frozen=True)
class _Staged:
    kind: DataKind
    record: FileIdentity
    members: tuple[Member, ...]
    dir_identity: tuple[int, int]


def _staged_snapshot(root: PinnedDir, name: str) -> _Staged | None:
    """Bounded, no-follow snapshot of one staging record; None if it is not a clean record."""
    st = root.lstat(name)
    if st is None or is_link(st) or not stat.S_ISDIR(st.st_mode):
        return None
    try:
        directory = root.child(name)
    except (FsPolicyError, OSError):
        return None
    with directory:
        if directory.identity() != (st.st_dev, st.st_ino):
            return None
        budget = WalkStats(max_entries=MAX_STAGED_MEMBERS)
        entries = directory.scan(budget)
        if budget.truncated or any(is_link(s) or not stat.S_ISREG(s.st_mode) for _, s in entries):
            return None
        members = tuple((n, FileIdentity.of(s)) for n, s in entries)
        listed = dict(members)
        if _RECORD not in listed:
            return None
        try:
            read = read_capped(directory, _RECORD, _MAX_RECORD_BYTES)
        except (FsPolicyError, OSError):
            return None
        if read is None or FileIdentity.of(read[1]) != listed[_RECORD]:
            return None
        try:
            data = json.loads(read[0].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        kind = _STAGED_KIND.get(str(data.get("state"))) if isinstance(data, dict) else None
        if kind is None:
            return None
        return _Staged(kind, listed[_RECORD], members, (st.st_dev, st.st_ino))


def _plan_staged(scope: PurgeScope, policy: RetentionPolicy, now: datetime,
                 max_entries: int) -> tuple[list[PurgeItem], bool]:
    try:
        root = open_root(scope.root)
    except FileNotFoundError:
        return [], False
    items: list[PurgeItem] = []
    budget = WalkStats(max_entries=max_entries)
    with root:
        for name, st in root.scan(budget):
            if not _STAGED_DIR.fullmatch(name) or is_link(st) or not stat.S_ISDIR(st.st_mode):
                continue
            snap = _staged_snapshot(root, name)
            if snap is None or not _expired_ns(now, snap.record.mtime_ns, _window(snap.kind, policy)):
                continue
            p = policy_for(snap.kind)
            items.append(PurgeItem(scope.scope_id, name, snap.kind, p.retention, p.purge_rule,
                                   sum(i.size for _, i in snap.members), snap.record.mtime_ns,
                                   _age_seconds(now, snap.record.mtime_ns), members=snap.members,
                                   dir_identity=snap.dir_identity))
    return items, budget.truncated


def _plan_files(scope: PurgeScope, policy: RetentionPolicy, now: datetime,
                max_entries: int) -> tuple[list[PurgeItem], bool]:
    p = policy_for(scope.kind)
    window = _window(scope.kind, policy)
    stats = WalkStats(max_entries=max_entries)
    items = [
        PurgeItem(scope.scope_id, entry.relpath, scope.kind, p.retention, p.purge_rule, entry.size_bytes,
                  entry.mtime_ns, _age_seconds(now, entry.mtime_ns), identity=entry.identity)
        for entry in walk_files(scope.root, max_entries=max_entries, stats=stats)
        if _expired_ns(now, entry.mtime_ns, window)
    ]
    return items, stats.truncated


# --- rejected payloads -------------------------------------------------------------


def _payload_parts(artifact_hash: str) -> tuple[str, str, str]:
    return artifact_hash[:2], artifact_hash[2:4], artifact_hash


def _pending_name(artifact_hash: str) -> str:
    return f".{artifact_hash}{PENDING_SUFFIX}"


def _payload_dir(stack: ExitStack, object_root: Path, artifact_hash: str) -> PinnedDir:
    first, second, _ = _payload_parts(artifact_hash)
    root = stack.enter_context(open_root(object_root))
    return stack.enter_context(stack.enter_context(root.child(first)).child(second))


def _hash_pinned(directory: PinnedDir, name: str) -> tuple[str, FileIdentity]:
    fd = directory.open_file(name)
    try:
        identity = FileIdentity.of(os.fstat(fd))
        digest = hashlib.sha256()
        while block := os.read(fd, 1 << 20):
            digest.update(block)
        return digest.hexdigest(), identity
    finally:
        os.close(fd)


def _authority_check(authority: RejectionAuthority, artifact_hash: str, policy: RetentionPolicy,
                     now: datetime) -> tuple[RejectionEvidence | None, str | None]:
    """(evidence, None) when purgeable now, else (None, refusal code)."""
    if authority.referenced_by_accepted_state(artifact_hash):
        return None, ItemStatus.PAYLOAD_REFERENCED_BY_ACCEPTED_STATE.value
    evidence = authority.rejection_evidence(artifact_hash)
    if evidence is None:
        return None, authority.refusal_code(artifact_hash) or "ARTIFACT_NOT_PROVEN_REJECTED"
    if evidence.artifact_hash != artifact_hash or evidence.status != "REJECTED":
        return None, "ARTIFACT_NOT_PROVEN_REJECTED"
    decided = _parse_time(evidence.decided_at)
    if decided is None:
        return None, "REJECTION_TIME_UNKNOWN"
    # Both instants are timezone-aware: the comparison is between UTC instants.
    if now.astimezone(UTC) - decided.astimezone(UTC) <= payload_window(policy):
        return None, "PAYLOAD_RETENTION_NOT_ELAPSED"
    return evidence, None


def _plan_payloads(candidates: Sequence[str], object_root: Path, authority: RejectionAuthority,
                   policy: RetentionPolicy, now: datetime) -> tuple[list[PurgeItem], list[tuple[str, str]]]:
    if len(candidates) != len(set(candidates)):
        raise PurgeError("DUPLICATE_PAYLOAD_CANDIDATE")
    p = policy_for(DataKind.REJECTED_ARTIFACT_PAYLOAD)
    items: list[PurgeItem] = []
    refused: list[tuple[str, str]] = []
    for artifact_hash in sorted(candidates):
        if not isinstance(artifact_hash, str) or not _SHA256.fullmatch(artifact_hash):
            raise PurgeError("ARTIFACT_HASH_INVALID")
        evidence, code = _authority_check(authority, artifact_hash, policy, now)
        if evidence is None:
            refused.append((artifact_hash, str(code)))
            continue
        try:
            with ExitStack() as stack:
                st = _payload_dir(stack, object_root, artifact_hash).lstat(artifact_hash)
        except FileNotFoundError:
            st = None
        except FsPolicyError:
            refused.append((artifact_hash, "PAYLOAD_PATH_NOT_PLAIN"))
            continue
        if st is None or is_link(st) or not stat.S_ISREG(st.st_mode):
            refused.append((artifact_hash, "PAYLOAD_NOT_PRESENT"))
            continue
        decided = _parse_time(evidence.decided_at)
        age = int((now - decided).total_seconds()) if decided else 0
        items.append(PurgeItem(PAYLOAD_SCOPE_ID, "/".join(_payload_parts(artifact_hash)),
                               DataKind.REJECTED_ARTIFACT_PAYLOAD, p.retention, p.purge_rule, st.st_size,
                               st.st_mtime_ns, age, identity=FileIdentity.of(st), evidence=evidence))
    return items, refused


# --- plan --------------------------------------------------------------------------


def plan_purge(
    scopes: Sequence[PurgeScope],
    *,
    now: datetime,
    policy: RetentionPolicy | None = None,
    protected: Sequence[Path] = (),
    candidates: Sequence[str] = (),
    object_root: Path | None = None,
    authority: RejectionAuthority | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> PurgePlan:
    """Deterministic dry run: identical filesystem state, authority answers and ``now`` give one hash.

    ``candidates`` are artifact hashes to consider for payload purge; whether each
    is rejected is decided by ``authority``, never by the caller.
    """
    if now.tzinfo is None:
        raise PurgeError("NOW_MUST_BE_TIMEZONE_AWARE")
    active = policy or RetentionPolicy()
    guarded = [*protected, *([object_root] if object_root is not None else [])]
    _validate_scopes(scopes, guarded)
    if candidates and (object_root is None or authority is None):
        raise PurgeError("REJECTION_PURGE_REQUIRES_OBJECT_ROOT_AND_AUTHORITY")
    if object_root is not None and _lstat_link(object_root):
        raise PurgeError("OBJECT_ROOT_IS_SYMLINK")
    items: list[PurgeItem] = []
    truncated = False
    for scope in sorted(scopes, key=lambda s: s.scope_id):
        planner = _plan_staged if scope.unit is PurgeUnit.STAGED_RECORD else _plan_files
        found, cut = planner(scope, active, now, max_entries)
        items.extend(found)
        truncated = truncated or cut
    refused: list[tuple[str, str]] = []
    if candidates and object_root is not None and authority is not None:
        payloads, refused = _plan_payloads(candidates, object_root, authority, active, now)
        items.extend(payloads)
    plan = PurgePlan(
        generated_at=now.astimezone(UTC).isoformat(),
        r0_seconds=int(active.r0.total_seconds()),
        r1_seconds=int(active.r1.total_seconds()),
        explicit_override=active.explicit_override,
        scopes=tuple(sorted(scopes, key=lambda s: s.scope_id)),
        object_root=None if object_root is None else str(real_root(object_root)),
        protected=tuple(sorted(str(real_root(p)) for p in protected)),
        items=tuple(sorted(items, key=lambda i: (i.scope_id, i.relpath))),
        refused=tuple(refused),
        truncated=truncated,
    )
    return replace(plan, plan_hash=plan.compute_hash())


# --- apply -------------------------------------------------------------------------


@dataclass
class _ApplyContext:
    plan: PurgePlan
    policy: RetentionPolicy
    now: datetime
    authority: RejectionAuthority | None
    tombstones: TombstoneLedger | None
    reason: str
    written: int = 0

    def tombstone(self, phase: Phase, evidence: RejectionEvidence, reason: str,
                  reserved: tuple[int, int, int]) -> None:
        if self.tombstones is None:
            raise PurgeError("REJECTION_PURGE_REQUIRES_AUTHORITY_AND_TOMBSTONES")
        self.tombstones.append(tombstone_record(
            phase=phase, artifact_hash=evidence.artifact_hash, status=evidence.status,
            timestamp=evidence.decided_at, receipt_ref=evidence.receipt_ref,
            decision_evidence_ref=evidence.decision_evidence_ref,
            recorded_at=self.now.astimezone(UTC).isoformat(), reason=reason, plan_hash=self.plan.plan_hash,
            reserved=reserved))
        self.written += 1


LOCK_DIR = ".pados-lifecycle-locks"


@contextmanager
def artifact_lock(object_root: Path, artifact_hash: str) -> Iterator[None]:
    """Per-artifact, process-safe lock in the object store's PADOS-owned lock directory.

    Held across pending lookup -> reservation -> authority re-check -> unlink -> final
    tombstone, and by recovery, so concurrent purges and recoveries of one artifact
    serialize. Object-store writers do not take it (see ACCEPTED_RISK in the docs).
    """
    if not _SHA256.fullmatch(artifact_hash):
        raise PurgeError("ARTIFACT_HASH_INVALID")
    lock_dir = Path(object_root) / LOCK_DIR
    ensure_durable_dir(lock_dir)
    with exclusive_lock(lock_dir / f"{artifact_hash}.lock"):
        yield


def _reserved_tuple(identity: FileIdentity) -> tuple[int, int, int]:
    return identity.dev, identity.ino, identity.size


def _split_relpath(relpath: str) -> list[str] | None:
    parts = relpath.split("/")
    if any(not p or p in {".", ".."} or "\\" in p or "\x00" in p for p in parts):
        return None
    return parts


def _apply_file(item: PurgeItem, root: Path, ctx: _ApplyContext) -> tuple[ItemStatus, int]:
    parts = _split_relpath(item.relpath)
    if parts is None or item.identity is None:
        return ItemStatus.REFUSED_OUTSIDE_ROOT, 0
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(open_root(root))
            for part in parts[:-1]:
                directory = stack.enter_context(directory.child(part))
        except FileNotFoundError:
            return ItemStatus.ALREADY_ABSENT, 0
        st = directory.lstat(parts[-1])
        if st is None:
            return ItemStatus.ALREADY_ABSENT, 0
        if is_link(st) or not stat.S_ISREG(st.st_mode):
            return ItemStatus.REFUSED_NOT_REGULAR, 0
        if FileIdentity.of(st) != item.identity:
            return ItemStatus.SKIPPED_CHANGED, 0
        if not _expired_ns(ctx.now, st.st_mtime_ns, _window(item.data_kind, ctx.policy)):
            return ItemStatus.SKIPPED_NOT_EXPIRED, 0
        if not directory.unlink_verified(parts[-1], item.identity):
            return ItemStatus.SKIPPED_CHANGED, 0
        return ItemStatus.DELETED, item.size_bytes


def _apply_staged(item: PurgeItem, root: Path, ctx: _ApplyContext) -> tuple[ItemStatus, int]:
    if not _STAGED_DIR.fullmatch(item.relpath) or item.dir_identity is None:
        return ItemStatus.REFUSED_OUTSIDE_ROOT, 0
    try:
        pinned_root = open_root(root)
    except FileNotFoundError:
        return ItemStatus.ALREADY_ABSENT, 0
    with pinned_root:
        if pinned_root.lstat(item.relpath) is None:
            return ItemStatus.ALREADY_ABSENT, 0
        snap = _staged_snapshot(pinned_root, item.relpath)
        if snap is None or snap.kind is not item.data_kind or snap.members != item.members \
                or snap.dir_identity != item.dir_identity:
            return ItemStatus.SKIPPED_CHANGED, 0
        if not _expired_ns(ctx.now, snap.record.mtime_ns, _window(snap.kind, ctx.policy)):
            return ItemStatus.SKIPPED_NOT_EXPIRED, 0
        freed = 0
        with pinned_root.child(item.relpath) as directory:
            if directory.identity() != item.dir_identity:
                return ItemStatus.SKIPPED_CHANGED, 0
            for name, identity in item.members:
                if not directory.unlink_verified(name, identity):
                    return ItemStatus.PARTIAL_DIRECTORY_RETAINED if freed else ItemStatus.SKIPPED_CHANGED, freed
                freed += identity.size
            directory.fsync()
        try:
            pinned_root.rmdir(item.relpath)
        except OSError:
            return ItemStatus.PARTIAL_DIRECTORY_RETAINED, freed  # an unapproved entry appeared: kept
        pinned_root.fsync()
        return ItemStatus.DELETED, freed


# Restore outcomes that leave the reservation unresolved: PENDING is kept, nothing is recorded.
UNRESOLVED_RESTORE = frozenset({ItemStatus.RESTORE_CONFLICT, ItemStatus.RESTORE_CLEANUP_FAILED})


def _restore(directory: PinnedDir, artifact_hash: str, pending: str, identity: FileIdentity) -> ItemStatus:
    """No-clobber rollback of a reservation, bound to the verified reserved inode.

    Callers hold the artifact lock. The pending name is re-verified (no-follow descriptor:
    identity and sha256) immediately before it is published; after the link the canonical
    name must be that same inode with the right hash, otherwise the published name is removed
    again (``RESTORE_CONFLICT``). A cleanup that fails is ``RESTORE_CLEANUP_FAILED``. In both
    cases the PENDING tombstone stays and nothing is recorded for the failed step.
    """
    try:
        digest, opened = _hash_pinned(directory, pending)
    except (FileNotFoundError, FsPolicyError):
        return ItemStatus.RESTORE_CONFLICT
    if opened != identity or digest != artifact_hash:
        return ItemStatus.RESTORE_CONFLICT
    try:
        directory.link(pending, artifact_hash)
    except FileExistsError:
        # A writer re-published the content-addressed object: keep it if its bytes verify.
        canonical_digest, _ = _hash_pinned(directory, artifact_hash)
        if canonical_digest != artifact_hash:
            return ItemStatus.RESTORE_CONFLICT  # both names kept for human review
        if not directory.unlink_verified(pending, identity):
            return ItemStatus.RESTORE_CLEANUP_FAILED
        directory.fsync()
        return ItemStatus.RESTORE_CANONICAL_ALREADY_PRESENT
    published = directory.lstat(artifact_hash)
    try:
        published_digest, published_identity = _hash_pinned(directory, artifact_hash)
    except (FileNotFoundError, FsPolicyError):
        published_digest, published_identity = "", None
    if (published is None or (published.st_dev, published.st_ino) != (identity.dev, identity.ino)
            or published_identity != identity or published_digest != artifact_hash):
        if published is not None:  # we published something that is not the verified inode: withdraw it
            directory.unlink_verified(artifact_hash, FileIdentity.of(published))
        directory.fsync()
        return ItemStatus.RESTORE_CONFLICT
    if not directory.unlink_verified(pending, identity):
        directory.fsync()
        return ItemStatus.RESTORE_CLEANUP_FAILED
    directory.fsync()
    return ItemStatus.REFUSED_REFERENCED_AFTER_RESERVE


def _apply_payload(item: PurgeItem, object_root: Path, ctx: _ApplyContext) -> tuple[ItemStatus, int]:
    planned = item.evidence
    if planned is None or item.identity is None:
        return ItemStatus.REFUSED_NOT_REJECTED, 0
    artifact_hash = planned.artifact_hash
    if not _SHA256.fullmatch(artifact_hash) or item.relpath != "/".join(_payload_parts(artifact_hash)):
        return ItemStatus.REFUSED_OUTSIDE_ROOT, 0
    if ctx.authority is None or ctx.tombstones is None:
        raise PurgeError("REJECTION_PURGE_REQUIRES_AUTHORITY_AND_TOMBSTONES")
    evidence, code = _authority_check(ctx.authority, artifact_hash, ctx.policy, ctx.now)
    if code == ItemStatus.PAYLOAD_REFERENCED_BY_ACCEPTED_STATE.value:
        return ItemStatus.PAYLOAD_REFERENCED_BY_ACCEPTED_STATE, 0
    if code == "PAYLOAD_RETENTION_NOT_ELAPSED":
        return ItemStatus.SKIPPED_NOT_EXPIRED, 0
    if evidence is None:
        return ItemStatus.REFUSED_NOT_REJECTED, 0
    if evidence != planned:
        return ItemStatus.SKIPPED_CHANGED, 0
    with artifact_lock(object_root, artifact_hash):
        return _purge_locked(item, item.identity, evidence, object_root, ctx)


def _purge_locked(item: PurgeItem, planned_identity: FileIdentity, evidence: RejectionEvidence,
                  object_root: Path, ctx: _ApplyContext) -> tuple[ItemStatus, int]:
    if ctx.authority is None or ctx.tombstones is None:
        raise PurgeError("REJECTION_PURGE_REQUIRES_AUTHORITY_AND_TOMBSTONES")
    artifact_hash = evidence.artifact_hash
    latest = ctx.tombstones.latest(artifact_hash)
    if latest is not None and latest["phase"] == Phase.PENDING.value:
        return ItemStatus.PENDING_RECOVERY_REQUIRED, 0
    with ExitStack() as stack:
        try:
            directory = _payload_dir(stack, object_root, artifact_hash)
        except FileNotFoundError:
            directory = None
        st = None if directory is None else directory.lstat(artifact_hash)
        if directory is None or st is None:
            purged = latest is not None and latest["phase"] == Phase.COMPLETED.value
            return (ItemStatus.ALREADY_PURGED if purged else ItemStatus.ALREADY_ABSENT), 0
        if is_link(st) or not stat.S_ISREG(st.st_mode):
            return ItemStatus.REFUSED_NOT_REGULAR, 0
        if FileIdentity.of(st) != planned_identity:
            return ItemStatus.SKIPPED_CHANGED, 0
        digest, identity = _hash_pinned(directory, artifact_hash)
        if digest != artifact_hash or identity != planned_identity:
            return ItemStatus.REFUSED_HASH_MISMATCH, 0
        pending = _pending_name(artifact_hash)
        if directory.lstat(pending) is not None:
            return ItemStatus.PENDING_RECOVERY_REQUIRED, 0
        reserved = _reserved_tuple(identity)
        ctx.tombstone(Phase.PENDING, evidence, ctx.reason, reserved)
        try:
            directory.link(artifact_hash, pending)  # no-clobber reservation of the verified inode
        except FileExistsError:
            ctx.tombstone(Phase.ABORTED, evidence, "RESERVATION_CONFLICT", reserved)
            return ItemStatus.PENDING_RECOVERY_REQUIRED, 0
        linked = directory.lstat(pending)
        if linked is None or FileIdentity.of(linked) != identity \
                or not directory.unlink_verified(artifact_hash, identity):
            # The canonical name changed between hashing and reservation: drop our link only.
            cleaned = linked is None or directory.unlink_verified(pending, FileIdentity.of(linked))
            directory.fsync()
            if not cleaned:
                return ItemStatus.RESTORE_CLEANUP_FAILED, 0  # PENDING stays for recovery
            ctx.tombstone(Phase.ABORTED, evidence, "CANONICAL_CHANGED_DURING_RESERVE", reserved)
            return ItemStatus.SKIPPED_CHANGED, 0
        directory.fsync()  # reserved: the canonical name no longer serves the bytes
        still = ctx.authority.rejection_evidence(artifact_hash)
        if ctx.authority.referenced_by_accepted_state(artifact_hash) or still != evidence:
            status = _restore(directory, artifact_hash, pending, identity)
            if status not in UNRESOLVED_RESTORE:
                ctx.tombstone(Phase.ABORTED, evidence, status.value, reserved)
            return status, 0
        if not directory.unlink_verified(pending, identity):
            return ItemStatus.PENDING_RECOVERY_REQUIRED, 0  # reserved name was swapped: PENDING stays
        directory.fsync()
        ctx.tombstone(Phase.COMPLETED, evidence, ctx.reason, reserved)
        return ItemStatus.PAYLOAD_PURGED, item.size_bytes


def apply_purge(
    plan: PurgePlan,
    *,
    approved_plan_hash: str,
    now: datetime,
    policy: RetentionPolicy | None = None,
    protected: Sequence[Path] = (),
    authority: RejectionAuthority | None = None,
    tombstones: TombstoneLedger | None = None,
    reason: str = "REJECTED_PAYLOAD_RETENTION_ELAPSED",
) -> PurgeResult:
    """Execute an approved plan under its own windows; refusals are reported per item, not raised."""
    if not isinstance(approved_plan_hash, str) or not hmac.compare_digest(
            approved_plan_hash.encode("utf-8"), plan.plan_hash.encode("utf-8")):
        raise PurgeError("PLAN_HASH_NOT_APPROVED")
    if plan.compute_hash() != plan.plan_hash:
        raise PurgeError("PLAN_HASH_MISMATCH")
    if now.tzinfo is None:
        raise PurgeError("NOW_MUST_BE_TIMEZONE_AWARE")
    bound = plan.policy()
    if policy is not None and (policy.r0, policy.r1) != (bound.r0, bound.r1):
        raise PurgeError("POLICY_MISMATCH")
    guarded = [Path(p) for p in plan.protected] + list(protected)
    if tombstones is not None:
        guarded.append(tombstones.path)
    if plan.object_root is not None:
        guarded.append(Path(plan.object_root))
    _validate_scopes(plan.scopes, guarded)
    ctx = _ApplyContext(plan, bound, now, authority, tombstones, reason)
    scopes = {s.scope_id: s for s in plan.scopes}
    outcomes: list[ItemOutcome] = []
    for item in plan.items:
        try:
            status, freed = _apply_item(item, scopes, ctx)
        except FsPolicyError:
            status, freed = ItemStatus.REFUSED_OUTSIDE_ROOT, 0
        except TombstoneError:
            status, freed = ItemStatus.REFUSED_TOMBSTONE_CHAIN_INVALID, 0
        except OSError:
            status, freed = ItemStatus.FAILED_IO, 0
        outcomes.append(ItemOutcome(item.scope_id, item.relpath, item.data_kind, status, freed))
    return PurgeResult(plan.plan_hash, now.astimezone(UTC).isoformat(), tuple(outcomes), ctx.written)


def _apply_item(item: PurgeItem, scopes: dict[str, PurgeScope], ctx: _ApplyContext) -> tuple[ItemStatus, int]:
    if item.scope_id == PAYLOAD_SCOPE_ID:
        if item.data_kind is not DataKind.REJECTED_ARTIFACT_PAYLOAD or ctx.plan.object_root is None \
                or item.rule is not PurgeRule.PURGE_PAYLOAD_KEEP_METADATA:
            return ItemStatus.REFUSED_AUTHORITATIVE, 0
        return _apply_payload(item, Path(ctx.plan.object_root), ctx)
    scope = scopes.get(item.scope_id)
    if scope is None:
        return ItemStatus.REFUSED_OUTSIDE_ROOT, 0
    if not _deletable(policy_for(item.data_kind)) or item.rule is not PurgeRule.DELETE_RECORD:
        return ItemStatus.REFUSED_AUTHORITATIVE, 0
    if scope.unit is PurgeUnit.STAGED_RECORD:
        if item.data_kind not in _STAGED_KIND.values():
            return ItemStatus.REFUSED_AUTHORITATIVE, 0
        return _apply_staged(item, scope.root, ctx)
    if item.data_kind is not scope.kind:
        return ItemStatus.REFUSED_AUTHORITATIVE, 0
    return _apply_file(item, scope.root, ctx)


# --- recovery ----------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryOutcome:
    artifact_hash: str
    status: RecoveryStatus

    def to_dict(self) -> dict[str, str]:
        return {"artifact_hash": self.artifact_hash, "status": self.status.value}


def _record_evidence(record: dict[str, Any]) -> RejectionEvidence:
    return RejectionEvidence(record["artifact_hash"], record["status"], record["timestamp"],
                             record["receipt_ref"], record["decision_evidence_ref"])


def recover_pending(
    tombstones: TombstoneLedger,
    *,
    object_root: Path,
    authority: RejectionAuthority,
    approved_plan_hash: str,
    now: datetime,
) -> list[RecoveryOutcome]:
    """Resolve PENDING tombstones of one approved plan after an interrupted apply.

    Recovery is bound to the reserved inode recorded in the PENDING tombstone and runs
    under the per-artifact lock. Only that exact reserved inode is ever unlinked, and only
    when the authority still proves the artifact rejected and unreferenced; otherwise the
    reservation is restored without clobbering. The canonical object is never deleted.
    """
    if now.tzinfo is None:
        raise PurgeError("NOW_MUST_BE_TIMEZONE_AWARE")
    if _lstat_link(object_root):
        raise PurgeError("OBJECT_ROOT_IS_SYMLINK")
    outcomes: list[RecoveryOutcome] = []
    for record in tombstones.pending():
        artifact_hash = str(record["artifact_hash"])
        if record["schema"] != TOMBSTONE_SCHEMA:
            # Legacy PENDING without a bound reserved inode: never recovered automatically.
            outcomes.append(RecoveryOutcome(artifact_hash, RecoveryStatus.LEGACY_PENDING_REQUIRES_OPERATOR))
            continue
        if not _approved(record, approved_plan_hash):
            outcomes.append(RecoveryOutcome(artifact_hash, RecoveryStatus.NOT_APPROVED))
            continue
        with artifact_lock(object_root, artifact_hash):
            latest = tombstones.latest(artifact_hash)
            if latest is None or latest["phase"] != Phase.PENDING.value:
                continue  # resolved by a concurrent recovery while we waited for the lock
            if not _approved(latest, approved_plan_hash) or _binding(latest) != _binding(record):
                outcomes.append(RecoveryOutcome(artifact_hash, RecoveryStatus.RECOVERY_PLAN_MISMATCH))
                continue
            # The context is built only from the record re-verified under the lock.
            ctx = _ApplyContext(PurgePlan("", 0, 0, False, (), None, (), (), (), False, str(latest["plan_hash"])),
                                RetentionPolicy(), now, authority, tombstones, "RECOVERED_PENDING_PURGE")
            outcomes.append(RecoveryOutcome(artifact_hash, _recover_one(latest, object_root, ctx)))
    return outcomes


_BINDING_KEYS = ("entry_hash", "schema", "plan_hash", "artifact_hash", "status", "timestamp", "receipt_ref",
                 "decision_evidence_ref", "reserved_dev", "reserved_ino", "reserved_size", "reserved_sha256")


def _approved(record: dict[str, Any], approved_plan_hash: str) -> bool:
    return isinstance(approved_plan_hash, str) and hmac.compare_digest(
        str(record["plan_hash"]).encode("utf-8"), approved_plan_hash.encode("utf-8"))


def _binding(record: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a PENDING record as validated before locking (entry hash included)."""
    return tuple(record.get(key) for key in _BINDING_KEYS)


def _recover_one(record: dict[str, Any], object_root: Path, ctx: _ApplyContext) -> RecoveryStatus:
    if ctx.authority is None:
        raise PurgeError("RECOVERY_REQUIRES_AUTHORITY")
    evidence = _record_evidence(record)
    artifact_hash = evidence.artifact_hash
    reserved = (int(record["reserved_dev"]), int(record["reserved_ino"]), int(record["reserved_size"]))
    valid = (not ctx.authority.referenced_by_accepted_state(artifact_hash)
             and ctx.authority.rejection_evidence(artifact_hash) == evidence)
    pending = _pending_name(artifact_hash)
    with ExitStack() as stack:
        try:
            directory = _payload_dir(stack, object_root, artifact_hash)
        except FileNotFoundError:
            ctx.tombstone(Phase.COMPLETED, evidence, "RECOVERED_PAYLOAD_ABSENT", reserved)
            return RecoveryStatus.RECOVERED_COMPLETED
        canonical = directory.lstat(artifact_hash)
        if directory.lstat(pending) is None:
            if canonical is None:
                ctx.tombstone(Phase.COMPLETED, evidence, "RECOVERED_PAYLOAD_ABSENT", reserved)
                return RecoveryStatus.RECOVERED_COMPLETED
            ctx.tombstone(Phase.ABORTED, evidence, "RECOVERED_NOT_RESERVED", reserved)
            return RecoveryStatus.RECOVERED_ABORTED
        # Bind to the recorded inode: open no-follow, fstat identity, hash the opened descriptor.
        digest, identity = _hash_pinned(directory, pending)
        if (identity.dev, identity.ino, identity.size) != reserved or digest != artifact_hash:
            return RecoveryStatus.RECOVERY_CONFLICT
        if canonical is None and valid:
            if not directory.unlink_verified(pending, identity):
                return RecoveryStatus.RECOVERY_CONFLICT
            directory.fsync()
            ctx.tombstone(Phase.COMPLETED, evidence, "RECOVERED_PENDING_PURGE", reserved)
            return RecoveryStatus.RECOVERED_COMPLETED
        status = _restore(directory, artifact_hash, pending, identity)
        if status is ItemStatus.RESTORE_CONFLICT:
            return RecoveryStatus.RECOVERY_CONFLICT
        if status is ItemStatus.RESTORE_CLEANUP_FAILED:
            return RecoveryStatus.RESTORE_CLEANUP_FAILED
        ctx.tombstone(Phase.ABORTED, evidence, f"RECOVERED_{status.value}", reserved)
        return RecoveryStatus.RECOVERED_ABORTED
