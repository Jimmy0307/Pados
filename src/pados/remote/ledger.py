# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only, hash-chained ledger of remote intents and their outcomes.

The ledger is operational evidence of what remote clients asked for. It is not
semantic truth; accepted state lives only behind receipt -> validation -> CAS.

Appends hold an OS file lock so separate processes cannot fork the chain. Before
an intent is dispatched its id is *claimed* in the ledger (atomically, under the
lock, bound to actor and request fingerprint), so a second process or service
instance never dispatches the same intent: dispatch is at most once. A claim
that never got an outcome (crash) is reported, never silently re-dispatched.

The ledger directory must be private to the PADOS service account: the lock and
ledger files are opened without following links, the lock is created 0600 and
must be a regular file owned by the current user, and a world-writable ledger
directory without the sticky bit is refused.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized

GENESIS = "0" * 64
CLAIMED, RELEASED = "CLAIMED", "RELEASED"
DEFAULT_CLAIM_LEASE_SECONDS = 300.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class LedgerPathError(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse_link(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise LedgerPathError("LEDGER_PATH_IS_LINK")
    if not stat.S_ISREG(info.st_mode):
        raise LedgerPathError("LEDGER_PATH_NOT_REGULAR")


def _open(path: Path, flags: int, mode: str) -> IO[Any]:
    """Open a ledger/lock file without following symlinks or reparse points."""
    _refuse_link(path)
    try:
        fd = os.open(path, flags | _NOFOLLOW | _BINARY | _CLOEXEC, 0o600)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP: a link was swapped in after the lstat check
        raise LedgerPathError("LEDGER_PATH_IS_LINK") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise LedgerPathError("LEDGER_PATH_NOT_REGULAR")
    return os.fdopen(fd, mode)


@dataclass(frozen=True)
class ClaimResult:
    """``CLAIMED`` (dispatch now), ``OUTCOME`` (replay ``record``), ``IN_FLIGHT``,
    ``UNRESOLVED`` (claim without outcome past its lease) or ``REUSED`` (other request)."""

    status: str
    claim_id: str | None = None
    record: dict[str, Any] | None = None


class IntentLedger:
    def __init__(self, path: Path, *, claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.claim_lease_seconds = claim_lease_seconds
        self._lock = threading.Lock()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _check_directory(self) -> None:
        if sys.platform == "win32":
            return
        info = os.stat(self.path.parent)
        if info.st_mode & stat.S_IWOTH and not info.st_mode & stat.S_ISVTX:
            raise LedgerPathError("LEDGER_DIRECTORY_NOT_PROTECTED")

    def _entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists() and not self.path.is_symlink():
            return
        with _open(self.path, os.O_RDONLY, "rb") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def _last_hash(self) -> str:
        last = GENESIS
        for entry in self._entries():
            last = entry["entry_hash"]
        return last

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Thread lock plus an OS file lock, so separate processes cannot fork the chain."""
        self._check_directory()
        with self._lock, _open(self.lock_path, os.O_RDWR | os.O_CREAT, "r+b") as lock_file:
            if sys.platform == "win32":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                info = os.fstat(lock_file.fileno())
                if info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise LedgerPathError("LEDGER_LOCK_NOT_OWNED")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _append_locked(self, record: dict[str, Any]) -> dict[str, Any]:
        entry = {"prev_hash": self._last_hash(), "record": record}
        entry["entry_hash"] = canonical_hash(entry)
        with _open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, "ab") as handle:
            handle.write((json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._exclusive():
            return self._append_locked(record)

    def claim(self, *, intent_id: str, actor_id: str, request_fingerprint: str) -> ClaimResult:
        """Atomically claim ``intent_id`` for one dispatch, or say why it must not dispatch.

        Only the decision and the claim record happen under the lock; the dispatch itself
        is awaited outside it.
        """
        with self._exclusive():
            outcome: dict[str, Any] | None = None
            claims: dict[str, dict[str, Any]] = {}
            released: set[str] = set()
            for entry in self._entries():
                record = entry["record"]
                if record.get("intent_id") != intent_id:
                    continue
                kind = record.get("claim")
                if kind == CLAIMED:
                    claims[str(record["claim_id"])] = record
                elif kind == RELEASED:
                    released.add(str(record.get("claim_id")))
                elif not record.get("replayed") and outcome is None:
                    outcome = {**record, "ledger_entry_hash": entry["entry_hash"]}
            if outcome is not None:
                return ClaimResult("OUTCOME", record=outcome)
            for claim_id, record in claims.items():
                if claim_id in released:
                    continue
                if (record.get("actor_id"), record.get("request_fingerprint")) != (actor_id, request_fingerprint):
                    return ClaimResult("REUSED", claim_id=claim_id)
                age = time.time() - float(record.get("claimed_at", 0))
                return ClaimResult("IN_FLIGHT" if age <= self.claim_lease_seconds else "UNRESOLVED", claim_id)
            claim_id = f"clm-{uuid.uuid4().hex}"
            record = {"intent_id": intent_id, "claim": CLAIMED, "claim_id": claim_id, "actor_id": actor_id,
                      "request_fingerprint": request_fingerprint, "claimed_at": round(time.time(), 3)}
            assert_minimized(record)
            self._append_locked(record)
            return ClaimResult("CLAIMED", claim_id=claim_id)

    def release(self, *, intent_id: str, claim_id: str, reason: str) -> dict[str, Any]:
        """Release a claim whose dispatch provably had no external effect, or after human review
        of the downstream idempotency record (``remote:<intent_id>``)."""
        record = {"intent_id": intent_id, "claim": RELEASED, "claim_id": claim_id, "reason": reason}
        assert_minimized(record)
        return self.append(record)

    def find(self, intent_id: str) -> dict[str, Any] | None:
        """First recorded outcome for ``intent_id`` (the original: never a replay or a claim)."""
        for entry in self._entries():
            record = entry["record"]
            if record.get("intent_id") == intent_id and not record.get("replayed") and not record.get("claim"):
                return {**record, "ledger_entry_hash": entry["entry_hash"]}
        return None

    def verify(self) -> bool:
        prev = GENESIS
        for entry in self._entries():
            claimed = entry.pop("entry_hash")
            if entry["prev_hash"] != prev or canonical_hash(entry) != claimed:
                return False
            prev = claimed
        return True
