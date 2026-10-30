# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded file store for loop receipts, capability candidates and learning progress (R1, no schema change).

The store is operator-placed (``store_path`` in the loop config), owner-only, written atomically under a
cross-process lock, and every row passes ``assert_minimized`` before it reaches disk. It is non-authoritative
operational evidence (``DataKind.CAPABILITY_STAGE_EVIDENCE``): deleting it never changes PADOS authority.
A corrupt or tampered store is refused as a whole rather than repaired or guessed.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pados.capability_loop.contracts import CapabilityCandidate
from pados.capability_loop.learning_contracts import (
    LearningSessionStatus,
    MasteryLevel,
    ModelRouting,
    is_valid_session_ref,
)
from pados.capability_loop.receipts import GENESIS, OperationReceipt, is_valid_receipt_id, verify_chain
from pados.connectivity.storage import atomic_write_json, read_json, store_lock
from pados.enterprise.classification import DataClassification
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

STORE_SCHEMA = "pados.capability_loop.store.v1"
DEFAULT_MAX_RECEIPTS = 5000
DEFAULT_MAX_CANDIDATES = 2000
_KEYS = frozenset({"schema_version", "receipts", "candidates", "progress"})


class LoopStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate_candidates(candidates: dict[Any, Any]) -> None:
    """Every candidate validates as a model, its key equals its ``candidate_id``, and it is minimized.

    The data-minimization guard enforced on write is reapplied on load: a tampered row that satisfies the
    model (for example a ``description`` carrying a credential-shaped value) but violates minimization is
    refused.
    """
    for key, value in candidates.items():
        if not isinstance(key, str):
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        try:
            candidate = CapabilityCandidate.model_validate(value)
        except (TypeError, ValueError) as exc:
            raise LoopStoreError("LOOP_STORE_CORRUPT") from exc
        if candidate.candidate_id != key:
            raise LoopStoreError("LOOP_STORE_CANDIDATE_KEY_MISMATCH")
        try:
            assert_minimized(candidate.model_dump(mode="json"))
        except DataMinimizationViolation as exc:
            raise LoopStoreError("LOOP_STORE_CORRUPT") from exc


# The learning progress projection (ADVISORY_PROJECTION_ONLY) has one schema, enforced on write AND on load.
# A legacy (pre-binding) row carries the required fields only; a current row adds the governed work contract
# and classification together; an assessed row adds mastery and the never-granting flag together.
_PROGRESS_REQUIRED = frozenset({"project_id", "capability_gap_ref", "provider_id", "session_status",
                                "model_routing", "progress_percent"})
_PROGRESS_BINDING = frozenset({"work_contract_id", "classification"})
_PROGRESS_ASSESSMENT = frozenset({"human_mastery", "grants_capability"})
_PROGRESS_KEYS = _PROGRESS_REQUIRED | _PROGRESS_BINDING | _PROGRESS_ASSESSMENT
_PROGRESS_IDS = ("project_id", "capability_gap_ref", "provider_id", "work_contract_id")
_PROGRESS_ENUMS: dict[str, frozenset[str]] = {
    "session_status": frozenset(LearningSessionStatus), "model_routing": frozenset(ModelRouting),
    "classification": frozenset(DataClassification), "human_mastery": frozenset(MasteryLevel)}


def _progress_row_valid(row: dict[Any, Any]) -> bool:
    keys = set(row)
    if not (keys <= _PROGRESS_KEYS and _PROGRESS_REQUIRED <= keys):
        return False
    if len(keys & _PROGRESS_BINDING) == 1 or len(keys & _PROGRESS_ASSESSMENT) == 1:
        return False  # incoherent: half a binding or half an assessment
    if any(name in row and not is_valid_receipt_id(row[name]) for name in _PROGRESS_IDS):
        return False
    if any(name in row and not (isinstance(row[name], str) and row[name] in allowed)
           for name, allowed in _PROGRESS_ENUMS.items()):
        return False
    percent = row["progress_percent"]
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        return False
    return "grants_capability" not in row or row["grants_capability"] is False  # learning never grants


def _validate_progress(progress: dict[Any, Any]) -> None:
    """Every progress row matches the projection schema and is keyed by a canonical session ref."""
    for key, row in progress.items():
        if (not isinstance(key, str) or not is_valid_session_ref(key) or not isinstance(row, dict)
                or not _progress_row_valid(row)):
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        try:
            assert_minimized(row, allowed_keys=_PROGRESS_KEYS)
        except DataMinimizationViolation as exc:
            raise LoopStoreError("LOOP_STORE_CORRUPT") from exc


class LoopStore:
    def __init__(self, path: Path, *, max_receipts: int = DEFAULT_MAX_RECEIPTS,
                 max_candidates: int = DEFAULT_MAX_CANDIDATES, clock: Callable[[], str] = _now) -> None:
        self.path = Path(path)
        self.max_receipts = max_receipts
        self.max_candidates = max_candidates
        self.clock = clock

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": STORE_SCHEMA, "receipts": [], "candidates": {}, "progress": {}}
        data = read_json(self.path)
        if not isinstance(data, dict) or set(data) != _KEYS or data.get("schema_version") != STORE_SCHEMA:
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        # Every persisted section is validated before the store reports healthy: a corrupt or tampered
        # receipt chain, candidate or progress row fails the whole load closed, never partial-recovers.
        if (not isinstance(data["receipts"], list) or not isinstance(data["candidates"], dict)
                or not isinstance(data["progress"], dict)):
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        try:
            receipts = [OperationReceipt.model_validate(item) for item in data["receipts"]]
        except (TypeError, ValueError) as exc:
            raise LoopStoreError("LOOP_STORE_CORRUPT") from exc
        if not verify_chain(receipts):
            raise LoopStoreError("LOOP_STORE_CHAIN_BROKEN")
        _validate_candidates(data["candidates"])
        _validate_progress(data["progress"])
        return data

    def receipts(self) -> tuple[OperationReceipt, ...]:
        with store_lock(self.path):
            data = self._load()
        return tuple(OperationReceipt.model_validate(item) for item in data["receipts"])

    def candidates(self) -> dict[str, CapabilityCandidate]:
        with store_lock(self.path):
            data = self._load()
        return {key: CapabilityCandidate.model_validate(value) for key, value in data["candidates"].items()}

    def candidate(self, candidate_id: str) -> CapabilityCandidate | None:
        return self.candidates().get(candidate_id)

    def progress(self) -> dict[str, dict[str, Any]]:
        with store_lock(self.path):
            data = self._load()
        return {key: dict(value) for key, value in data["progress"].items()}

    def append(self, fields: dict[str, Any], *, candidate: CapabilityCandidate | None = None,
               progress: tuple[str, dict[str, Any]] | None = None,
               precondition: Callable[[dict[str, Any]], bool] | None = None) -> OperationReceipt:
        """Append one receipt (chained to the last) and, atomically with it, the candidate / progress row.

        ``precondition`` is evaluated under the write lock against the freshly loaded store, before anything
        is written; if it returns False the append is refused with ``LOOP_STORE_PRECONDITION_FAILED``. This
        lets a caller assert a still-current invariant (for example that a session binding is still the
        latest owner) atomically with the write, closing a read-then-write race.
        """
        with store_lock(self.path):
            data = self._load()
            if precondition is not None and not precondition(data):
                raise LoopStoreError("LOOP_STORE_PRECONDITION_FAILED")
            receipts = data["receipts"]
            if len(receipts) >= self.max_receipts:
                raise LoopStoreError("LOOP_STORE_FULL")
            previous = OperationReceipt.model_validate(receipts[-1]).receipt_hash if receipts else GENESIS
            receipt = OperationReceipt(sequence=len(receipts) + 1, previous_receipt_hash=previous,
                                       recorded_at=self.clock(), **fields)
            row = receipt.model_dump(mode="json")
            assert_minimized(row)
            receipts.append(row)
            if candidate is not None:
                if candidate.candidate_id not in data["candidates"] and len(data["candidates"]) >= self.max_candidates:
                    raise LoopStoreError("LOOP_STORE_FULL")
                candidate_row = candidate.model_dump(mode="json")
                assert_minimized(candidate_row)
                _validate_candidates({candidate.candidate_id: candidate_row})  # must remain loadable
                data["candidates"][candidate.candidate_id] = candidate_row
            if progress is not None:
                key, value = progress
                _validate_progress({key: value})  # the load validator itself: never write an unloadable row
                data["progress"][key] = value
            atomic_write_json(self.path, data)
        return receipt
