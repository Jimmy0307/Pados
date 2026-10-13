# SPDX-License-Identifier: AGPL-3.0-only
"""Shared source/evidence semantics for graph and Corpus projections.

The graph and Corpus remain separate storage surfaces.  They must not invent
different meanings for a source or an evidence span, however.  This module is
intentionally storage-neutral and returns JSON-safe dictionaries so existing
metadata columns and provider context contracts can adopt one vocabulary
without a migration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

REFERENCE_ONLY = "REFERENCE_ONLY"
SELECTED = "SELECTED"


def _metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = value.get("metadata_json")
    return metadata if isinstance(metadata, Mapping) else value


def _first_text(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def source_record(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical provider-facing identity for a source object."""

    metadata = _metadata(source)
    source_id = str(source.get("id") or metadata.get("source_id") or "")
    content_hash = _first_text(
        metadata,
        "content_version",
        "extracted_text_hash",
        "content_hash",
        "sha256",
    )
    retrieval_status = _first_text(metadata, "retrieval_status", "status") or "UNKNOWN"
    normalization_status = _first_text(metadata, "normalization_status", "parse_status")
    if normalization_status is None:
        normalization_status = "NORMALIZED" if content_hash and source.get("content") else "UNKNOWN"
    origin = _first_text(
        metadata,
        "canonical_url",
        "final_url",
        "original_url",
        "source_path",
        "file_path",
        "source_uri",
    )
    return {
        "source_id": source_id,
        "source_type": _first_text(metadata, "source_type") or str(source.get("semantic_type") or "RESOURCE"),
        "title": str(source.get("title") or metadata.get("title") or "Untitled source"),
        "origin": origin,
        "retrieval_time": _first_text(metadata, "retrieval_time", "fetched_at", "retrieved_at"),
        "content_hash": content_hash,
        "content_version": content_hash,
        "retrieval_status": retrieval_status,
        "normalization_status": normalization_status,
        "evidence_eligibility": _first_text(metadata, "evidence_eligibility") or "UNKNOWN",
        "selection_status": REFERENCE_ONLY,
    }


def evidence_record(
    *,
    source: Mapping[str, Any],
    evidence_id: str,
    excerpt: str,
    location: Mapping[str, Any] | None,
    relevance_score: int,
) -> dict[str, Any]:
    """Return one bounded selected evidence span linked to a source version."""

    source_identity = source_record(source)
    return {
        "kind": "EVIDENCE",
        "context_role": "SELECTED_EVIDENCE",
        "evidence_id": evidence_id,
        "source_id": source_identity["source_id"],
        "source_node_id": source_identity["source_id"],
        "source_version": source_identity["content_version"],
        "excerpt": excerpt,
        "location": dict(location or {}),
        "selection_status": SELECTED,
        "relevance_score": relevance_score,
        "provenance": {
            "source_id": source_identity["source_id"],
            "source_node_id": source_identity["source_id"],
            "content_hash": source_identity["content_hash"],
            "content_version": source_identity["content_version"],
            "origin": source_identity["origin"],
        },
    }


def validate_evidence(
    references: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep only evidence that maps to a known source/version/span."""

    sources = {str(item.get("source_id") or item.get("source_node_id") or ""): item for item in references}
    valid: list[dict[str, Any]] = []
    rejected = 0
    for item in evidence:
        source_id = str(item.get("source_id") or item.get("source_node_id") or "")
        excerpt = str(item.get("excerpt") or "").strip()
        source = sources.get(source_id)
        provenance = item.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        source_version = str(item.get("source_version") or provenance.get("content_version") or "")
        known_version = str((source or {}).get("content_version") or "")
        if (
            not source
            or not excerpt
            or str(item.get("selection_status") or "") != SELECTED
            or (known_version and source_version and known_version != source_version)
        ):
            rejected += 1
            continue
        valid.append(dict(item))
    return valid, rejected


def evidence_state(references: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]]) -> str:
    """Return a human-safe state that cannot claim evidence when none exists."""

    if evidence:
        return "EVIDENCE_SELECTED"
    if references:
        return "REFERENCE_ONLY"
    return "NO_SOURCE"
