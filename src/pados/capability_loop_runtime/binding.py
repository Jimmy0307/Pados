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

``InMemorySessionBindingAuthority`` enforces the same immutable-binding semantics without an external anchor;
it is for ephemeral / test use where durable external anchoring is not the property under test.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability_loop.binding_authority import (
    BindingAuthorityError,
    BindingProof,
    GovernedSessionBinding,
)
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.learning.anchor import AnchorError, Checkpoint, ExternalAnchor, FileAnchor
from pados.learning.store import JsonlFile, LearningStoreError
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

GENESIS = "0" * 64
_SHA256 = r"^[0-9a-f]{64}$"
DEFAULT_MAX_BINDINGS = 100_000
_LINE_BYTES = 8 * 1024


class _BindingRecord(BaseModel):
    """One sealed, chained binding-log entry. The governed context fields ARE the authoritative binding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=_SHA256)
    entry_hash: str = Field(pattern=_SHA256)
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
    def seal(cls, binding: GovernedSessionBinding, *, seq: int, prev_hash: str) -> _BindingRecord:
        data = {
            "seq": seq, "prev_hash": prev_hash, "provider_id": binding.provider_id,
            "external_session_ref": binding.external_session_ref, "project_id": binding.project_id,
            "work_contract_id": binding.work_contract_id, "classification": binding.classification.value,
            "capability_gap_ref": binding.capability_gap_ref, "created_at": binding.created_at}
        return cls.model_validate({**data, "entry_hash": _seal(data)})


def _seal(data: dict[str, Any]) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != "entry_hash"})


_RECORD_KEYS = frozenset(_BindingRecord.model_fields)


class AnchoredSessionBindingAuthority:
    """``SessionBindingAuthority`` over an append-only binding log anchored by an independent ``FileAnchor``."""

    def __init__(self, log_path: Path, anchor: ExternalAnchor, *,
                 max_bindings: int = DEFAULT_MAX_BINDINGS) -> None:
        if not 1 <= max_bindings <= DEFAULT_MAX_BINDINGS:
            raise ValueError("BINDING_LOG_BOUND_INVALID")
        self._file = JsonlFile(Path(log_path), max_line_bytes=_LINE_BYTES)
        self._anchor = anchor
        self._max = max_bindings

    # -- public contract ----------------------------------------------------
    def bind(self, binding: GovernedSessionBinding) -> BindingProof:
        with self._file.locked():
            records = self._validated()
            existing = _find(records, binding.identity)
            if existing is not None:
                if existing.binding.context_hash != binding.context_hash:
                    raise BindingAuthorityError("BINDING_CONFLICT")  # no rebind to a different context
                return self._proof(existing)  # idempotent: identical binding already recorded
            if len(records) + 1 > self._max:
                raise BindingAuthorityError("BINDING_LOG_FULL")
            previous = records[-1] if records else None
            record = _BindingRecord.seal(binding, seq=previous.seq + 1 if previous else 1,
                                         prev_hash=previous.entry_hash if previous else GENESIS)
            self._commit(records, [*records, record])
            return self._proof(record)

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        with self._file.locked():
            records = self._validated()
        found = _find(records, (provider_id, external_session_ref))
        return None if found is None else found.binding

    # -- internals ----------------------------------------------------------
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
        try:
            lines = self._file.read_lines()
        except LearningStoreError as exc:
            raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
        records: list[_BindingRecord] = []
        contexts: dict[tuple[str, str], str] = {}
        for index, line in enumerate(lines, start=1):
            try:
                assert_minimized(line, allowed_keys=_RECORD_KEYS, max_string=160, max_items=16,
                                 max_total_bytes=_LINE_BYTES - 512)
                record = _BindingRecord.model_validate(line)  # _sealed() re-validates the governed contract
            except (ValueError, DataMinimizationViolation) as exc:
                raise BindingAuthorityError("BINDING_LOG_CORRUPT") from exc
            previous = records[-1] if records else None
            if (record.seq != index or record.prev_hash != (previous.entry_hash if previous else GENESIS)):
                raise BindingAuthorityError("BINDING_LOG_CHAIN_BROKEN")
            prior_context = contexts.get(record.binding.identity)
            if prior_context is not None and prior_context != record.binding.context_hash:
                raise BindingAuthorityError("BINDING_LOG_INCONSISTENT")  # one identity, two governed contexts
            contexts[record.binding.identity] = record.binding.context_hash
            records.append(record)
        return records

    def _validated(self) -> list[_BindingRecord]:
        """Chain-verified records whose head IS the independently anchored checkpoint. Fails closed otherwise.

        This runs before every bind and before every resolve, so a log advanced past the anchor (or an anchor
        advanced past the log) is never treated as healthy.
        """
        records = self._load()
        anchored = self._anchor_latest()
        if records:
            if anchored is None:
                raise BindingAuthorityError("BINDING_NOT_ANCHORED")
            if records[-1].checkpoint != anchored:
                raise BindingAuthorityError("BINDING_HEAD_NOT_ANCHORED")
        elif anchored is not None:
            raise BindingAuthorityError("BINDING_ANCHOR_AHEAD_OF_LOG")
        return records

    def _commit(self, before: list[_BindingRecord], after: list[_BindingRecord]) -> None:
        """Append the new record, then advance the anchor by compare-and-set from the validated head; on any
        anchor refusal/unavailability restore ``before`` and fail closed, so the anchor keeps the trusted head
        and a substituted chain is never laundered (mirrors the advice-log commit ordering)."""
        self._file.append_line(_minimized(after[-1]))
        anchored = before[-1].checkpoint if before else None
        try:
            self._anchor.record(anchored, after[-1].checkpoint)
        except Exception as exc:  # noqa: BLE001 - the anchor decides; any failure restores the previous state
            self._file.rewrite([_minimized(record) for record in before])
            refused = isinstance(exc, AnchorError) and exc.code in {"ANCHOR_CONFLICT", "ANCHOR_NOT_CONTINUING"}
            raise BindingAuthorityError("BINDING_ANCHOR_REFUSED" if refused
                                        else "BINDING_ANCHOR_UNAVAILABLE") from exc


def _minimized(record: _BindingRecord) -> dict[str, Any]:
    data = record.model_dump(mode="json")
    assert_minimized(data, allowed_keys=_RECORD_KEYS, max_string=160, max_items=16,
                     max_total_bytes=_LINE_BYTES - 512)
    return data


def _find(records: list[_BindingRecord], identity: tuple[str, str]) -> _BindingRecord | None:
    return next((record for record in records if record.binding.identity == identity), None)


def anchored_session_binding_authority(log_path: Path, anchor_directory: Path) -> AnchoredSessionBindingAuthority:
    """Build the authority from a binding-log path and a SEPARATE anchor directory (owner-only, outside the log)."""
    return AnchoredSessionBindingAuthority(Path(log_path), FileAnchor(Path(anchor_directory)))


class InMemorySessionBindingAuthority:
    """Immutable-binding authority without an external anchor (ephemeral / test use; not tail-anchored).

    Enforces the same no-rebind semantics as the anchored authority, so authorization logic can be exercised
    without durable anchoring; it does NOT provide the R4-F4 whole-file-rewrite protection.
    """

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], GovernedSessionBinding] = {}
        self._seq = 0

    def bind(self, binding: GovernedSessionBinding) -> BindingProof:
        existing = self._bindings.get(binding.identity)
        if existing is not None:
            if existing.context_hash != binding.context_hash:
                raise BindingAuthorityError("BINDING_CONFLICT")
            return BindingProof(context_hash=binding.context_hash, sequence=self._seq,
                                anchor_checkpoint_hash=GENESIS)
        self._seq += 1
        self._bindings[binding.identity] = binding
        return BindingProof(context_hash=binding.context_hash, sequence=self._seq,
                            anchor_checkpoint_hash=GENESIS)

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        return self._bindings.get((provider_id, external_session_ref))
