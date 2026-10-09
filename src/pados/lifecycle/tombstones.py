# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only, hash-chained, two-phase tombstones for rejected-artifact payloads.

Purging the bytes of a REJECTED artifact keeps its authority metadata: artifact
hash, status, decision timestamp, receipt reference, decision evidence
reference, when and why the payload was purged and the payload state. The
ledger follows the ``pados.remote.ledger.IntentLedger`` chain pattern
(prev_hash -> entry_hash) with three phases per purge:

* ``PENDING``   written before the payload is reserved (payload_state PRESENT); it
  binds the reserved inode (``reserved_dev``, ``reserved_ino``, ``reserved_size``,
  ``reserved_sha256``) so recovery acts only on that exact inode;
* ``COMPLETED`` written after unlink and directory fsync
  (payload_state ``METADATA_RETAINED_PAYLOAD_PURGED``);
* ``ABORTED``   the reservation was rolled back (payload_state PRESENT).

A PENDING without a later COMPLETED/ABORTED is reported for explicit recovery
and never acted on blindly. Records of the earlier v2 schema (no reserved
identity; never written by a released PADOS version) are accepted as read-only
history: the chain is still verified, a legacy PENDING is reported
``LEGACY_PENDING_REQUIRES_OPERATOR`` by recovery and never auto-recovered, and
every new write is v3. Every read and every append verifies the whole
chain first and fails closed (``TOMBSTONE_CHAIN_INVALID``); verify + append +
fsync run under a process-safe lock (``fcntl.flock`` / ``msvcrt.locking``) on a
sibling lock file. Each record passes ``assert_minimized`` with an explicit
schema. It is R3 authority metadata: nothing in Lifecycle deletes it.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.execution.context import canonical_hash
from pados.lifecycle.fswalk import fsync_directory
from pados.lifecycle.locking import ensure_durable_dir, exclusive_lock
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.lifecycle.retention import DataKind, PayloadState

GENESIS = "0" * 64
TOMBSTONE_SCHEMA = "pados.lifecycle.payload-tombstone.v3"
# v2 (no reserved identity) is accepted as read-only history; it is never written.
LEGACY_TOMBSTONE_SCHEMA_V2 = "pados.lifecycle.payload-tombstone.v2"
TOMBSTONE_KEYS_V2 = frozenset({
    "schema", "phase", "artifact_hash", "data_kind", "status", "timestamp", "receipt_ref",
    "decision_evidence_ref", "recorded_at", "purged_at", "reason", "payload_state", "plan_hash",
})
TOMBSTONE_KEYS = TOMBSTONE_KEYS_V2 | frozenset({"reserved_dev", "reserved_ino", "reserved_size", "reserved_sha256"})
_KEYS_BY_SCHEMA = {TOMBSTONE_SCHEMA: TOMBSTONE_KEYS, LEGACY_TOMBSTONE_SCHEMA_V2: TOMBSTONE_KEYS_V2}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,199}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


class Phase(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


_PAYLOAD_STATE = {
    Phase.PENDING: PayloadState.PRESENT,
    Phase.COMPLETED: PayloadState.METADATA_RETAINED_PAYLOAD_PURGED,
    Phase.ABORTED: PayloadState.PRESENT,
}


class TombstoneError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def tombstone_record(*, phase: Phase, artifact_hash: str, status: str, timestamp: str, receipt_ref: str,
                     decision_evidence_ref: str, recorded_at: str, reason: str, plan_hash: str,
                     reserved: tuple[int, int, int]) -> dict[str, Any]:
    """``reserved`` is the (st_dev, st_ino, size) of the verified payload inode being reserved."""
    record: dict[str, Any] = {
        "schema": TOMBSTONE_SCHEMA,
        "phase": phase.value,
        "artifact_hash": artifact_hash,
        "data_kind": DataKind.REJECTED_ARTIFACT_PAYLOAD.value,
        "status": status,
        "timestamp": timestamp,
        "receipt_ref": receipt_ref,
        "decision_evidence_ref": decision_evidence_ref,
        "recorded_at": recorded_at,
        "purged_at": recorded_at if phase is Phase.COMPLETED else None,
        "reason": reason,
        "payload_state": _PAYLOAD_STATE[phase].value,
        "plan_hash": plan_hash,
        "reserved_dev": reserved[0],
        "reserved_ino": reserved[1],
        "reserved_size": reserved[2],
        "reserved_sha256": artifact_hash,
    }
    validate_tombstone(record)
    return record


def _is_aware_iso(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def validate_tombstone(record: Mapping[str, Any]) -> None:
    """Validate a v3 record, or a well-formed legacy v2 record (read-only history)."""
    keys = _KEYS_BY_SCHEMA.get(str(record.get("schema"))) if isinstance(record, Mapping) else None
    if keys is None or set(record) != keys:
        raise TombstoneError("TOMBSTONE_SCHEMA_MISMATCH")
    legacy = record["schema"] == LEGACY_TOMBSTONE_SCHEMA_V2
    try:
        phase = Phase(record["phase"])
    except ValueError:
        raise TombstoneError("TOMBSTONE_PHASE_INVALID") from None
    for key in ("artifact_hash", "plan_hash"):
        if not isinstance(record[key], str) or not _SHA256.fullmatch(record[key]):
            raise TombstoneError("TOMBSTONE_HASH_INVALID")
    for key in ("receipt_ref", "decision_evidence_ref"):
        if not isinstance(record[key], str) or not _REF.fullmatch(record[key]):
            raise TombstoneError("TOMBSTONE_REFERENCE_INVALID")
    for key in ("status", "reason"):
        if not isinstance(record[key], str) or not _CODE.fullmatch(record[key]):
            raise TombstoneError("TOMBSTONE_CODE_INVALID")
    if not _is_aware_iso(record["timestamp"]) or not _is_aware_iso(record["recorded_at"]):
        raise TombstoneError("TOMBSTONE_TIMESTAMP_INVALID")
    purged = record["purged_at"]
    if (phase is Phase.COMPLETED) != (purged is not None) or (purged is not None and purged != record["recorded_at"]):
        raise TombstoneError("TOMBSTONE_TIMESTAMP_INVALID")
    if record["payload_state"] != _PAYLOAD_STATE[phase].value:
        raise TombstoneError("TOMBSTONE_PAYLOAD_STATE_INVALID")
    if not legacy:
        for key in ("reserved_dev", "reserved_ino", "reserved_size"):
            value = record[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TombstoneError("TOMBSTONE_RESERVED_IDENTITY_INVALID")
        if record["reserved_sha256"] != record["artifact_hash"]:
            raise TombstoneError("TOMBSTONE_RESERVED_IDENTITY_INVALID")
    if record["data_kind"] != DataKind.REJECTED_ARTIFACT_PAYLOAD.value:
        raise TombstoneError("TOMBSTONE_DATA_KIND_INVALID")
    assert_minimized(record, allowed_keys=keys, max_string=256, max_items=24, max_total_bytes=4096)


class TombstoneLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        ensure_durable_dir(self.path.parent)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Process-safe exclusive lock over the ledger (verify, append and fsync)."""
        with exclusive_lock(self.lock_path):
            yield

    def _exists(self) -> bool:
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise TombstoneError("TOMBSTONE_LEDGER_NOT_REGULAR")
        return True

    def _verified(self) -> list[dict[str, Any]]:
        """All entries after full chain + record validation; raises TOMBSTONE_CHAIN_INVALID."""
        if not self._exists():
            return []
        entries: list[dict[str, Any]] = []
        prev = GENESIS
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if not isinstance(entry, dict) or set(entry) != {"prev_hash", "record", "entry_hash"}:
                        raise TombstoneError("TOMBSTONE_CHAIN_INVALID")
                    body = {"prev_hash": entry["prev_hash"], "record": entry["record"]}
                    if entry["prev_hash"] != prev or canonical_hash(body) != entry["entry_hash"]:
                        raise TombstoneError("TOMBSTONE_CHAIN_INVALID")
                    validate_tombstone(entry["record"])
                    prev = str(entry["entry_hash"])
                    entries.append(entry)
        except (ValueError, KeyError, TypeError, DataMinimizationViolation) as exc:
            if isinstance(exc, TombstoneError) and exc.code == "TOMBSTONE_CHAIN_INVALID":
                raise
            raise TombstoneError("TOMBSTONE_CHAIN_INVALID") from None
        return entries

    def entries(self) -> list[dict[str, Any]]:
        with self.locked():
            return self._verified()

    def verify(self) -> bool:
        try:
            self.entries()
        except TombstoneError:
            return False
        return True

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        validate_tombstone(record)
        if record["schema"] != TOMBSTONE_SCHEMA:
            raise TombstoneError("TOMBSTONE_LEGACY_SCHEMA_READ_ONLY")
        with self.locked():
            existing = self._verified()
            created = not self._exists()
            prev = existing[-1]["entry_hash"] if existing else GENESIS
            entry: dict[str, Any] = {"prev_hash": prev, "record": dict(record)}
            entry["entry_hash"] = canonical_hash(entry)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            if created:
                fsync_directory(self.path.parent)
            return entry

    def latest(self, artifact_hash: str) -> dict[str, Any] | None:
        """Most recent record for ``artifact_hash`` from a verified chain."""
        found: dict[str, Any] | None = None
        for entry in self.entries():
            if entry["record"]["artifact_hash"] == artifact_hash:
                found = {**entry["record"], "entry_hash": entry["entry_hash"]}
        return found

    find = latest

    def pending(self) -> list[dict[str, Any]]:
        """Records whose latest phase is PENDING (a purge interrupted before completion)."""
        latest: dict[str, dict[str, Any]] = {}
        for entry in self.entries():
            latest[entry["record"]["artifact_hash"]] = {**entry["record"], "entry_hash": entry["entry_hash"]}
        return [r for _, r in sorted(latest.items()) if r["phase"] == Phase.PENDING.value]
