# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal, read-only Mission Control visibility for the Enterprise Pack.

Evidence provenance is read from the hash-verified run-context objects that
the product loop already persists in the proof lane; nothing here writes
state or creates a second store. Only content-free summaries are projected.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from pados.enterprise.status import enterprise_status

MAX_CONTEXT_OBJECT_BYTES = 2_000_000
# Known PADOS provider identities (ExecutionReceipt.provider_id) and where they execute.
EXECUTION_CLASS = {
    "PADOS_FILE_DIRECT_MAKER": "LOCAL",
    "PADOS_CODEX_CLI": "REMOTE",
    "PADOS_CLAUDE_CODE": "REMOTE",
    "PADOS_LOCAL_INFERENCE": "LOCAL",  # Local Compute Node V1 (private-network node)
}

RECENT_RUNS_SQL = """
SELECT i.run_id::text AS run_id, i.scope_id AS project_id, i.created_at,
       o.storage_uri, o.content_hash,
       (SELECT r.provider_id FROM state_proof_results r
         WHERE r.run_id = i.run_id ORDER BY r.created_at DESC LIMIT 1) AS provider_id,
       (SELECT r.status FROM state_proof_results r
         WHERE r.run_id = i.run_id ORDER BY r.created_at DESC LIMIT 1) AS result_status
FROM p6_21h_product_read_index i JOIN state_proof_objects o ON o.id = i.context_object_id
ORDER BY i.created_at DESC LIMIT 25
"""


def execution_class(provider_id: str | None) -> str:
    return EXECUTION_CLASS.get(str(provider_id or ""), "UNKNOWN")


def enterprise_visibility(
    *, config_path: str, core_ready: bool, object_root: str, known_projects: Iterable[str],
    rows: Callable[[str], list[dict[str, Any]]],
) -> dict[str, Any]:
    status = enterprise_status(config_path, core_ready=core_ready)
    known = set(known_projects)
    evidence: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    try:
        recent = rows(RECENT_RUNS_SQL)
    except Exception as exc:  # noqa: BLE001 - visibility degrades, never breaks Mission Control
        recent = []
        failures.append({"kind": "PROOF_LANE_UNAVAILABLE", "reason": type(exc).__name__})
    for row in recent:
        if str(row.get("project_id")) not in known:
            continue
        summary = _extension_summary(row, object_root)
        if summary is None:
            continue
        entry = {
            "run_id": row["run_id"], "project_id": row["project_id"], "created_at": row.get("created_at"),
            "provider_id": row.get("provider_id") or "UNVERIFIED_PROVIDER",
            "execution_class": execution_class(row.get("provider_id")),
            "result_status": row.get("result_status"), **summary,
        }
        evidence.append(entry)
        withheld = summary["authorization"].get("withheld_count", 0)
        if withheld:
            failures.append({"kind": "EVIDENCE_WITHHELD", "run_id": row["run_id"],
                             "project_id": row["project_id"], "count": withheld,
                             "reasons": summary["authorization"].get("withheld", {})})
    for capability in status["capabilities"]:
        if capability["health"] == "UNAUTHORIZED":
            failures.append({"kind": "CAPABILITY_UNAUTHORIZED", "capability_id": capability["capability_id"],
                             "reason": capability.get("reason")})
    return {
        "status": status["status"], "core": status["core"], "capabilities": status["capabilities"],
        "recent_evidence": evidence[:10], "authorization_failures": failures[:20],
        "projection_only": True, "semantic_authority": "NONE",
    }


def _extension_summary(row: dict[str, Any], object_root: str) -> dict[str, Any] | None:
    if not object_root:
        return None
    root = Path(object_root).resolve()
    target = Path(str(row.get("storage_uri") or "")).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    if target.stat().st_size > MAX_CONTEXT_OBJECT_BYTES:
        return None
    payload = target.read_bytes()
    if hashlib.sha256(payload).hexdigest() != str(row.get("content_hash") or "").removeprefix("sha256:"):
        return None
    try:
        context = json.loads(payload)
        extension = context["bounded_context"]["extensions"]["enterprise_evidence"]
        summary = extension["summary"]
    except (KeyError, TypeError, ValueError):
        return None
    return dict(summary) if isinstance(summary, dict) else None
