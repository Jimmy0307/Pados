# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted execution ledger: the executor's append-only, hash-chained record of executions (content-free).

Two objects, one file (CR-16b H-04):

* ``FileExecutionLedger`` is the WRITER. The operator constructs it for the executor only
  (``CapabilityExecutor(..., ledger=)``), which holds it and appends ONE entry for every execution that
  produced a ``PlanReceipt``, completed or failed, through ``append_execution``. That function is the only
  writer, and a static test shows ``executor.py`` is its only importer under ``src/pados``.
* ``ExecutionLedgerReader`` is READ ONLY: ``resolve``, ``entries``, ``verify``. It is what consumers (the
  pipeline report builder and adapter) receive, from ``FileExecutionLedger.reader()`` or constructed from the
  same path and anchor. It has no append path and holds no reference to a writer (only the path, the
  capacity, the anchor's read side and the optional MAC key REFERENCE). Consumers refuse anything that is
  not exactly a reader (``EXECUTION_LEDGER_READER_REQUIRED``), so a writer is never handed to report code.

Readers resolve an execution by its ``PlanReceipt`` envelope hash (``resolve``) and compare the receipts they
were handed with the recorded entry (``LedgerEntry.matches``). A fabricated receipt chain is refused even when
it is internally consistent: it was never recorded.

An entry holds ids, closed codes and hashes only: ``schema, seq, envelope_hash, plan_id, plan_hash, task_id,
task_type, task_hash, bindings_hash, status, stages, recorded_at, prev_hash, entry_hash`` and, for a keyed
ledger, ``entry_mac``. ``stages`` lists, in plan order, ``[stage_id, receipt_hash, verification_method,
verification_status, verifier_id, evidence_hash]``. ``bindings_hash`` (``stage_bindings_hash``) binds the
template and every stage's capability and input bindings, so a reader can authenticate a graph without the
plan's parameters. Every entry passes ``assert_minimized`` with ``ENTRY_KEYS`` (plus ``entry_mac``) before it
is written and again when it is read.

File discipline (as the consumed-decision ledger, CR13c-02):

* Owner-only: the directory is created 0700 and the file 0600; on POSIX a file that is not regular, is
  reached through a link, belongs to another user or is group/other accessible is refused
  (``EXECUTION_LEDGER_UNSAFE``).
* Every read and every append runs under the process-safe ``pados.lifecycle.locking.exclusive_lock`` and
  verifies the whole chain first: an edit, reordering, insertion, gap, foreign or non-canonical line fails
  closed (``EXECUTION_LEDGER_CORRUPT``). Reads never fall back to "not found" on a damaged file.
* Complete appends: ``os.write`` is repeated until every byte is written (zero progress refuses) and
  ``fsync`` runs only after the complete entry. A torn trailing line (an interrupted append) makes every
  later read and append refuse (``EXECUTION_LEDGER_TORN_ENTRY``) until an operator repairs the file; it is
  never truncated automatically.
* Bounded: ``capacity`` entries, ``MAX_ENTRY_BYTES`` per line and ``MAX_LEDGER_BYTES`` per file. An append
  is refused BEFORE anything is written when the verified file size plus the serialized entry would exceed
  ``MAX_LEDGER_BYTES``, or when the ledger holds ``capacity`` entries (``EXECUTION_LEDGER_FULL``, CR-16b
  H-07), so a successful append never leaves a file that later reads refuse.

Anchoring (the compare-and-set pattern of the learning advice-log anchor; no code is shared): an
``ExecutionAnchor`` kept OUTSIDE the writable store holds the latest ``(seq, entry_hash)``. Each append
records the new head with compare-and-set after the entry is durable, and every read requires the anchored
head to equal the file's head (``EXECUTION_LEDGER_ANCHOR_MISMATCH``). A whole-file rewrite with a recomputed
chain is then refused as well. An append whose anchoring fails leaves the file one entry ahead of the anchor,
which fails closed until an operator repairs it. Anchoring is optional for the ledger itself and MANDATORY
for report building (consumers refuse an unanchored reader: ``EXECUTION_LEDGER_ANCHOR_REQUIRED``).

Optional entry authentication: with ``mac_key_ref`` (a ``secretref://`` reference) and a ``SecretBoundary``,
the writer adds ``entry_mac`` = HMAC-SHA256 over the entry hash under the executor key, and a reader
configured with the same reference verifies every entry (``EXECUTION_LEDGER_MAC_INVALID``; an unresolvable
or short key refuses with ``EXECUTION_LEDGER_KEY_UNAVAILABLE``). The key is resolved inside one function for
one scan or append and never stored, returned or persisted. This stops a party that can rewrite the ledger
file AND its anchor but cannot resolve the key.

What an entry proves, and what it cannot (residual, ``ASYMMETRIC_SIGNATURE_UPGRADE =
USER_DECISION_REQUIRED`` class): inside one Python process, module separation is not an isolation boundary.
Same-process code that obtains the writer, constructs a ``FileExecutionLedger`` for the same path, or can
resolve the MAC key reference through any ``SecretBoundary`` can still append a well-formed (and correctly
MACed) entry; HMAC is symmetric, so every reader that verifies MACs could also compute them. The static
tests keep PADOS modules honest; they do not authenticate a caller. A separate append service owning the
key (or an asymmetric executor signing key) is the stronger design.

DataKind: ``CAPABILITY_STAGE_EVIDENCE`` (R1 operational, hashes and metadata only). Requested inventory
point: ``capability_execution_ledger``. Rotating the file (retention) makes older executions unresolvable,
so reports for them are refused - never silently accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pados.capability.graph import InputBinding
from pados.capability.model import VerificationMethod
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus
from pados.enterprise.secrets import SecretBoundary, is_secret_ref
from pados.execution.context import canonical_hash
from pados.lifecycle.fswalk import fsync_directory
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import DataKind, RetentionClass

EXECUTION_LEDGER_DATA_KIND = DataKind.CAPABILITY_STAGE_EVIDENCE
EXECUTION_LEDGER_RETENTION = RetentionClass.R1_OPERATIONAL
EXECUTION_LEDGER_SCHEMA = "pados.capability.execution-ledger/v1"
ENTRY_KEYS = frozenset({"schema", "seq", "envelope_hash", "plan_id", "plan_hash", "task_id", "task_type",
                        "task_hash", "bindings_hash", "status", "stages", "recorded_at", "prev_hash", "entry_hash"})
MAC_FIELD = "entry_mac"
KEYED_ENTRY_KEYS = ENTRY_KEYS | {MAC_FIELD}
LEDGER_STATUSES = frozenset({"COMPLETED", "FAILED"})
MAX_EXECUTIONS = 20_000
MAX_LEDGER_STAGES = 64
MAX_ENTRY_BYTES = 32 * 1024
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MIN_MAC_KEY_BYTES = 32
GENESIS = "0" * 64
_MAC_DOMAIN = b"pados.capability.execution-ledger.entry-mac.v1\n"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_TASK_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")

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


class ExecutionLedgerError(PermissionError):
    """Closed-code refusal of a ledger read or append."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LedgerStage:
    stage_id: str
    receipt_hash: str
    verification_method: VerificationMethod
    verification_status: VerificationStatus
    verifier_id: str | None
    evidence_hash: str | None

    @classmethod
    def of(cls, receipt: StageReceipt) -> LedgerStage:
        return cls(receipt.stage_id, receipt.receipt_hash, receipt.verification_method, receipt.verification,
                   receipt.verifier_id, receipt.verification_evidence_hash)

    def row(self) -> list[str | None]:
        return [self.stage_id, self.receipt_hash, self.verification_method.value, self.verification_status.value,
                self.verifier_id, self.evidence_hash]


@dataclass(frozen=True)
class LedgerEntry:
    """One recorded execution (read side)."""

    seq: int
    envelope_hash: str
    plan_id: str
    plan_hash: str
    task_id: str
    task_type: str
    task_hash: str
    bindings_hash: str
    status: str
    stages: tuple[LedgerStage, ...]
    recorded_at: str
    entry_hash: str

    def matches(self, receipt: PlanReceipt) -> bool:
        """True only when ``receipt`` is exactly the recorded one (envelope, plan and every stage row)."""
        return (self.envelope_hash == receipt.envelope_hash and self.plan_id == receipt.plan_id
                and self.plan_hash == receipt.plan_hash
                and self.stages == tuple(LedgerStage.of(stage) for stage in receipt.stages))

    def stage(self, stage_id: str) -> LedgerStage | None:
        return next((item for item in self.stages if item.stage_id == stage_id), None)


@dataclass(frozen=True)
class LedgerCheckpoint:
    """An externally anchorable head of the execution ledger."""

    seq: int
    entry_hash: str

    def __post_init__(self) -> None:
        if (type(self.seq) is not int or self.seq < 1 or type(self.entry_hash) is not str
                or not _HEX64.fullmatch(self.entry_hash)):
            raise ValueError("LEDGER_CHECKPOINT_INVALID")


class AnchorView(Protocol):
    """The read side of an anchor (all a reader needs)."""

    def latest(self) -> LedgerCheckpoint | None:
        """The latest anchored checkpoint; ``None`` only before the first ``record``."""


class ExecutionAnchor(AnchorView, Protocol):
    """Compare-and-set head store OUTSIDE the ledger's writable store (operator-supplied)."""

    def record(self, previous: LedgerCheckpoint | None, new: LedgerCheckpoint) -> None:
        """Replace the latest checkpoint by ``new`` only when ``previous`` equals it and ``new.seq`` continues
        it (``previous.seq + 1``, or ``1`` for the first); otherwise raise."""


@dataclass(frozen=True)
class _AnchorHead:
    """A reader's handle on an anchor: only ``latest`` is ever called (no reference to the writer)."""

    source: AnchorView

    def latest(self) -> LedgerCheckpoint | None:
        return self.source.latest()


def stage_bindings_hash(template_id: str | None, template_hash: str | None,
                        stages: Sequence[tuple[str, str, Sequence[InputBinding]]]) -> str:
    """Canonical hash of the template identity and each stage's (id, capability, input bindings), in order."""
    return canonical_hash({
        "kind": "pados.capability.stage-bindings.v1", "template_id": template_id, "template_hash": template_hash,
        "stages": [[stage_id, capability_id, [item.model_dump(mode="json") for item in inputs]]
                   for stage_id, capability_id, inputs in stages]})


def _sealed(entry: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in entry.items() if key not in {"entry_hash", MAC_FIELD}})


def _minimized(entry: dict[str, Any]) -> None:
    assert_minimized(entry, allowed_keys=KEYED_ENTRY_KEYS, max_string=96, max_items=MAX_LEDGER_STAGES,
                     max_total_bytes=MAX_ENTRY_BYTES)


def _hex(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _optional_hex(value: object) -> bool:
    return value is None or _hex(value)


def _stage(row: object) -> LedgerStage:
    if not (isinstance(row, list) and len(row) == 6):
        raise ValueError("EXECUTION_LEDGER_CORRUPT")
    stage_id, receipt_hash, method, status, verifier_id, evidence_hash = row
    if not (type(stage_id) is str and _ID.fullmatch(stage_id) and _hex(receipt_hash)
            and (verifier_id is None or (type(verifier_id) is str and _ID.fullmatch(verifier_id)))
            and _optional_hex(evidence_hash) and type(method) is str and type(status) is str):
        raise ValueError("EXECUTION_LEDGER_CORRUPT")
    return LedgerStage(stage_id, receipt_hash, VerificationMethod(method), VerificationStatus(status), verifier_id,
                       evidence_hash)


def _validated(entry: object, seq: int, prev_hash: str) -> LedgerEntry:
    if not isinstance(entry, dict) or set(entry) not in (ENTRY_KEYS, KEYED_ENTRY_KEYS):
        raise ValueError("EXECUTION_LEDGER_CORRUPT")
    _minimized(entry)
    stages = entry["stages"]
    if not (entry["schema"] == EXECUTION_LEDGER_SCHEMA and type(entry["seq"]) is int and entry["seq"] == seq
            and entry["prev_hash"] == prev_hash and _hex(entry["entry_hash"])
            and entry["entry_hash"] == _sealed(entry) and (MAC_FIELD not in entry or _hex(entry[MAC_FIELD]))
            and all(_hex(entry[key]) for key in ("envelope_hash", "plan_hash", "task_hash", "bindings_hash"))
            and type(entry["plan_id"]) is str and _ID.fullmatch(entry["plan_id"])
            and type(entry["task_id"]) is str and _ID.fullmatch(entry["task_id"])
            and type(entry["task_type"]) is str and _TASK_TYPE.fullmatch(entry["task_type"])
            and entry["status"] in LEDGER_STATUSES and isinstance(stages, list)
            and 1 <= len(stages) <= MAX_LEDGER_STAGES and type(entry["recorded_at"]) is str
            and datetime.fromisoformat(entry["recorded_at"]).tzinfo is not None):
        raise ValueError("EXECUTION_LEDGER_CORRUPT")
    rows = tuple(_stage(row) for row in stages)
    if len({row.stage_id for row in rows}) != len(rows):
        raise ValueError("EXECUTION_LEDGER_CORRUPT")
    return LedgerEntry(seq, entry["envelope_hash"], entry["plan_id"], entry["plan_hash"], entry["task_id"],
                       entry["task_type"], entry["task_hash"], entry["bindings_hash"], entry["status"], rows,
                       entry["recorded_at"], entry["entry_hash"])


def _line(entry: object) -> bytes:
    return (json.dumps(entry, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def _write_all(fd: int, data: bytes) -> None:
    """Append every byte: a short write continues with the rest; zero progress, an impossible count or an
    error refuses. A refusal after partial progress leaves a torn trailing line that every later read refuses."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if type(written) is not int or not 0 < written <= len(view):
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNAVAILABLE")
        view = view[written:]


@dataclass(frozen=True)
class _MacKey:
    """A key REFERENCE and the boundary that resolves it; the key itself is never stored."""

    ref: str
    boundary: SecretBoundary

    @classmethod
    def of(cls, ref: str | None, boundary: SecretBoundary | None) -> _MacKey | None:
        if ref is None and boundary is None:
            return None
        if not (is_secret_ref(ref) and isinstance(boundary, SecretBoundary)):
            raise ValueError("EXECUTION_LEDGER_MAC_CONFIG_INVALID")
        return cls(str(ref), boundary)

    def macs(self, entry_hashes: Sequence[str]) -> list[str]:
        """Resolve the key ONCE for one scan or append; only the MACs leave this function."""
        try:
            with self.boundary.resolved({self.ref}, authorized_refs={self.ref}) as values:
                key = values[self.ref].reveal().encode("utf-8")
                if len(key) < MIN_MAC_KEY_BYTES:
                    raise ValueError("EXECUTION_LEDGER_KEY_UNAVAILABLE")
                return [hmac.new(key, _MAC_DOMAIN + item.encode("ascii"), hashlib.sha256).hexdigest()
                        for item in entry_hashes]
        except Exception:  # noqa: BLE001 - an unresolvable key never authenticates (and never leaks)
            pass
        raise ExecutionLedgerError("EXECUTION_LEDGER_KEY_UNAVAILABLE")


class _LedgerFile:
    """Verified, locked READ access to one ledger file (no append path)."""

    __slots__ = ("path", "lock_path", "_capacity", "_anchor", "_mac")

    def __init__(self, path: Path, *, capacity: int, anchor: AnchorView | None, mac: _MacKey | None) -> None:
        if type(capacity) is not int or not 1 <= capacity <= MAX_EXECUTIONS:
            raise ValueError("EXECUTION_LEDGER_CAPACITY_INVALID")
        if anchor is not None and not callable(getattr(anchor, "latest", None)):
            raise ValueError("EXECUTION_LEDGER_ANCHOR_INVALID")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._capacity = capacity
        self._anchor = None if anchor is None else _AnchorHead(anchor)
        self._mac = mac
        try:
            ensure_durable_dir(self.path.parent)
        except (OSError, ValueError) as exc:
            raise ValueError("EXECUTION_LEDGER_DIRECTORY_INVALID") from exc

    @property
    def durable(self) -> bool:
        return True

    @property
    def anchored(self) -> bool:
        """True when every read is checked against an external anchor."""
        return self._anchor is not None

    @property
    def authenticated(self) -> bool:
        """True when every entry's MAC is verified under the configured key reference."""
        return self._mac is not None

    def resolve(self, envelope_hash: str) -> LedgerEntry | None:
        """The recorded execution with this envelope hash, or ``None``; a damaged ledger raises (fail closed)."""
        if not _hex(envelope_hash):
            return None
        with self._locked():
            entries, _ = self._verified()
        return next((entry for entry in entries if entry.envelope_hash == envelope_hash), None)

    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._locked():
            return tuple(self._verified()[0])

    def verify(self) -> bool:
        try:
            with self._locked():
                self._verified()
        except ExecutionLedgerError:
            return False
        return True

    # --- internals (callers hold the lock) ------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            with exclusive_lock(self.lock_path):
                yield
        except ExecutionLedgerError:
            raise
        except (OSError, ValueError) as exc:  # lock file unusable (incl. FsPolicyError): fail closed
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNAVAILABLE") from exc

    def _exists(self) -> bool:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNAVAILABLE") from exc
        if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNSAFE")
        return True

    def _read(self) -> bytes:
        try:
            fd = os.open(self.path, _READ_FLAGS)
        except OSError as exc:
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNSAFE") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
                raise ExecutionLedgerError("EXECUTION_LEDGER_UNSAFE")
            if info.st_size > MAX_LEDGER_BYTES:
                raise ExecutionLedgerError("EXECUTION_LEDGER_CORRUPT")
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(MAX_LEDGER_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_LEDGER_BYTES:
            raise ExecutionLedgerError("EXECUTION_LEDGER_CORRUPT")
        return data

    def _verified(self) -> tuple[list[LedgerEntry], int]:
        """Every entry, verified, and the verified file size in bytes."""
        data = self._read() if self._exists() else b""
        if data and not data.endswith(b"\n"):  # a torn trailing entry (interrupted append): fail closed
            raise ExecutionLedgerError("EXECUTION_LEDGER_TORN_ENTRY")
        entries: list[LedgerEntry] = []
        macs: list[object] = []
        prev = GENESIS
        try:
            for raw in data.split(b"\n")[:-1] if data else ():
                if not raw or len(raw) > MAX_ENTRY_BYTES:
                    raise ValueError("EXECUTION_LEDGER_CORRUPT")
                parsed = json.loads(raw.decode("ascii"))
                if _line(parsed) != raw + b"\n":  # one canonical form only (no duplicate keys, no padding)
                    raise ValueError("EXECUTION_LEDGER_CORRUPT")
                entry = _validated(parsed, len(entries) + 1, prev)
                prev = entry.entry_hash
                entries.append(entry)
                macs.append(parsed.get(MAC_FIELD))
        except (ValueError, TypeError, KeyError, DataMinimizationViolation) as exc:
            raise ExecutionLedgerError("EXECUTION_LEDGER_CORRUPT") from exc
        if len(entries) > self._capacity:
            raise ExecutionLedgerError("EXECUTION_LEDGER_CORRUPT")
        self._check_macs(entries, macs)
        self._check_anchor(entries)
        return entries, len(data)

    def _check_macs(self, entries: Sequence[LedgerEntry], macs: Sequence[object]) -> None:
        if self._mac is None or not entries:
            return
        expected = self._mac.macs([entry.entry_hash for entry in entries])
        if not all(type(found) is str and hmac.compare_digest(found, wanted) for found, wanted in zip(
                macs, expected, strict=True)):
            raise ExecutionLedgerError("EXECUTION_LEDGER_MAC_INVALID")

    def _anchored(self) -> LedgerCheckpoint | None:
        if self._anchor is None:
            return None
        try:
            latest = self._anchor.latest()
        except Exception as exc:  # noqa: BLE001 - an anchor that cannot answer is a refusal, never a pass
            raise ExecutionLedgerError("EXECUTION_LEDGER_ANCHOR_UNAVAILABLE") from exc
        if latest is not None and not isinstance(latest, LedgerCheckpoint):
            raise ExecutionLedgerError("EXECUTION_LEDGER_ANCHOR_MISMATCH")
        return latest

    def _check_anchor(self, entries: Sequence[LedgerEntry]) -> None:
        if self._anchor is None:
            return
        head = LedgerCheckpoint(len(entries), entries[-1].entry_hash) if entries else None
        if self._anchored() != head:
            raise ExecutionLedgerError("EXECUTION_LEDGER_ANCHOR_MISMATCH")


class ExecutionLedgerReader(_LedgerFile):
    """READ-ONLY view of an execution ledger: ``resolve``, ``entries``, ``verify`` (CR-16b H-04).

    It has no append path and holds no reference to a writer: only the path, the capacity, the anchor's
    ``latest`` and the optional MAC key reference. Consumers accept exactly this type and nothing else."""

    __slots__ = ()

    def __init__(self, path: Path, *, capacity: int = MAX_EXECUTIONS, anchor: AnchorView | None = None,
                 mac_key_ref: str | None = None, boundary: SecretBoundary | None = None) -> None:
        super().__init__(path, capacity=capacity, anchor=anchor, mac=_MacKey.of(mac_key_ref, boundary))


class FileExecutionLedger(_LedgerFile):
    """The WRITER: durable, process-shared execution ledger held by the executor (``append_execution``).

    It keeps the read API for the executor's own checks; consumers get ``reader()`` instead."""

    __slots__ = ("_writer_anchor",)

    def __init__(self, path: Path, *, capacity: int = MAX_EXECUTIONS, anchor: ExecutionAnchor | None = None,
                 mac_key_ref: str | None = None, boundary: SecretBoundary | None = None) -> None:
        if anchor is not None and not callable(getattr(anchor, "record", None)):
            raise ValueError("EXECUTION_LEDGER_ANCHOR_INVALID")
        super().__init__(path, capacity=capacity, anchor=anchor, mac=_MacKey.of(mac_key_ref, boundary))
        self._writer_anchor = anchor

    def reader(self) -> ExecutionLedgerReader:
        """A read-only view of this ledger for consumers (it never references this writer)."""
        anchor = None if self._anchor is None else self._anchor.source
        mac = self._mac
        return ExecutionLedgerReader(self.path, capacity=self._capacity, anchor=anchor,
                                     mac_key_ref=None if mac is None else mac.ref,
                                     boundary=None if mac is None else mac.boundary)

    def _append_entry(self, fields: dict[str, Any]) -> LedgerEntry:
        """Called only by ``append_execution``: verify, de-duplicate, bound, append, fsync, anchor."""
        with self._locked():
            entries, size = self._verified()
            existing = next((item for item in entries if item.envelope_hash == fields["envelope_hash"]), None)
            if existing is not None:  # the same execution evidence (identical envelope) is recorded once
                if _content(existing) != {key: fields[key] for key in _CONTENT_KEYS}:
                    raise ExecutionLedgerError("EXECUTION_LEDGER_CONFLICT")
                return existing
            if len(entries) >= self._capacity:
                raise ExecutionLedgerError("EXECUTION_LEDGER_FULL")
            entry: dict[str, Any] = {"schema": EXECUTION_LEDGER_SCHEMA, "seq": len(entries) + 1, **fields,
                                     "prev_hash": entries[-1].entry_hash if entries else GENESIS}
            entry["entry_hash"] = _sealed(entry)
            if self._mac is not None:
                entry[MAC_FIELD] = self._mac.macs([entry["entry_hash"]])[0]
            try:
                _minimized(entry)
                line = _line(entry)
                recorded = _validated(json.loads(line), len(entries) + 1, entry["prev_hash"])
            except (ValueError, TypeError, KeyError, DataMinimizationViolation) as exc:
                raise ExecutionLedgerError("EXECUTION_LEDGER_ENTRY_INVALID") from exc
            if len(line) > MAX_ENTRY_BYTES:
                raise ExecutionLedgerError("EXECUTION_LEDGER_ENTRY_INVALID")
            if size + len(line) > MAX_LEDGER_BYTES:  # refuse BEFORE writing (CR-16b H-07)
                raise ExecutionLedgerError("EXECUTION_LEDGER_FULL")
            previous = LedgerCheckpoint(len(entries), entries[-1].entry_hash) if entries else None
            self._append(line, created=not entries and not self._exists())
            if self._writer_anchor is not None:
                try:
                    self._writer_anchor.record(previous, LedgerCheckpoint(recorded.seq, recorded.entry_hash))
                except Exception as exc:  # noqa: BLE001 - the file is now ahead of the anchor: fails closed later
                    raise ExecutionLedgerError("EXECUTION_LEDGER_ANCHOR_UNAVAILABLE") from exc
            return recorded

    def _append(self, line: bytes, *, created: bool) -> None:
        try:
            fd = os.open(self.path, _APPEND_FLAGS, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not _owner_only(info):
                    raise ExecutionLedgerError("EXECUTION_LEDGER_UNSAFE")
                _write_all(fd, line)  # every byte, or refuse
                os.fsync(fd)  # only after the COMPLETE entry is appended
            finally:
                os.close(fd)
            if created:
                fsync_directory(self.path.parent)
        except ExecutionLedgerError:
            raise
        except OSError as exc:
            raise ExecutionLedgerError("EXECUTION_LEDGER_UNAVAILABLE") from exc


_CONTENT_KEYS = ("envelope_hash", "plan_id", "plan_hash", "task_id", "task_type", "task_hash", "bindings_hash",
                 "status", "stages")


def _content(entry: LedgerEntry) -> dict[str, Any]:
    return {"envelope_hash": entry.envelope_hash, "plan_id": entry.plan_id, "plan_hash": entry.plan_hash,
            "task_id": entry.task_id, "task_type": entry.task_type, "task_hash": entry.task_hash,
            "bindings_hash": entry.bindings_hash, "status": entry.status,
            "stages": [stage.row() for stage in entry.stages]}


def append_execution(ledger: FileExecutionLedger, *, receipt: PlanReceipt, task_id: str, task_type: str,
                     task_hash: str, bindings_hash: str, status: str, recorded_at: datetime) -> LedgerEntry:
    """THE writer (executor only; static test): record one execution's receipt chain, content-free."""
    if type(ledger) is not FileExecutionLedger:
        raise ExecutionLedgerError("EXECUTION_LEDGER_INVALID")
    if status not in LEDGER_STATUSES or not receipt.verify_structure():
        raise ExecutionLedgerError("EXECUTION_LEDGER_ENTRY_INVALID")
    try:
        when = recorded_at.isoformat()
    except Exception as exc:  # noqa: BLE001 - no trustworthy timestamp means no entry
        raise ExecutionLedgerError("EXECUTION_LEDGER_ENTRY_INVALID") from exc
    fields: dict[str, Any] = {
        "envelope_hash": receipt.envelope_hash, "plan_id": receipt.plan_id, "plan_hash": receipt.plan_hash,
        "task_id": task_id, "task_type": task_type, "task_hash": task_hash, "bindings_hash": bindings_hash,
        "status": status, "stages": [LedgerStage.of(stage).row() for stage in receipt.stages], "recorded_at": when}
    return ledger._append_entry(fields)


def require_reader(ledger: object) -> ExecutionLedgerReader:
    """For consumers: exactly a read-only, ANCHORED reader, or a closed refusal (a writer is never accepted)."""
    if type(ledger) is not ExecutionLedgerReader:
        raise ExecutionLedgerError("EXECUTION_LEDGER_READER_REQUIRED")
    if not ledger.anchored:
        raise ExecutionLedgerError("EXECUTION_LEDGER_ANCHOR_REQUIRED")
    return ledger
