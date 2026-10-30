# SPDX-License-Identifier: AGPL-3.0-only
"""Externally anchored ``SessionBindingAuthority`` implementations (outside the capability_loop boundary).

``AnchoredSessionBindingAuthority`` is an append-only, hash-chained binding log (``JsonlFile``) whose tail is
authenticated by an independent ``FileAnchor`` (``pados.learning.anchor``) placed in a SEPARATE owner-only
directory. It reuses the advice-log commit ordering: every mutation first validates the log against the
anchor (head must equal the anchored checkpoint), writes the new entry, then advances the anchor by
compare-and-set from the validated head, rolling the log back and failing closed if the anchor refuses or is
unavailable. A whole-file rewrite with a recomputed chain is therefore refused because the independent anchor
still holds the trusted checkpoint. Security level: INDEPENDENTLY_ANCHORED_TAMPER_EVIDENT (see
``pados.capability_loop.binding_authority``); it is not cryptographic tamper-proof.

Every log record is one state transition of one binding identity (``STAGED`` -> ``COMMITTED`` | ``ABORTED``),
carrying the full governed context, so the log is self-describing; loading re-validates that transition grammar
(even under a matching anchor) and only ``COMMITTED`` identities resolve. A ``COMMITTED`` record names the
creation evidence (receipt hash) it was finalized against. ``max_bindings`` bounds log records; a created
session uses two.

Crash/failure outcomes of the creation protocol (``LearningService.create_session``):

* before ``stage`` is anchored: no record (or a rolled-back one) -- nothing exists, nothing resolves;
* after ``stage``, before/while the creation receipt is written: the identity is pending -- never resolvable;
  the service aborts it when it can, otherwise it stays pending (still fail closed);
* after the receipt, before ``commit`` is anchored: pending -- never resolvable; the receipt chain then shows a
  RECORDED creation whose binding was never finalized (operational evidence over-reports; authority does not);
* inside any transition, between the log append and the anchor advance (or a torn append): the next operation
  discards that single unanchored tail (``_recover``) and continues; it is never anchored after the fact.

``InMemorySessionBindingAuthority`` enforces the same transition semantics without an external anchor; it is
for ephemeral / test use where durable external anchoring is not the property under test.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability_loop.binding_authority import (
    BindingAuthorityError,
    BindingProof,
    BindingState,
    GovernedSessionBinding,
)
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.learning.anchor import AnchorError, Checkpoint, ExternalAnchor, FileAnchor
from pados.learning.store import JsonlFile, LearningStoreError
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

GENESIS = "0" * 64
_SHA256 = r"^[0-9a-f]{64}$"
_SHA256_RE = re.compile(_SHA256)
DEFAULT_MAX_BINDINGS = 100_000
# A binding record at contract-maximum sizes (four 96-char ids + a 128-char session ref + three 64-char
# hashes + state + timestamp + json overhead) is about 1 KiB; 2 KiB gives ample headroom so every value
# GovernedSessionBinding accepts fits, and the minimization bound stays under the per-line cap.
_LINE_BYTES = 2048
_RECORD_BYTES = _LINE_BYTES - 256


class _BindingRecord(BaseModel):
    """One sealed, chained binding-log entry: one state transition of one governed binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=_SHA256)
    entry_hash: str = Field(pattern=_SHA256)
    state: BindingState
    evidence_ref: str | None = Field(default=None, pattern=_SHA256)
    provider_id: str
    external_session_ref: str
    project_id: str
    work_contract_id: str
    classification: DataClassification
    capability_gap_ref: str
    created_at: str

    @model_validator(mode="after")
    def _sealed(self) -> _BindingRecord:
        if self.entry_hash != _seal(self.model_dump(mode="json")):
            raise ValueError("BINDING_RECORD_HASH_MISMATCH")
        if self.evidence_ref is not None and self.state is not BindingState.COMMITTED:
            raise ValueError("BINDING_EVIDENCE_ON_NON_COMMIT")  # only a finalization names creation evidence
        _ = self.binding  # constructing the governed binding validates ids / session ref / aware timestamp
        return self

    @property
    def binding(self) -> GovernedSessionBinding:
        return GovernedSessionBinding(
            provider_id=self.provider_id, external_session_ref=self.external_session_ref,
            project_id=self.project_id, work_contract_id=self.work_contract_id,
            classification=self.classification, capability_gap_ref=self.capability_gap_ref,
            created_at=self.created_at)

    @property
    def checkpoint(self) -> Checkpoint:
        return Checkpoint(self.seq, self.entry_hash)

    @classmethod
    def seal(cls, binding: GovernedSessionBinding, *, seq: int, prev_hash: str,
             state: BindingState | str = BindingState.STAGED, evidence_ref: str | None = None) -> _BindingRecord:
        data = {
            "seq": seq, "prev_hash": prev_hash, "state": BindingState(state).value, "evidence_ref": evidence_ref,
            "provider_id": binding.provider_id, "external_session_ref": binding.external_session_ref,
            "project_id": binding.project_id, "work_contract_id": binding.work_contract_id,
            "classification": binding.classification.value, "capability_gap_ref": binding.capability_gap_ref,
            "created_at": binding.created_at}
        return cls.model_validate({**data, "entry_hash": _seal(data)})


def _seal(data: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != "entry_hash"})


_RECORD_KEYS = frozenset(_BindingRecord.model_fields)


@dataclass(frozen=True)
class _Head:
    """The latest transition of one identity (what decides whether and how it may move next)."""

    state: BindingState
    context_hash: str
    evidence_ref: str | None
    binding: GovernedSessionBinding


def _advances(head: _Head | None, binding: GovernedSessionBinding, target: BindingState,
              evidence_ref: str | None) -> bool:
    """Decide one requested transition: True -> write it, False -> idempotent no-op; otherwise fail closed.

    STAGED -> COMMITTED | ABORTED; COMMITTED and ABORTED are terminal. Shared by both implementations so the
    anchored and in-memory authorities can never disagree on what a transition means.
    """
    if evidence_ref is not None and (target is not BindingState.COMMITTED or not _SHA256_RE.fullmatch(evidence_ref)):
        raise BindingAuthorityError("BINDING_EVIDENCE_REF_INVALID")  # only a finalize names evidence: a SHA-256
    if head is None:
        if target is BindingState.STAGED:
            return True
        raise BindingAuthorityError("BINDING_NOT_STAGED")
    if head.context_hash != binding.context_hash:
        raise BindingAuthorityError("BINDING_CONFLICT")  # one identity, never two governed contexts
    if head.state is BindingState.STAGED:
        return target is not BindingState.STAGED  # re-stage of a pending binding is idempotent
    if head.state is BindingState.ABORTED:
        if target is BindingState.ABORTED:
            return False
        raise BindingAuthorityError("BINDING_ABORTED")  # a retired identity is never revived
    if target is BindingState.COMMITTED and head.evidence_ref == evidence_ref:
        return False  # the same finalization again
    raise BindingAuthorityError("BINDING_ALREADY_COMMITTED")  # never aborted, re-staged or re-pointed


@dataclass(frozen=True)
class _LogView:
    records: tuple[_BindingRecord, ...]
    heads: dict[tuple[str, str], _BindingRecord]
    torn: bool = False  # bytes after the last newline: an append interrupted mid-write (not parsed)
    tail: bytes = b""  # those bytes (only meaningful when torn)


def _head(records: list[_BindingRecord] | tuple[_BindingRecord, ...]) -> Checkpoint | None:
    return records[-1].checkpoint if records else None


def _record_head(record: _BindingRecord) -> _Head:
    return _Head(record.state, record.binding.context_hash, record.evidence_ref, record.binding)


class AnchoredSessionBindingAuthority:
    """``SessionBindingAuthority`` over an append-only binding log anchored by an independent ``FileAnchor``."""

    def __init__(self, log_path: Path, anchor: ExternalAnchor, *,
                 max_bindings: int = DEFAULT_MAX_BINDINGS) -> None:
        if not 1 <= max_bindings <= DEFAULT_MAX_BINDINGS:
            raise ValueError("BINDING_LOG_BOUND_INVALID")
        # Size the readable byte bound to the record cap so the log can never exceed the read limit before
        # reaching ``max_bindings`` (which read_lines would otherwise reject as LEARNING_STORE_TOO_LARGE,
        # bricking every existing session).
        self._file = JsonlFile(Path(log_path), max_bytes=max_bindings * _LINE_BYTES, max_line_bytes=_LINE_BYTES)
        self._anchor = anchor
        self._max = max_bindings
        # Recovery never deletes silently: every discarded tail is first appended (fsynced) here, owner-only.
        self._discards = JsonlFile(Path(log_path).with_name(Path(log_path).name + ".discarded"),
                                   max_line_bytes=2 * _LINE_BYTES)
        # Parsed-log cache keyed on the SHA-256 of the exact bytes read, never on file metadata: mtime can be
        # restored, Windows st_ctime_ns is creation time, and coarse timestamps can survive a same-size
        # rewrite. Every lookup still reads the whole bounded log; only the parse/validation of identical
        # bytes is reused, so any byte change is re-validated (and checked against the anchor) on every
        # platform.
        self._cache: tuple[str, _LogView] | None = None

    # -- public contract ----------------------------------------------------
    def stage(self, binding: GovernedSessionBinding) -> BindingProof:
        return self._transition(binding, BindingState.STAGED, None)

    def commit(self, binding: GovernedSessionBinding, *, evidence_ref: str | None) -> BindingProof:
        return self._transition(binding, BindingState.COMMITTED, evidence_ref)

    def abort(self, binding: GovernedSessionBinding) -> None:
        self._transition(binding, BindingState.ABORTED, None)

    def bind(self, binding: GovernedSessionBinding) -> BindingProof:
        """One-shot ``stage`` + ``commit`` with no separate creation evidence, for fixtures and callers that own
        no evidence store. Idempotent for an identical binding. ``LearningService`` never uses it: a created
        session is committed only against its durable creation receipt."""
        return _bind_once(self, binding)

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        with self._normalized():
            with self._file.locked():
                view = self._validated()
        head = view.heads.get((provider_id, external_session_ref))
        return head.binding if head is not None and head.state is BindingState.COMMITTED else None

    # -- internals ----------------------------------------------------------
    def _transition(self, binding: GovernedSessionBinding, target: BindingState,
                    evidence_ref: str | None) -> BindingProof:
        with self._normalized():
            with self._file.locked():
                view = self._validated()
                current = view.heads.get(binding.identity)
                if not _advances(None if current is None else _record_head(current), binding, target,
                                 evidence_ref):
                    assert current is not None  # an idempotent no-op always has a head
                    return self._proof(current)
                if len(view.records) + 1 > self._max:
                    raise BindingAuthorityError("BINDING_LOG_FULL")
                previous = view.records[-1] if view.records else None
                # A transition record repeats the ORIGINAL staged context (incl. created_at) verbatim.
                source = binding if current is None else current.binding
                record = _BindingRecord.seal(source, seq=previous.seq + 1 if previous else 1,
                                             prev_hash=previous.entry_hash if previous else GENESIS,
                                             state=target, evidence_ref=evidence_ref)
                self._commit(list(view.records), [*view.records, record])
                return self._proof(record)

    @contextmanager
    def _normalized(self) -> Iterator[None]:
        """Translate filesystem / lock / parse failures into the authority's closed error vocabulary, so a
        caller (LearningService) always sees a BindingAuthorityError rather than a raw store exception."""
        try:
            yield
        except BindingAuthorityError:
            raise
        except (LearningStoreError, DataMinimizationViolation, ValueError) as exc:
            raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
        except OSError as exc:  # I/O, permissions, lock timeout: unavailable, not evidence of corruption
            raise BindingAuthorityError("BINDING_LOG_UNAVAILABLE") from exc

    def _proof(self, record: _BindingRecord) -> BindingProof:
        return BindingProof(context_hash=record.binding.context_hash, sequence=record.seq,
                            anchor_checkpoint_hash=record.entry_hash)

    def _anchor_latest(self) -> Checkpoint | None:
        try:
            latest = self._anchor.latest()
        except Exception as exc:  # noqa: BLE001 - an unreachable anchor fails closed
            raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE") from exc
        if not (latest is None or isinstance(latest, Checkpoint)):
            raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE")
        return latest

    def _load(self) -> list[_BindingRecord]:
        return list(self._view().records)

    def _view(self) -> _LogView:
        try:
            payload = self._file.read_payload()  # also enforces regular file / owner-only / no link
        except LearningStoreError as exc:
            raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if self._cache is not None and self._cache[0] == digest:
            return self._cache[1]  # byte-identical to the last validated parse (anchor checked by caller)
        # Every append writes one complete newline-terminated line; bytes after the last newline can only be an
        # append interrupted mid-write. They are never parsed; _validated decides whether they are recoverable.
        complete, torn = payload, bool(payload) and not payload.endswith(b"\n")
        if torn:
            complete = payload[:payload.rfind(b"\n") + 1]
        try:
            lines = self._file.parse_payload(complete)
        except LearningStoreError as exc:
            raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
        view = _parse_log(lines, torn=torn, tail=payload[len(complete):])
        self._cache = (digest, view)
        return view

    def _validated(self) -> _LogView:
        """Chain-verified records whose head IS the independently anchored checkpoint. Fails closed otherwise.

        This runs before every transition and before every resolve, so a log advanced past the anchor (or an
        anchor advanced past the log) is never treated as healthy; the only repair is ``_recover`` discarding the
        single unanchored tail an interrupted commit leaves.
        """
        view = self._view()
        anchored = self._anchor_latest()
        if view.torn or _head(view.records) != anchored:
            view = self._recover(view, anchored)
        return view

    def _recover(self, view: _LogView, anchored: Checkpoint | None) -> _LogView:
        """Recover the one state an interrupted commit can leave; fail closed on anything else.

        The log is appended (and fsynced) before the anchor advances, under the cross-process lock, so a crash
        or escaped exception between the two leaves exactly one unanchored record (or a torn, newline-less
        partial line) after the anchored head. That tail was never authority: it is DISCARDED, never anchored
        after the fact (anchoring it would let a log writer launder a forged transition). Two or more unanchored
        records, a substituted anchored head, a truncated log or an anchor ahead of the log are tampering or
        loss and still fail closed.
        """
        records = list(view.records)
        completed = _completed_tail(view) if view.torn else None
        discard: dict[str, Any] | None = None
        if completed is not None and _head(completed.records) == anchored:
            kept = list(completed.records)  # a complete, ANCHORED last line that only lost its newline: keep it
        elif view.torn and _head(records) == anchored:
            kept, discard = records, _discard_entry("TORN_TAIL", None, view.tail, anchored)
        elif not view.torn and records and _head(records[:-1]) == anchored:
            tail = records[-1].model_dump(mode="json")
            kept = records[:-1]
            discard = _discard_entry("UNANCHORED_TAIL", tail, self._file.encode(tail), anchored)
        elif records and anchored is None:
            raise BindingAuthorityError("BINDING_NOT_ANCHORED")
        elif records:
            raise BindingAuthorityError("BINDING_HEAD_NOT_ANCHORED")
        else:
            raise BindingAuthorityError("BINDING_ANCHOR_AHEAD_OF_LOG")
        if discard is not None:
            self._preserve(discard)  # durable copy first; if this fails nothing is truncated
        self._rollback_log(kept)
        return _parse_log([_minimized(record) for record in kept])

    def _preserve(self, entry: dict[str, Any]) -> None:
        try:
            self._discards.append_line(entry)
        except (LearningStoreError, OSError, ValueError) as exc:
            raise BindingAuthorityError("BINDING_LOG_WRITE_FAILED") from exc

    def _commit(self, before: list[_BindingRecord], after: list[_BindingRecord]) -> None:
        """Append the new record, then advance the anchor by compare-and-set from the validated head; on any
        anchor refusal/unavailability reconcile against the anchor's actual head and fail closed, so the anchor
        keeps the trusted head and a substituted chain is never laundered (mirrors the advice-log ordering).

        Any persistence failure (oversized/minimization/IO) is normalized to ``BindingAuthorityError`` so the
        caller always sees the authority contract, never a raw store exception.
        """
        anchored = before[-1].checkpoint if before else None
        try:
            self._file.append_line(_minimized(after[-1]))
        except (DataMinimizationViolation, LearningStoreError, OSError, ValueError) as exc:
            # The append may have made the record visible on disk before raising (e.g. the bytes landed but a
            # following fsync failed). The anchor has NOT advanced yet, so reconcile the log back to the
            # validated pre-append state; otherwise the log would sit one entry ahead of the anchor and every
            # later bind/resolve would fail closed (BINDING_HEAD_NOT_ANCHORED), disabling all existing sessions.
            self._rollback_log(before)
            raise BindingAuthorityError("BINDING_LOG_WRITE_FAILED") from exc
        try:
            self._anchor.record(anchored, after[-1].checkpoint)
        except Exception as exc:  # noqa: BLE001 - the anchor decides; reconcile, then fail closed
            self._reconcile_failed_anchor(exc, before, after, anchored)

    def _reconcile_failed_anchor(self, exc: Exception, before: list[_BindingRecord],
                                 after: list[_BindingRecord], anchored: Checkpoint | None) -> None:
        """``record()`` may have committed the new checkpoint before raising (e.g. os.replace succeeded but a
        following fsync failed). Inspect the anchor's actual head rather than assuming nothing changed.
        """
        try:
            current = self._anchor.latest()
        except Exception as read_exc:  # noqa: BLE001 - an unreadable anchor after the write fails closed
            # Includes a failed first-anchor initialization (sentinel written, head write failed -> head lost):
            # roll the orphan log record back so the log is clean; the anchor stays in its fail-closed state.
            self._rollback_log(before)
            raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE") from read_exc
        if current == after[-1].checkpoint:
            return  # the anchor DID advance; log and anchor are consistent -- keep the appended record
        if current != anchored:
            raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE") from exc  # unexpected head: never guess
        self._rollback_log(before)  # anchor unchanged: roll back the orphan record
        refused = isinstance(exc, AnchorError) and exc.code in {"ANCHOR_CONFLICT", "ANCHOR_NOT_CONTINUING"}
        raise BindingAuthorityError("BINDING_ANCHOR_REFUSED" if refused
                                    else "BINDING_ANCHOR_UNAVAILABLE") from exc

    def _rollback_log(self, before: list[_BindingRecord]) -> None:
        """Restore the log to ``before``; a failed rollback is normalized (the next op re-validates closed)."""
        try:
            self._file.rewrite([_minimized(record) for record in before])
        except (DataMinimizationViolation, LearningStoreError, OSError, ValueError) as exc:
            raise BindingAuthorityError("BINDING_LOG_WRITE_FAILED") from exc


def _parse_log(lines: list[dict[str, Any]], *, torn: bool = False, tail: bytes = b"") -> _LogView:
    """Validate every record, the hash chain, and each identity's transition grammar (fail closed)."""
    records: list[_BindingRecord] = []
    heads: dict[tuple[str, str], _BindingRecord] = {}
    for index, line in enumerate(lines, start=1):
        try:
            assert_minimized(line, allowed_keys=_RECORD_KEYS, max_string=160, max_items=16,
                             max_total_bytes=_RECORD_BYTES)
            record = _BindingRecord.model_validate(line)  # _sealed() re-validates the governed contract
        except (ValueError, DataMinimizationViolation) as exc:
            raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
        previous = records[-1] if records else None
        if record.seq != index or record.prev_hash != (previous.entry_hash if previous else GENESIS):
            raise BindingAuthorityError("BINDING_LOG_CHAIN_BROKEN")
        prior = heads.get(record.binding.identity)
        if prior is None:
            legal = record.state is BindingState.STAGED
        else:
            legal = (prior.state is BindingState.STAGED and record.state is not BindingState.STAGED
                     and prior.binding == record.binding)  # same context AND the original created_at
        if not legal:
            raise BindingAuthorityError("BINDING_LOG_INCONSISTENT")  # illegal transition or second context
        heads[record.binding.identity] = record
        records.append(record)
    return _LogView(tuple(records), heads, torn, tail if torn else b"")


def _completed_tail(view: _LogView) -> _LogView | None:
    """The log with its newline-less tail parsed as one more record, if that tail is a complete, valid, chained
    transition; else None (a genuinely torn write). Whether it is kept is decided against the anchor."""
    try:
        value = json.loads(view.tail.decode("utf-8"))
        if not isinstance(value, dict):
            return None
        return _parse_log([*(record.model_dump(mode="json") for record in view.records), value])
    except (ValueError, BindingAuthorityError):
        return None


def _discard_entry(reason: str, record: dict[str, Any] | None, raw: bytes,
                   anchored: Checkpoint | None) -> dict[str, Any]:
    """What recovery preserves before truncating: the record itself (an unanchored transition) or, for a torn
    write whose bytes are not a record, only their length and digest."""
    return {"reason": reason, "record": record, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "anchored_seq": 0 if anchored is None else anchored.seq}


def _bind_once(authority: AnchoredSessionBindingAuthority | InMemorySessionBindingAuthority,
               binding: GovernedSessionBinding) -> BindingProof:
    try:
        authority.stage(binding)
    except BindingAuthorityError as exc:
        if exc.code != "BINDING_ALREADY_COMMITTED":
            raise
    return authority.commit(binding, evidence_ref=None)


def _minimized(record: _BindingRecord) -> dict[str, Any]:
    data = record.model_dump(mode="json")
    assert_minimized(data, allowed_keys=_RECORD_KEYS, max_string=160, max_items=16,
                     max_total_bytes=_RECORD_BYTES)
    return data


def anchored_session_binding_authority(log_path: Path, anchor_directory: Path) -> AnchoredSessionBindingAuthority:
    """Build the authority from a binding-log path and a SEPARATE anchor directory (owner-only, outside the log).

    The anchor is the independent tamper-evidence root: it must not live in, contain, or equal the directory
    that holds the binding log. A colocated anchor shares the log's write scope, so a single whole-directory
    rewrite could restamp both and defeat the anchoring; such a layout is rejected (fail closed) at construction.
    """
    log = Path(log_path)
    anchor = Path(anchor_directory)
    log_dir = log.resolve().parent
    anchor_dir = anchor.resolve()
    if anchor_dir.is_relative_to(log_dir) or log_dir.is_relative_to(anchor_dir):
        raise BindingAuthorityError("BINDING_ANCHOR_NOT_SEPARATED")
    return AnchoredSessionBindingAuthority(log, FileAnchor(anchor))


@dataclass(frozen=True)
class _MemoryEntry:
    head: _Head
    sequence: int


class InMemorySessionBindingAuthority:
    """Same transition semantics as the anchored authority, without an external anchor (ephemeral / test use).

    It does NOT provide the R4-F4 whole-file-rewrite protection. Sequences count transitions exactly like the
    anchored log, and an idempotent transition returns the ORIGINAL sequence of the state it repeats.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _MemoryEntry] = {}
        self._seq = 0
        self._lock = threading.Lock()  # serialize the check/increment/store so no concurrent rebind slips in

    def stage(self, binding: GovernedSessionBinding) -> BindingProof:
        return self._transition(binding, BindingState.STAGED, None)

    def commit(self, binding: GovernedSessionBinding, *, evidence_ref: str | None) -> BindingProof:
        return self._transition(binding, BindingState.COMMITTED, evidence_ref)

    def abort(self, binding: GovernedSessionBinding) -> None:
        self._transition(binding, BindingState.ABORTED, None)

    def bind(self, binding: GovernedSessionBinding) -> BindingProof:
        """One-shot ``stage`` + ``commit`` (see ``AnchoredSessionBindingAuthority.bind``)."""
        return _bind_once(self, binding)

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        with self._lock:
            entry = self._entries.get((provider_id, external_session_ref))
        if entry is None or entry.head.state is not BindingState.COMMITTED:
            return None
        return entry.head.binding

    def _transition(self, binding: GovernedSessionBinding, target: BindingState,
                    evidence_ref: str | None) -> BindingProof:
        with self._lock:
            entry = self._entries.get(binding.identity)
            if not _advances(None if entry is None else entry.head, binding, target, evidence_ref):
                assert entry is not None
                return BindingProof(context_hash=binding.context_hash, sequence=entry.sequence,
                                    anchor_checkpoint_hash=GENESIS)
            self._seq += 1
            original = binding if entry is None else entry.head.binding
            self._entries[binding.identity] = _MemoryEntry(
                _Head(target, original.context_hash, evidence_ref, original), self._seq)
            return BindingProof(context_hash=binding.context_hash, sequence=self._seq,
                                anchor_checkpoint_hash=GENESIS)
