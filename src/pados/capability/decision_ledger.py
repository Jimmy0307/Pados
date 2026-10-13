# SPDX-License-Identifier: AGPL-3.0-only
"""Consumed human-decision ledger: durable, process-shared, hash-chained, single use. Decision ids only.

``FileConsumedDecisionLedger`` is the ledger the executor requires for human-gated tasks. ``consume`` is an
atomic consume-if-unused: under the process-safe ``pados.lifecycle.locking.exclusive_lock`` on a sibling
lock file it verifies the whole chain, refuses an id that is already consumed, appends ONE entry and
fsyncs it before returning. A restart, a second verifier or a second process using the same file
therefore sees every earlier consumption.

* Owner-only: the directory is created 0700 and the file 0600. On POSIX a file that is not a regular file,
  is reached through a link, belongs to another user or is group/other accessible is refused.
* Hash-chained: each entry carries ``seq``, ``prev_hash`` and ``entry_hash`` (canonical hash of the rest).
  Any edit, reordering, insertion or gap fails closed (``HUMAN_DECISION_LEDGER_CORRUPT``).
* Minimized: an entry holds only ``schema, seq, decision_id, consumed_at, prev_hash, entry_hash`` and passes
  ``assert_minimized`` with that explicit schema before it is written (and again when read back).
* Bounded: ``capacity`` entries (fail closed when full: ``HUMAN_DECISION_LEDGER_FULL``) and a line limit.
* Complete appends (CR13c-02): ``os.write`` is repeated until every byte of the entry is appended (zero
  progress refuses), and ``fsync`` runs only after the complete entry. Any failure refuses the consumption
  with ``HUMAN_DECISION_LEDGER_UNAVAILABLE``, so execution never starts on an incomplete record. Afterwards
  the decision is consumed (the full entry reached the file) or refused: with zero progress nothing was
  written and the decision has authorized nothing; with partial progress the file ends in a torn line and
  every later open fails closed (``HUMAN_DECISION_LEDGER_TORN_ENTRY``) until an operator repairs the file -
  the torn tail is never truncated automatically, because that would make the refused decision usable again
  without an operator seeing it.

DataKind: ``CAPABILITY_DECISION_LEDGER`` (R3 authority metadata, never purged automatically; lifecycle
catalog and inventory point ``capability_decision_ledger``). The chain proves internal consistency, not origin: a writer able to rewrite the whole
file can recompute a chain (same residual as every local hash-chained ledger in PADOS).

``in_memory_ledger_for_tests()`` builds a NON-durable ledger for unit tests only. The executor refuses it
for gated tasks, and a static test shows that no non-test module calls it.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pados.capability.human_decision import HumanDecisionCode, HumanDecisionRefused
from pados.execution.context import canonical_hash
from pados.lifecycle.fswalk import fsync_directory
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import DataKind, RetentionClass

CAPABILITY_DECISION_LEDGER = DataKind.CAPABILITY_DECISION_LEDGER
CAPABILITY_DECISION_LEDGER_RETENTION = RetentionClass.R3_AUTHORITY
LEDGER_SCHEMA = "pados.capability.consumed-decision/v1"
LEDGER_KEYS = frozenset({"schema", "seq", "decision_id", "consumed_at", "prev_hash", "entry_hash"})
MAX_CONSUMED_DECISIONS = 100_000
MAX_LINE_BYTES = 1024
GENESIS = "0" * 64
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

if sys.platform == "win32":
    _READ_FLAGS = os.O_RDONLY | os.O_BINARY
    _APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_BINARY

    def _owner_only(info: os.stat_result) -> bool:
        return True  # ACCEPTED_RISK_WINDOWS: NTFS ACLs are not modelled by st_mode
else:
    _READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    _APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC

    def _owner_only(info: os.stat_result) -> bool:
        return info.st_uid == os.getuid() and not info.st_mode & 0o077


def _refused(code: HumanDecisionCode) -> HumanDecisionRefused:
    return HumanDecisionRefused(code)


def _decision_id(value: object) -> str:
    if type(value) is not str or not _HEX32.fullmatch(value):
        raise _refused(HumanDecisionCode.HUMAN_DECISION_MALFORMED)
    return value


def _sealed(entry: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in entry.items() if key != "entry_hash"})


def _minimized(entry: dict[str, Any]) -> None:
    assert_minimized(entry, allowed_keys=LEDGER_KEYS, max_string=96, max_items=8, max_total_bytes=MAX_LINE_BYTES)


def _validated(entry: object, seq: int, prev_hash: str) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != LEDGER_KEYS:
        raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_CORRUPT)
    _minimized(entry)
    consumed_at = entry["consumed_at"]
    if not (entry["schema"] == LEDGER_SCHEMA and entry["seq"] == seq and type(entry["seq"]) is int
            and entry["prev_hash"] == prev_hash and type(entry["decision_id"]) is str
            and _HEX32.fullmatch(entry["decision_id"]) and type(entry["entry_hash"]) is str
            and _HEX64.fullmatch(entry["entry_hash"]) and entry["entry_hash"] == _sealed(entry)
            and type(consumed_at) is str and datetime.fromisoformat(consumed_at).tzinfo is not None):
        raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_CORRUPT)
    return entry


def _write_all(fd: int, data: bytes) -> None:
    """Append every byte of ``data``: a short write continues with the rest; zero progress, an impossible
    count or an error refuses (``HUMAN_DECISION_LEDGER_UNAVAILABLE``). A refusal after partial progress leaves
    a torn trailing line, which every later open refuses (``HUMAN_DECISION_LEDGER_TORN_ENTRY``)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if type(written) is not int or not 0 < written <= len(view):
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE)
        view = view[written:]


class FileConsumedDecisionLedger:
    """Durable single-use ledger shared by every verifier and process that opens the same file."""

    def __init__(self, path: Path, *, capacity: int = MAX_CONSUMED_DECISIONS,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not 1 <= capacity <= MAX_CONSUMED_DECISIONS:
            raise ValueError("HUMAN_DECISION_LEDGER_CAPACITY_INVALID")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            ensure_durable_dir(self.path.parent)
        except OSError as exc:
            raise ValueError("HUMAN_DECISION_LEDGER_DIRECTORY_INVALID") from exc

    @property
    def durable(self) -> bool:
        return True

    def is_consumed(self, decision_id: str) -> bool:
        """Advisory pre-check; ``consume`` alone is authoritative (it re-checks under the lock)."""
        wanted = _decision_id(decision_id)
        with self._locked():
            return any(entry["decision_id"] == wanted for entry in self._verified())

    def consume(self, decision_id: str) -> None:
        """Atomically record ``decision_id`` unless it is already consumed (then refuse)."""
        wanted = _decision_id(decision_id)
        with self._locked():
            entries = self._verified()
            if any(entry["decision_id"] == wanted for entry in entries):
                raise _refused(HumanDecisionCode.HUMAN_DECISION_ALREADY_CONSUMED)
            if len(entries) >= self._capacity:
                raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_FULL)
            try:
                consumed_at = self._clock().astimezone(UTC).isoformat()
            except Exception as exc:  # noqa: BLE001 - no trustworthy timestamp means no consumption
                raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE) from exc
            entry: dict[str, Any] = {"schema": LEDGER_SCHEMA, "seq": len(entries) + 1, "decision_id": wanted,
                                     "consumed_at": consumed_at,
                                     "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS}
            entry["entry_hash"] = _sealed(entry)
            try:
                _minimized(entry)
            except DataMinimizationViolation as exc:
                raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE) from exc
            self._append(entry, created=not entries and not self._exists())

    def verify(self) -> bool:
        try:
            with self._locked():
                self._verified()
        except HumanDecisionRefused:
            return False
        return True

    # --- internals (callers hold the lock) ------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            with exclusive_lock(self.lock_path):
                yield
        except HumanDecisionRefused:
            raise
        except (OSError, ValueError) as exc:  # lock file unusable (incl. FsPolicyError): fail closed
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE) from exc

    def _exists(self) -> bool:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE) from exc
        if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNSAFE)
        return True

    def _verified(self) -> list[dict[str, Any]]:
        if not self._exists():
            return []
        try:
            fd = os.open(self.path, _READ_FLAGS)
        except OSError as exc:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNSAFE) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
                raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNSAFE)
            if info.st_size > self._capacity * MAX_LINE_BYTES:
                raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_CORRUPT)
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(self._capacity * MAX_LINE_BYTES + 1)
        finally:
            os.close(fd)
        if data and not data.endswith(b"\n"):  # a torn trailing entry (interrupted append): fail closed
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_TORN_ENTRY)
        entries: list[dict[str, Any]] = []
        prev = GENESIS
        try:
            for raw in data.split(b"\n"):
                if not raw:
                    continue
                if len(raw) > MAX_LINE_BYTES:
                    raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_CORRUPT)
                entry = _validated(json.loads(raw.decode("utf-8")), len(entries) + 1, prev)
                prev = entry["entry_hash"]
                entries.append(entry)
        except HumanDecisionRefused:
            raise
        except (ValueError, TypeError, KeyError, DataMinimizationViolation) as exc:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_CORRUPT) from exc
        return entries

    def _append(self, entry: dict[str, Any], *, created: bool) -> None:
        line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        try:
            fd = os.open(self.path, _APPEND_FLAGS, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
                    raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNSAFE)
                _write_all(fd, line)  # every byte, or refuse (CR13c-02)
                os.fsync(fd)  # only after the COMPLETE entry is appended
            finally:
                os.close(fd)
            if created:
                fsync_directory(self.path.parent)
        except HumanDecisionRefused:
            raise
        except OSError as exc:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_UNAVAILABLE) from exc


class _InMemoryConsumedDecisions:
    """NON-durable ledger for unit tests (``in_memory_ledger_for_tests``). The executor refuses it."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._ids: set[str] = set()

    @property
    def durable(self) -> bool:
        return False

    def is_consumed(self, decision_id: str) -> bool:
        return _decision_id(decision_id) in self._ids

    def consume(self, decision_id: str) -> None:
        wanted = _decision_id(decision_id)
        if wanted in self._ids:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_ALREADY_CONSUMED)
        if len(self._ids) >= self._capacity:
            raise _refused(HumanDecisionCode.HUMAN_DECISION_LEDGER_FULL)
        self._ids.add(wanted)


def in_memory_ledger_for_tests(*, capacity: int = MAX_CONSUMED_DECISIONS) -> _InMemoryConsumedDecisions:
    """TEST ONLY: a process-local, non-durable ledger (a restart forgets every consumption)."""
    return _InMemoryConsumedDecisions(capacity)
