# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only, hash-chained JSONL log shared by the training job, registry and decision ledgers.

Operational evidence only: every entry passes ``assert_minimized`` with the writer's explicit schema, so
only identifiers, hashes, states, counts and timestamps are persisted. The chain proves internal
consistency, not origin: anyone able to rewrite the whole file can recompute it, which is why state
changes that matter are additionally bound to signed, consumed human decisions.

``append`` runs the caller's ``build`` callback under an exclusive file lock with the verified entries, so
a state-machine check and the append it justifies are atomic across processes; a callback returning
``None`` records nothing (used for idempotent replays of an already-applied decision).

External anchor (CR-12b TP-01, mandatory for authority-bearing transitions since CR-12c): the operator
configures an ``ExternalAnchor`` (for example ``FileExternalAnchor`` on storage the training process cannot
rewrite). The log binds its name to its identity in the anchor when it is opened, every append records the
new ``(seq, head_hash)`` there, and every read requires the log head to EQUAL the anchored head:

* the log is shorter than, or diverges below, the anchored head -> ``LEDGER_ROLLBACK_DETECTED``;
* the log is ahead of the anchored head -> ``LEDGER_HEAD_NOT_ANCHORED``, except for the one entry this very
  log object appended and could not anchor (an interrupted append), whose head is recorded first
  (recovery) and accepted only once recorded (CR-12c-01). There is NO in-process recovery API for an
  unanchored head left by a crash (CR-12d-01): the log stays fail-closed until the operator restores it to
  the anchored head from backup, or retires it (a new log and a new anchor name), offline;
* the anchor cannot be read -> ``LEDGER_ANCHOR_UNAVAILABLE``; its head vanished after initialization ->
  ``LEDGER_ANCHOR_LOST``.

Without an anchor the log is ``UNANCHORED``: it can still be read for reporting, but the training plane
refuses every authority-bearing transition over it (``ANCHOR_REQUIRED``).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pados.execution.context import canonical_hash
from pados.lifecycle.fswalk import FsPolicyError, fsync_directory
from pados.lifecycle.locking import exclusive_lock
from pados.lifecycle.minimization import assert_minimized

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

GENESIS = "0" * 64
CHAIN_FIELDS = ("seq", "prev_hash", "entry_hash")
MAX_LOG_BYTES = 64 * 1024 * 1024
ANCHOR_SCHEMA = "pados.training-ledger-anchor/v1"
ANCHOR_FIELDS = ("schema", "log_name", "seq", "head_hash")
ANCHOR_INIT_SCHEMA = "pados.training-ledger-anchor-init/v1"
ANCHOR_INIT_FIELDS = ("schema", "log_name", "log_id")
LOG_IDENTITY_SCHEMA = "pados.training-ledger-identity/v1"
MAX_ANCHOR_BYTES = 4096
JOB_LOG_NAME = "training-job-events"
REGISTRY_LOG_NAME = "model-registry"
LEDGER_LOG_NAME = "training-decision-ledger"
LEDGER_ROLLBACK_DETECTED = "LEDGER_ROLLBACK_DETECTED"
LEDGER_ANCHOR_UNAVAILABLE = "LEDGER_ANCHOR_UNAVAILABLE"
LEDGER_HEAD_NOT_ANCHORED = "LEDGER_HEAD_NOT_ANCHORED"
LEDGER_ANCHOR_LOST = "LEDGER_ANCHOR_LOST"
ANCHOR_LOG_NAME_IN_USE = "ANCHOR_LOG_NAME_IN_USE"
ANCHOR_REQUIRED = "ANCHOR_REQUIRED"
ANCHOR_CODES = frozenset({LEDGER_ROLLBACK_DETECTED, LEDGER_ANCHOR_UNAVAILABLE, LEDGER_HEAD_NOT_ANCHORED,
                          LEDGER_ANCHOR_LOST, ANCHOR_LOG_NAME_IN_USE, ANCHOR_REQUIRED})
_LOG_NAME = re.compile(r"[a-z][a-z0-9-]{2,63}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class ChainedLogError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Anchoring(StrEnum):
    """Whether rollback below an externally recorded head is detectable for the logs behind a verdict."""

    ANCHORED = "ANCHORED"
    UNANCHORED = "UNANCHORED"


@dataclass(frozen=True)
class AnchorHead:
    log_name: str
    seq: int
    head_hash: str


@runtime_checkable
class ExternalAnchor(Protocol):
    """Operator-held record of each log's latest head, kept where the training process cannot rewrite it.

    ``bind`` claims ``log_name`` for exactly one log identity (refusing another with
    ``ANCHOR_LOG_NAME_IN_USE``) and initializes its head at ``seq = 0`` (genesis) the first time.
    ``record`` must be monotonic: it refuses a lower ``seq`` or a different hash at the same ``seq`` with
    ``LEDGER_ROLLBACK_DETECTED``. ``latest`` returns ``None`` only when nothing was ever bound or recorded."""

    def bind(self, log_name: str, log_id: str) -> None: ...

    def record(self, log_name: str, seq: int, head_hash: str) -> None: ...

    def latest(self, log_name: str) -> AnchorHead | None: ...


def _check_head(log_name: str, seq: int, head_hash: str, *, allow_genesis: bool = False) -> None:
    if (not _LOG_NAME.fullmatch(log_name) or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
            or not isinstance(head_hash, str) or not _HEX64.fullmatch(head_hash)):
        raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
    if seq == 0 and not (allow_genesis and head_hash == GENESIS):
        raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


class FileExternalAnchor:
    """One small JSON head file per log in an operator-chosen directory outside the writable log store.

    Per log name it keeps ``<log_name>.anchor.init`` (written once, exclusively, when the log is first
    bound: the durable record that the log was initialized and which log identity owns the name) and
    ``<log_name>.anchor.json`` (the current head, starting at the genesis head ``seq = 0``). Every
    ``bind``, ``record`` and ``latest`` runs under a per-name ``pados.lifecycle.locking`` lock, so
    concurrent recorders never regress the head (CR-12c-02). Head writes are atomic (temporary file,
    fsync, ``os.replace``, directory fsync on POSIX). A head that vanished after initialization fails
    closed with ``LEDGER_ANCHOR_LOST``; a missing, symlinked, oversized or malformed file fails closed with
    ``LEDGER_ANCHOR_UNAVAILABLE``. Only the log name, an identity hash, the sequence number and the head
    hash are persisted (``assert_minimized``)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._bound: dict[str, str] = {}

    def _path(self, log_name: str, suffix: str = ".anchor.json") -> Path:
        if not _LOG_NAME.fullmatch(log_name):
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        return self.directory / f"{log_name}{suffix}"

    @contextmanager
    def _locked(self, log_name: str) -> Iterator[None]:
        lock_path = self._path(log_name, ".anchor.lock")
        lock_path = lock_path.with_name("." + lock_path.name)
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive_lock(lock_path))
            except (OSError, FsPolicyError) as exc:
                raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc
            yield

    def _document(self, path: Path, fields: Sequence[str], schema: str, log_name: str) -> dict[str, Any] | None:
        if path.is_symlink():
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_ANCHOR_BYTES + 1)
        try:
            if len(raw) > MAX_ANCHOR_BYTES:
                raise ValueError("ANCHOR_TOO_LARGE")
            document = json.loads(raw.decode("utf-8"))
            if (not isinstance(document, dict) or set(document) != set(fields)
                    or document["schema"] != schema or document["log_name"] != log_name):
                raise ValueError("ANCHOR_MALFORMED")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc
        return document

    def _init(self, log_name: str) -> dict[str, Any] | None:
        document = self._document(self._path(log_name, ".anchor.init"), ANCHOR_INIT_FIELDS, ANCHOR_INIT_SCHEMA,
                                  log_name)
        if document is not None and not (isinstance(document["log_id"], str) and _HEX64.fullmatch(document["log_id"])):
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        return document

    def _head(self, log_name: str) -> AnchorHead | None:
        """Current head under the caller's lock; ``LEDGER_ANCHOR_LOST`` when it vanished after initialization."""
        initialized = self._init(log_name) is not None
        document = self._document(self._path(log_name), ANCHOR_FIELDS, ANCHOR_SCHEMA, log_name)
        if document is None:
            if initialized:
                raise ChainedLogError(LEDGER_ANCHOR_LOST)
            return None
        if not initialized:  # a head without its durable initialization record
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        _check_head(document["log_name"], document["seq"], document["head_hash"], allow_genesis=True)
        return AnchorHead(document["log_name"], document["seq"], document["head_hash"])

    def _write(self, path: Path, document: dict[str, Any], fields: Sequence[str], *, exclusive: bool) -> None:
        assert_minimized(document, allowed_keys=fields)
        data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        target = path if exclusive else path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC) | _NOFOLLOW
        try:
            fd = os.open(target, flags, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            if not exclusive:
                os.replace(target, path)
            fsync_directory(self.directory)
        except OSError as exc:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc

    def bind(self, log_name: str, log_id: str) -> None:
        """Claim ``log_name`` for the log ``log_id`` and initialize its genesis head on first use."""
        if not isinstance(log_id, str) or not _HEX64.fullmatch(log_id):
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        if self._bound.get(log_name, log_id) != log_id:
            raise ChainedLogError(ANCHOR_LOG_NAME_IN_USE)
        with self._locked(log_name):
            init = self._init(log_name)
            if init is not None:
                if init["log_id"] != log_id:
                    raise ChainedLogError(ANCHOR_LOG_NAME_IN_USE)
                self._head(log_name)  # LEDGER_ANCHOR_LOST when the head vanished
            elif self._head(log_name) is None:
                self._write(self._path(log_name, ".anchor.init"),
                            {"schema": ANCHOR_INIT_SCHEMA, "log_name": log_name, "log_id": log_id},
                            ANCHOR_INIT_FIELDS, exclusive=True)
                self._write(self._path(log_name),
                            {"schema": ANCHOR_SCHEMA, "log_name": log_name, "seq": 0, "head_hash": GENESIS},
                            ANCHOR_FIELDS, exclusive=False)
        self._bound[log_name] = log_id

    def latest(self, log_name: str) -> AnchorHead | None:
        with self._locked(log_name):
            head = self._head(log_name)
            self._durable()  # CR-12d-03: a head is accepted only once its directory entry is durable
            return head

    def _durable(self) -> None:
        try:
            fsync_directory(self.directory)
        except OSError as exc:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc

    def record(self, log_name: str, seq: int, head_hash: str) -> None:
        _check_head(log_name, seq, head_hash)
        log_id = self._bound.get(log_name)
        if log_id is None:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)  # record only for a log bound through this anchor
        with self._locked(log_name):
            init = self._init(log_name)
            if init is None:
                raise ChainedLogError(LEDGER_ANCHOR_LOST)
            if init["log_id"] != log_id:
                raise ChainedLogError(ANCHOR_LOG_NAME_IN_USE)
            current = self._head(log_name)
            if current is None:
                raise ChainedLogError(LEDGER_ANCHOR_LOST)
            if seq < current.seq or (seq == current.seq and head_hash != current.head_hash):
                raise ChainedLogError(LEDGER_ROLLBACK_DETECTED)
            if seq == current.seq:
                self._durable()  # an equal-head retry completes the durability a failed write left open
                return
            self._write(self._path(log_name),
                        {"schema": ANCHOR_SCHEMA, "log_name": log_name, "seq": seq, "head_hash": head_hash},
                        ANCHOR_FIELDS, exclusive=False)


def _contains(outer: Path, inner: Path) -> bool:
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class HashChainedLog:
    """One JSONL file; entries carry ``seq``, ``prev_hash`` and ``entry_hash`` over the canonical body."""

    def __init__(self, path: Path, *, fields: Sequence[str], corrupt_code: str, log_name: str,
                 anchor: ExternalAnchor | None = None) -> None:
        missing = [name for name in CHAIN_FIELDS if name not in fields]
        if missing:
            raise ValueError("CHAIN_FIELDS_REQUIRED")
        if not _LOG_NAME.fullmatch(log_name):
            raise ValueError("LOG_NAME_INVALID")
        if anchor is not None and not isinstance(anchor, ExternalAnchor):
            raise TypeError("EXTERNAL_ANCHOR_REQUIRED")
        self.path = Path(path)
        if isinstance(anchor, FileExternalAnchor):
            store, outside = self.path.resolve().parent, anchor.directory.resolve()
            if _contains(store, outside) or _contains(outside, store):
                raise ValueError("ANCHOR_INSIDE_LOG_STORE")
        self.log_name = log_name
        self._fields = tuple(fields)
        self._corrupt = corrupt_code
        self._anchor = anchor
        self._outstanding: tuple[int, str] | None = None
        if anchor is not None:
            _anchor_call(lambda: anchor.bind(log_name, self.log_id))

    @property
    def anchoring(self) -> Anchoring:
        return Anchoring.UNANCHORED if self._anchor is None else Anchoring.ANCHORED

    @property
    def anchor(self) -> ExternalAnchor | None:
        return self._anchor

    @property
    def log_id(self) -> str:
        """The identity this log claims its name with in the anchor (a hash, never the path itself)."""
        return canonical_hash({"schema": LOG_IDENTITY_SCHEMA, "path": str(self.path.resolve())})

    def _check_anchor(self, entries: list[dict[str, Any]]) -> None:
        """Accept the log only when its head EQUALS the anchored head (see the module docstring)."""
        anchor = self._anchor
        if anchor is None:
            return
        head = _anchor_call(lambda: anchor.latest(self.log_name))
        if head is None:  # bound when opened: a head that is gone now was lost
            raise ChainedLogError(LEDGER_ANCHOR_LOST)
        _compare_head(entries, head)
        count = len(entries)
        if count == head.seq:
            self._outstanding = None
            return
        if count == head.seq + 1 and self._outstanding == (count, entries[-1]["entry_hash"]):
            try:  # recovery of this log's own interrupted append: anchor the head before accepting it
                _anchor_call(lambda: anchor.record(self.log_name, count, entries[-1]["entry_hash"]))
            except ChainedLogError as exc:
                if exc.code == LEDGER_ROLLBACK_DETECTED:
                    raise
                raise ChainedLogError(LEDGER_HEAD_NOT_ANCHORED) from None
            self._outstanding = None
            return
        raise ChainedLogError(LEDGER_HEAD_NOT_ANCHORED)

    def _read(self) -> list[dict[str, Any]]:
        if self.path.is_symlink():
            raise ChainedLogError(self._corrupt)
        try:
            fd = os.open(self.path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            entries: list[dict[str, Any]] = []
        except OSError as exc:
            raise ChainedLogError(self._corrupt) from exc
        else:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(MAX_LOG_BYTES + 1)
            if len(raw) > MAX_LOG_BYTES:
                raise ChainedLogError(self._corrupt)
            entries = _parse_chain(raw, self._corrupt, fields=self._fields)
        self._check_anchor(entries)
        return entries

    def entries(self) -> list[dict[str, Any]]:
        with _locked(self.path):
            return self._read()

    def append(self, build: Callable[[list[dict[str, Any]]], Mapping[str, Any] | None]) -> dict[str, Any] | None:
        """Append ``build(entries)``; a ``None`` body means "nothing to record" (idempotent replays)."""
        with _locked(self.path):
            entries = self._read()
            built = build(entries)
            if built is None:
                return None
            body = dict(built)
            if any(name in body for name in CHAIN_FIELDS):
                raise ValueError("CHAIN_FIELDS_ARE_ASSIGNED_BY_THE_LOG")
            entry: dict[str, Any] = {**body, "seq": len(entries) + 1,
                                     "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS}
            entry["entry_hash"] = canonical_hash(entry)
            if set(entry) != set(self._fields):
                raise ValueError("CHAIN_ENTRY_SCHEMA_MISMATCH")
            assert_minimized(entry, allowed_keys=self._fields)
            line = (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._record_anchor(entry)
            return entry

    def _record_anchor(self, entry: Mapping[str, Any]) -> None:
        """Record the new head after the entry is durable (the anchor never runs ahead of the log).

        Until it is recorded the entry is this log's outstanding head: the next read under the lock records
        it before accepting it (CR-12c-01) or fails closed with ``LEDGER_HEAD_NOT_ANCHORED``."""
        anchor = self._anchor
        if anchor is None:
            return
        seq, head_hash = int(entry["seq"]), str(entry["entry_hash"])
        self._outstanding = (seq, head_hash)
        _anchor_call(lambda: anchor.record(self.log_name, seq, head_hash))
        self._outstanding = None

def _anchor_call[T](call: Callable[[], T]) -> T:
    """Run an operator-supplied anchor operation; any failure other than a coded one is ``UNAVAILABLE``."""
    try:
        return call()
    except ChainedLogError:
        raise
    except Exception as exc:
        raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE) from exc


def _compare_head(entries: list[dict[str, Any]], head: AnchorHead) -> None:
    """Fail closed when the log no longer reaches the anchored head (truncated, or rewritten below it)."""
    if head.seq == 0:
        if head.head_hash != GENESIS:
            raise ChainedLogError(LEDGER_ANCHOR_UNAVAILABLE)
        return
    if len(entries) < head.seq or entries[head.seq - 1].get("entry_hash") != head.head_hash:
        raise ChainedLogError(LEDGER_ROLLBACK_DETECTED)


def _parse_chain(raw: bytes, corrupt: str, *, fields: Sequence[str] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous = GENESIS
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ChainedLogError(corrupt) from exc
    for number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ChainedLogError(corrupt) from exc
        if (not isinstance(entry, dict) or (fields is not None and set(entry) != set(fields))
                or not set(CHAIN_FIELDS) <= set(entry) or entry["seq"] != number or entry["prev_hash"] != previous
                or entry["entry_hash"] != canonical_hash({k: v for k, v in entry.items() if k != "entry_hash"})):
            raise ChainedLogError(corrupt)
        previous = entry["entry_hash"]
        entries.append(entry)
    return entries


def decision_ids(path: Path, *, anchor: ExternalAnchor | None = None, log_name: str = REGISTRY_LOG_NAME
                 ) -> frozenset[str]:
    """Every decision id named by another training log (``decision_id``, ``approval_id`` or a retained
    ``decision`` record), for cross-log single-use checks. Unreadable or malformed logs fail closed.

    With ``anchor`` the other log's chain is verified and its head must equal the head anchored under
    ``log_name`` (``LEDGER_ROLLBACK_DETECTED`` / ``LEDGER_HEAD_NOT_ANCHORED``); a log never anchored there
    is accepted only while it is empty."""
    if path.is_symlink():
        raise ChainedLogError("DECISION_LOG_UNREADABLE")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise ChainedLogError("DECISION_LOG_UNREADABLE") from exc
    else:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_LOG_BYTES + 1)
    if len(raw) > MAX_LOG_BYTES:
        raise ChainedLogError("DECISION_LOG_UNREADABLE")
    entries = _parse_chain(raw, "DECISION_LOG_UNREADABLE", fields=None)
    if anchor is not None:
        head = _anchor_call(lambda: anchor.latest(log_name))
        if head is None:
            if entries:
                raise ChainedLogError(LEDGER_HEAD_NOT_ANCHORED)
        else:
            _compare_head(entries, head)
            if len(entries) != head.seq:
                raise ChainedLogError(LEDGER_HEAD_NOT_ANCHORED)
    found: set[str] = set()
    for entry in entries:
        for key in ("decision_id", "approval_id"):
            if isinstance(entry.get(key), str):
                found.add(entry[key])
        decision = entry.get("decision")
        if isinstance(decision, dict) and isinstance(decision.get("decision_id"), str):
            found.add(decision["decision_id"])
    return frozenset(found)
