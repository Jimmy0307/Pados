# SPDX-License-Identifier: AGPL-3.0-only
"""External anchoring of the advice log: a compare-and-set checkpoint store OUTSIDE the writable log.

``ExternalAnchor`` is the contract the advice log writes through (operator-supplied; e.g. the existing audit
authority). ``record(previous, new)`` is compare-and-set: it refuses unless ``previous`` equals the anchor's
current latest checkpoint and ``new`` continues it (``new.seq == previous.seq + 1``, or ``1`` for the first
checkpoint). A writer that substitutes the log can therefore never move the anchor off the trusted head, and
two concurrent recorders can never regress it. ``record(..., marker=True)`` additionally keeps ``new`` in a
bounded history of anchored ``PRUNED`` markers, so a pruning transition is authenticated only when the anchor
actually recorded its marker.

``FileAnchor`` is the file implementation: one owner-only state file plus an initialization sentinel in an
owner-only directory, read-modify-write under ``pados.lifecycle.locking.exclusive_lock``, atomic replace with
file and directory fsync. Once initialized, a missing or unreadable head fails closed (``ANCHOR_HEAD_LOST`` /
``ANCHOR_STATE_INVALID``); it is never re-created silently. The anchor stores sequence numbers and hashes only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pados.lifecycle.fswalk import fsync_directory, is_link
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

ANCHOR_SCHEMA = "pados.learning.anchor.v1"
DEFAULT_MAX_MARKERS = 64
_STATE_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATE_KEYS = frozenset({"schema_version", "latest", "markers"})
_WINDOWS_REPLACE_RETRIES = 8
_WINDOWS_REPLACE_RETRYABLE_ERRORS = frozenset({5, 32, 33})


def replace_with_retry(source: Path, destination: Path) -> None:
    """``os.replace``, briefly retrying transient Windows sharing/access failures.

    Windows readers and on-access scanners can temporarily deny replacement of an open target.
    The retry is bounded and only covers the native sharing/access error codes; every persistent
    failure still propagates so the caller can fail closed and retain the previous anchor.
    """
    for attempt in range(_WINDOWS_REPLACE_RETRIES if sys.platform == "win32" else 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if (sys.platform != "win32" or getattr(exc, "winerror", None) not in _WINDOWS_REPLACE_RETRYABLE_ERRORS
                    or attempt + 1 == _WINDOWS_REPLACE_RETRIES):
                raise
            time.sleep(min(0.01 * (2 ** attempt), 0.25))


_replace_state = replace_with_retry  # the anchor's own state replace (tests substitute it)


class AnchorError(RuntimeError):
    """Closed-code refusal of an anchor operation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Checkpoint:
    """An externally anchorable head of the advice log."""

    seq: int
    entry_hash: str

    def __post_init__(self) -> None:
        if (not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1
                or not isinstance(self.entry_hash, str) or not _SHA256.fullmatch(self.entry_hash)):
            raise ValueError("CHECKPOINT_INVALID")


class ExternalAnchor(Protocol):
    """Compare-and-set anchor OUTSIDE the writable store (e.g. the existing audit authority)."""

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        """Atomically replace the latest checkpoint by ``new`` only when ``previous`` equals it and ``new``
        continues it (``seq + 1``; ``1`` for the first); otherwise raise. ``marker=True`` also keeps ``new`` in
        the bounded history of anchored PRUNED markers."""

    def latest(self) -> Checkpoint | None:
        """The latest anchored checkpoint; ``None`` only before the first ``record``."""

    def markers(self) -> tuple[Checkpoint, ...]:
        """Recently anchored PRUNED marker checkpoints (bounded, oldest first)."""


def continues(previous: Checkpoint | None, new: Checkpoint) -> bool:
    """The only update an anchor accepts: the first checkpoint, or the next sequence number."""
    return new.seq == 1 if previous is None else new.seq == previous.seq + 1


class FileAnchor:
    """``ExternalAnchor`` over an owner-only directory (place it OUTSIDE the advice log's writable store)."""

    def __init__(self, directory: Path, *, max_markers: int = DEFAULT_MAX_MARKERS) -> None:
        if not 1 <= max_markers <= 4096:
            raise ValueError("ANCHOR_BOUND_INVALID")
        self.directory = Path(directory)
        self.max_markers = max_markers
        self._head = self.directory / "anchor.head.json"
        self._sentinel = self.directory / "anchor.initialized"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        ensure_durable_dir(self.directory)
        info = os.lstat(self.directory)
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise AnchorError("ANCHOR_DIR_INVALID")
        if sys.platform != "win32" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise AnchorError("ANCHOR_DIR_INSECURE")
        with exclusive_lock(self.directory / "anchor.lock"):
            yield

    @staticmethod
    def _secure(info: os.stat_result) -> None:
        if is_link(info) or not stat.S_ISREG(info.st_mode):
            raise AnchorError("ANCHOR_STATE_INVALID")
        if sys.platform != "win32" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise AnchorError("ANCHOR_STATE_INSECURE")

    def _exists(self, path: Path) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        self._secure(info)
        return True

    def _state(self) -> tuple[Checkpoint | None, tuple[Checkpoint, ...]] | None:
        """(latest, markers), or None before initialization (caller holds the lock)."""
        initialized, present = self._exists(self._sentinel), self._exists(self._head)
        if not present:
            if initialized:
                raise AnchorError("ANCHOR_HEAD_LOST")
            return None
        state = self._read_head()
        if not initialized:
            # The head is written before the sentinel, so a head without a sentinel is an interrupted FIRST
            # initialization -- but only while it still holds just the first checkpoint. Complete it; a later
            # head without its sentinel means the sentinel was removed after initialization: refuse.
            latest = state[0]
            if latest is None or latest.seq != 1:
                raise AnchorError("ANCHOR_STATE_INVALID")
            self._complete_initialization()
        return state

    def _read_head(self) -> tuple[Checkpoint | None, tuple[Checkpoint, ...]]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(self._head, flags), "rb") as handle:
            self._secure(os.fstat(handle.fileno()))
            payload = handle.read(_STATE_BYTES + 1)
        try:
            if len(payload) > _STATE_BYTES:
                raise ValueError("ANCHOR_STATE_TOO_LARGE")
            data = json.loads(payload.decode("utf-8"))
            assert_minimized(data, allowed_keys=_STATE_KEYS, max_string=128, max_items=4096,
                             max_total_bytes=_STATE_BYTES)
            if data.get("schema_version") != ANCHOR_SCHEMA:
                raise ValueError("ANCHOR_SCHEMA_UNSUPPORTED")
            latest = None if data["latest"] is None else _point(data["latest"])
            markers = tuple(_point(item) for item in data["markers"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, DataMinimizationViolation) as exc:
            raise AnchorError("ANCHOR_STATE_INVALID") from exc
        if latest is None and markers:
            raise AnchorError("ANCHOR_STATE_INVALID")
        return latest, markers

    def latest(self) -> Checkpoint | None:
        with self._locked():
            state = self._state()
        return None if state is None else state[0]

    def markers(self) -> tuple[Checkpoint, ...]:
        with self._locked():
            state = self._state()
        return () if state is None else state[1]

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        if not isinstance(new, Checkpoint) or not (previous is None or isinstance(previous, Checkpoint)):
            raise AnchorError("ANCHOR_CHECKPOINT_INVALID")
        with self._locked():
            state = self._state()
            current, markers = (None, ()) if state is None else state
            if previous != current:
                raise AnchorError("ANCHOR_CONFLICT")  # compare-and-set: the caller did not see the latest head
            if not continues(current, new):
                raise AnchorError("ANCHOR_NOT_CONTINUING")
            kept = (*markers, new)[-self.max_markers:] if marker else markers
            state_data = {"schema_version": ANCHOR_SCHEMA, "latest": _plain(new),
                          "markers": [_plain(item) for item in kept]}
            if state is None:
                self._initialize(state_data)
            else:
                self._write(state_data)

    def _initialize(self, data: dict[str, Any]) -> None:
        """First record: head first (atomic replace), sentinel second.

        A sentinel without a head is indistinguishable from a head lost after initialization
        (``ANCHOR_HEAD_LOST``), so the sentinel is never written before the head exists: any failure or crash
        before the head replace lands leaves the anchor uninitialized (retryable), and a crash after it leaves a
        first-checkpoint head without a sentinel, which the next access completes (see ``_state``). No rollback
        step is needed, so none can fail and strand the anchor.
        """
        self._write(data)  # the commit point: once this replace landed the first checkpoint is real
        self._complete_initialization()

    def _complete_initialization(self) -> None:
        """Write the sentinel after the head landed. An I/O failure here is DEFERRED, not reported: the head is
        already committed, and reporting the call as failed would make a caller roll its log back while the
        anchor keeps the new head (anchor ahead of log, forever). The seq-1 head without a sentinel is completed
        by the next access (``_state``). A non-I/O refusal (an insecure existing sentinel) still propagates."""
        try:
            self._create_sentinel()
        except OSError:
            return  # deferred to the next access; never a reason to treat the landed head as uncommitted

    def _create_sentinel(self) -> None:
        """Mark the anchor initialized; idempotent (an interrupted initialization may already have created it)."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self._sentinel, flags, 0o600)
        except FileExistsError:
            self._exists(self._sentinel)  # still a secure regular file (links/foreign owners refused)
            return
        try:
            self._secure(os.fstat(descriptor))
            payload = ANCHOR_SCHEMA.encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("ANCHOR_SENTINEL_SHORT_WRITE")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self.directory)

    def _write(self, data: dict[str, Any]) -> None:
        assert_minimized(data, allowed_keys=_STATE_KEYS, max_string=128, max_items=4096, max_total_bytes=_STATE_BYTES)
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")
        temporary = self.directory / f".anchor.{secrets.token_hex(6)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                self._secure(os.fstat(handle.fileno()))
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_state(temporary, self._head)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        fsync_directory(self.directory)


def _plain(point: Checkpoint) -> dict[str, Any]:
    return {"seq": point.seq, "entry_hash": point.entry_hash}


def _point(value: Any) -> Checkpoint:
    if not isinstance(value, dict) or set(value) != {"seq", "entry_hash"}:
        raise ValueError("ANCHOR_CHECKPOINT_INVALID")
    return Checkpoint(value["seq"], value["entry_hash"])
