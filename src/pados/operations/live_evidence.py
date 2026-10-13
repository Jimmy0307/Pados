# SPDX-License-Identifier: AGPL-3.0-only
"""Live provider readiness derived from persisted PADOS execution evidence.

No new truth store: the signal is recomputed from existing state-proof rows.
A provider counts as live-verified only when a recent ResultReceipt was
accepted by semantic CAS and its persisted input manifest carries an
ExecutionReceipt that re-verifies deterministically as LIVE_PROVIDER evidence
from the same provider, bound to the run's contract and artifacts. A process
probe, a registered worker, or a failed/simulated run never qualifies.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pados.execution.receipts import verify_execution_receipt

LIVE_EVIDENCE_MAX_AGE = timedelta(days=7)
CANDIDATE_LIMIT = 5


def evaluate_live_evidence(
    candidates: Sequence[Mapping[str, Any]],
    *,
    provider_id: str,
    now: datetime,
    read_manifest: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    max_age: timedelta = LIVE_EVIDENCE_MAX_AGE,
) -> dict[str, Any]:
    """Return the live-UAT state for ``provider_id`` from newest-first candidates."""
    if not candidates:
        return {"status": "LIVE_UAT_PENDING", "reason": "NO_ACCEPTED_PROVIDER_RESULT"}
    for row in candidates:
        if row.get("provider_id") != provider_id or row.get("result_status") != "SUCCEEDED" \
                or row.get("commit_status") != "COMMITTED":
            continue
        manifest = read_manifest(row)
        if not manifest:
            continue
        runtime = (manifest.get("specification") or {}).get("runtime_result") or {}
        contract = (manifest.get("specification") or {}).get("work_contract") or {}
        receipt = runtime.get("execution_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("evidence_kind") != "LIVE_PROVIDER" \
                or receipt.get("provider_id") != provider_id or runtime.get("status") != "COMPLETED":
            continue
        if not verify_execution_receipt(receipt, run_id=str(runtime.get("run_id")), attempt=int(runtime.get("attempt", 0)),
                                        contract=contract, artifacts=runtime.get("artifact_refs") or []):
            continue
        accepted_at = row["accepted_at"]
        evidence = {
            "provider_id": provider_id,
            "result_id": str(row["result_id"]),
            "run_id": str(runtime.get("run_id")),
            "execution_receipt": receipt.get("receipt_id"),
            "accepted_scope_version": row.get("accepted_scope_version"),
            "accepted_at": accepted_at.isoformat(),
        }
        if now - accepted_at > max_age:
            return {"status": "LIVE_UAT_STALE", "reason": "NEWEST_VERIFIED_EVIDENCE_TOO_OLD", "evidence": evidence}
        return {"status": "LIVE_UAT_VERIFIED", "reason": None, "evidence": evidence}
    return {"status": "LIVE_UAT_UNVERIFIED", "reason": "NO_CANDIDATE_RECEIPT_VERIFIED"}


def live_provider_evidence(session: Session, provider_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        rows = session.execute(text("""
            SELECT r.id::text AS result_id, r.provider_id, r.status AS result_status,
                   c.status AS commit_status, c.new_version AS accepted_scope_version,
                   c.created_at AS accepted_at, o.storage_uri, o.content_hash
            FROM state_proof_results r
            JOIN state_proof_commits c ON c.result_id = r.id
            JOIN state_proof_objects o ON o.id = r.input_manifest_object_id
            WHERE r.provider_id = :provider
            ORDER BY c.created_at DESC LIMIT :limit
        """), {"provider": provider_id, "limit": CANDIDATE_LIMIT}).mappings().all()
    except SQLAlchemyError as exc:
        return {"status": "LIVE_UAT_UNAVAILABLE", "reason": type(exc).__name__}
    return evaluate_live_evidence([dict(row) for row in rows], provider_id=provider_id,
                                  now=now or datetime.now(UTC), read_manifest=_read_manifest)


def _read_manifest(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        payload = Path(str(row["storage_uri"])).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(payload).hexdigest() != str(row["content_hash"]).removeprefix("sha256:"):
        return None
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value if isinstance(value, Mapping) else None
