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
from pados.capability_loop.learning_contracts import is_valid_session_ref
from pados.capability_loop.receipts import GENESIS, OperationReceipt, verify_chain
from pados.connectivity.storage import atomic_write_json, read_json, store_lock
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


_SCALAR = (str, int, float, bool, type(None))


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


def _validate_progress(progress: dict[Any, Any]) -> None:
    """Every progress row is a flat, bounded, scalar-valued dict keyed by a well-formed session ref."""
    for key, row in progress.items():
        if not isinstance(key, str) or not is_valid_session_ref(key) or not isinstance(row, dict):
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        for field_name, field_value in row.items():
            if not isinstance(field_name, str) or not isinstance(field_value, _SCALAR):
                raise LoopStoreError("LOOP_STORE_CORRUPT")
        percent = row.get("progress_percent")
        if percent is not None and (isinstance(percent, bool) or not isinstance(percent, int)
                                    or not 0 <= percent <= 100):
            raise LoopStoreError("LOOP_STORE_CORRUPT")
        try:
            assert_minimized(row)
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
                data["candidates"][candidate.candidate_id] = candidate_row
            if progress is not None:
                key, value = progress
                assert_minimized(value)
                data["progress"][key] = value
            atomic_write_json(self.path, data)
        return receipt
