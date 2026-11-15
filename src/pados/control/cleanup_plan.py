# SPDX-License-Identifier: AGPL-3.0-only
"""Cleanup plan PREVIEW for the Resources page: what a cleanup could reclaim, never a cleanup. READ ONLY.

Built from the existing storage accounting (``pados.lifecycle.accounting``: per-location bytes and the retention-window
``eligible_for_purge`` estimate) and the retention catalog (``pados.lifecycle.retention``). It deletes nothing and
authorizes nothing (``deletes=false``, ``authority="NONE"``).

A location is a candidate only when it is a cleanup class (rebuildable cache, temporary sandbox data, operational logs)
AND every data kind it holds is R0/R1 with the ``DELETE_RECORD`` rule. Never candidates, whatever their age or size:

- evidence (job artifacts, receipts, release and project evidence), decisions and audit records (R3 authority
  metadata, ledgers, tombstones);
- unique copies (content-addressed objects, project sources, uploaded or staged records).

There is no governed, reversible cleanup path today (``pados.lifecycle.purge`` is an operator library with exact plan
hashes and tombstones, not a reversible action), so the plan never offers an execute action: ``execute.available`` is
false and the guidance is ``OWNER_AUTHORIZATION_REQUIRED``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pados.lifecycle.accounting import LocationStatus, LocationUsage, PurgeUnit, StorageLocation
from pados.lifecycle.retention import PurgeRule, RetentionClass, StorageCategory, policy_for

SCHEMA = "pados.control.cleanup-plan-preview.v1"

CLEANUP_CATEGORIES = frozenset({StorageCategory.CACHE, StorageCategory.SANDBOX_DATA,
                                StorageCategory.OPERATIONAL_LOGS})
PROTECTED_CATEGORIES: Mapping[StorageCategory, str] = {
    StorageCategory.AUTHORITY_METADATA: "PROTECTED_DECISIONS_AND_AUDIT",
    StorageCategory.ARTIFACTS: "PROTECTED_EVIDENCE_AND_UNIQUE_COPIES",
    StorageCategory.RELEASE_TEST_EVIDENCE: "PROTECTED_EVIDENCE",
    StorageCategory.PROJECT_SOURCES: "PROTECTED_UNIQUE_PROJECT_COPIES",
    StorageCategory.REMOTE_UPLOADS: "PROTECTED_UNIQUE_UPLOADED_COPIES",
}
_PROTECTED_NAME = re.compile(r"EVIDENCE|RECEIPT|DECISION|AUDIT|LEDGER|TOMBSTONE|ACCEPTANCE|INVENTORY|REGISTRY|SOURCE")
_DELETABLE = frozenset({RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL})


def protected_reason(location: StorageLocation) -> str | None:
    """Why ``location`` may never be a cleanup candidate, or None when it is a cleanup class."""
    if location.category in PROTECTED_CATEGORIES:
        return PROTECTED_CATEGORIES[location.category]
    if location.content_addressed:
        return "PROTECTED_CONTENT_ADDRESSED_UNIQUE_COPY"
    if location.purge_unit is PurgeUnit.STAGED_RECORD:
        return "PROTECTED_STAGED_RECORDS_NEED_PLAN_PURGE"
    if not location.kinds:
        return "PROTECTED_NO_DATA_KIND"
    for kind in location.kinds:
        policy = policy_for(kind)
        if (policy.authoritative or policy.retention not in _DELETABLE or policy.purge_rule is not PurgeRule.DELETE_RECORD
                or _PROTECTED_NAME.search(kind.value)):
            return f"PROTECTED_KIND_{kind.value}"
    if location.category not in CLEANUP_CATEGORIES:
        return "NOT_A_CLEANUP_CLASS"
    return None


def plan_cleanup(locations: Iterable[StorageLocation], usages: Iterable[LocationUsage]) -> dict[str, Any]:
    """The preview: candidates (bytes past their retention window), cleanup classes with nothing to reclaim, and every
    protected location with its reason. Sizes are the accounting's estimate (``estimate_only``)."""
    by_id = {usage.location_id: usage for usage in usages}
    candidates: list[dict[str, Any]] = []
    nothing_due: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    for location in locations:
        usage = by_id.get(location.location_id)
        base = {"location_id": location.location_id, "category": location.category.value,
                "data_kinds": [kind.value for kind in location.kinds],
                "bytes": usage.bytes if usage else 0, "file_count": usage.file_count if usage else 0}
        reason = protected_reason(location)
        if reason is not None:
            protected.append({**base, "reason": reason})
            continue
        if usage is None or usage.status is not LocationStatus.MEASURED:
            nothing_due.append({**base, "reason": f"NOT_MEASURED_{usage.status.value if usage else 'MISSING'}"})
            continue
        eligible = usage.eligible_for_purge
        if eligible.bytes <= 0 or not eligible.available:
            nothing_due.append({**base, "reason": "NOTHING_PAST_RETENTION_WINDOW"})
            continue
        candidates.append({**base, "reclaimable_bytes": eligible.bytes, "reclaimable_items": eligible.items,
                           "basis": list(eligible.basis),
                           "retention_classes": [c.value for c in usage.retention_classes],
                           "oldest_mtime": usage.oldest_mtime, "estimate_only": True})
    return {
        "schema": SCHEMA,
        "plan_only": True,
        "deletes": False,
        "authority": "NONE",
        "estimated_reclaimable_bytes": sum(c["reclaimable_bytes"] for c in candidates),
        "candidates": candidates,
        "nothing_due": nothing_due,
        "protected": protected,
        "never_candidates": ["EVIDENCE", "DECISIONS", "AUDIT_RECORDS", "UNIQUE_COPIES", "DATABASE_ROWS"],
        "execute": {"available": False, "reason": "NO_GOVERNED_REVERSIBLE_CLEANUP_PATH",
                    "guidance": "OWNER_AUTHORIZATION_REQUIRED"},
    }
