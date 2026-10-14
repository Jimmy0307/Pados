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
from pados.capability_loop.receipts import GENESIS, OperationReceipt, verify_chain
from pados.connectivity.storage import atomic_write_json, read_json, store_lock
from pados.lifecycle.minimization import assert_minimized

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
        try:
            receipts = [OperationReceipt.model_validate(item) for item in data["receipts"]]
        except (TypeError, ValueError) as exc:
            raise LoopStoreError("LOOP_STORE_CORRUPT") from exc
        if not verify_chain(receipts):
            raise LoopStoreError("LOOP_STORE_CHAIN_BROKEN")
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
               progress: tuple[str, dict[str, Any]] | None = None) -> OperationReceipt:
        """Append one receipt (chained to the last) and, atomically with it, the candidate / progress row."""
        with store_lock(self.path):
            data = self._load()
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
