# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical hash-chain records for semantic writes in the P6.19B authority."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = (
    "scope_id",
    "actor",
    "action",
    "target",
    "reason",
    "timestamp",
    "request_id",
    "idempotency_key",
    "outcome",
    "before_state",
    "after_state",
    "approval_ref",
    "rollback_ref",
    "previous_hash",
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def build_audit_record(
    *,
    scope_id: str,
    actor: str,
    action: str,
    target: str,
    reason: str,
    timestamp: str,
    request_id: str,
    idempotency_key: str,
    outcome: str,
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
    approval_ref: str | None,
    rollback_ref: str | None,
    previous_hash: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scope_id": scope_id,
        "actor": actor,
        "action": action,
        "target": target,
        "reason": reason,
        "timestamp": timestamp,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "outcome": outcome,
        "before_state": dict(before_state),
        "after_state": dict(after_state),
        "approval_ref": approval_ref,
        "rollback_ref": rollback_ref,
        "previous_hash": previous_hash,
    }
    if any(not record[field] for field in _FIELDS[:9]):
        raise ValueError("AUDIT_REQUIRED_FIELD_MISSING")
    if previous_hash is not None and not _HASH.fullmatch(previous_hash):
        raise ValueError("AUDIT_PREVIOUS_HASH_INVALID")
    record["record_hash"] = hashlib.sha256(canonical_json(record)).hexdigest()
    return record


def verify_audit_chain(records: Sequence[Mapping[str, Any]]) -> bool:
    """Verify record hashes, order links, and per-scope idempotency uniqueness."""
    previous_by_scope: dict[str, str | None] = {}
    idempotency: set[tuple[str, str]] = set()
    for record in records:
        if any(field not in record for field in (*_FIELDS, "record_hash")):
            return False
        scope_id = record["scope_id"]
        key = (scope_id, record["idempotency_key"])
        if not isinstance(scope_id, str) or not isinstance(key[1], str) or key in idempotency:
            return False
        expected_previous = previous_by_scope.get(scope_id)
        if record["previous_hash"] != expected_previous:
            return False
        if record["previous_hash"] is not None and not _HASH.fullmatch(str(record["previous_hash"])):
            return False
        payload = {field: record[field] for field in _FIELDS}
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        if record["record_hash"] != digest:
            return False
        idempotency.add(key)
        previous_by_scope[scope_id] = digest
    return True
