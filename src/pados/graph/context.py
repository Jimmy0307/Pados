# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol

from pados.graph.cognitive_pipeline import EvidenceResolver


class DirtyEvent(StrEnum):
    COSMETIC_CHANGE = "COSMETIC_CHANGE"
    STRUCTURAL_CHANGE = "STRUCTURAL_CHANGE"
    SEMANTIC_CHANGE = "SEMANTIC_CHANGE"
    SOURCE_CHANGE = "SOURCE_CHANGE"


class ContextProvider(Protocol):
    def retrieve(
        self,
        *,
        workspace_id: str,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        """Return bounded, provenance-bearing context references."""


class EmptyContextProvider:
    def retrieve(
        self,
        *,
        workspace_id: str,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        return []


_MAX_NODES = 40
_MAX_EDGES = 80
_MAX_REFERENCES = 8
DEFAULT_CONTEXT_TOKEN_BUDGET = 2_400
CONTEXT_SEGMENT_BUDGETS = {
    "question": 240,
    "evidence": 1_000,
    "concepts": 520,
    "prior_synthesis": 320,
    "memory": 160,
    "references": 160,
}
_EXCLUDED_METADATA = {
    "artifact",
    "auto_refresh_policy",
    "context_fingerprint",
    "human_confirmed_at",
    "previous_synthesis_node_id",
    "prior_synthesis_version",
    "source_snapshot",
    "stale_reason",
    "synthesis_id",
    "synthesis_status",
    "synthesis_version",
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _metadata(node: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = node.get("metadata_json")
    return metadata if isinstance(metadata, Mapping) else {}


def _semantic_metadata(node: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _metadata(node)
    return {
        str(key): _json_safe(value)
        for key, value in metadata.items()
        if str(key) not in _EXCLUDED_METADATA
    }


def node_semantic_projection(node: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content-bearing node fields used for synthesis identity.

    Position, selection state, panel state, and lifecycle projections are deliberately
    absent.  A pointer drag therefore cannot invalidate a living synthesis.
    """

    content = str(node.get("content") or "")
    return {
        "id": str(node.get("id") or ""),
        "semantic_type": str(node.get("semantic_type") or ""),
        "title": str(node.get("title") or ""),
        "content_hash": _digest(content),
        "provenance": str(node.get("provenance") or ""),
        "assurance_status": str(node.get("assurance_status") or ""),
        "idea_state": str(node.get("idea_state") or "") if node.get("idea_state") is not None else None,
        "source_capability_id": str(node.get("source_capability_id") or "") if node.get("source_capability_id") is not None else None,
        "source_version": str(node.get("source_version") or "") if node.get("source_version") is not None else None,
        "lineage": _json_safe(node.get("lineage_json") or {}),
        "metadata": _semantic_metadata(node),
    }


def _node_semantic_hash(node: Mapping[str, Any]) -> str:
    return _digest(node_semantic_projection(node))


def edge_semantic_projection(edge: Mapping[str, Any]) -> dict[str, Any]:
    metadata = edge.get("metadata_json")
    return {
        "id": str(edge.get("id") or ""),
        "source_node_id": str(edge.get("source_node_id") or ""),
        "target_node_id": str(edge.get("target_node_id") or ""),
        "relationship_type": str(edge.get("relationship_type") or ""),
        "label": str(edge.get("label") or ""),
        "provenance": str(edge.get("provenance") or ""),
        "metadata": _json_safe(metadata if isinstance(metadata, Mapping) else {}),
    }


def _edge_semantic_hash(edge: Mapping[str, Any]) -> str:
    return _digest(edge_semantic_projection(edge))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[0-9A-Za-z\u0080-\uffff]{2,}", value.casefold())
        if len(token) >= 2
    }


def _estimate_tokens(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


def _fit_text(value: str, budget: int) -> tuple[str, bool]:
    if _estimate_tokens(value) <= budget:
        return value, False
    max_chars = max(4, budget * 4)
    return value[:max_chars].rstrip() + "…", True


def _fit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    budget: int,
    text_key: str,
) -> tuple[list[dict[str, Any]], int, bool]:
    selected: list[dict[str, Any]] = []
    remaining = budget
    truncated = False
    for record in records:
        if remaining <= 0:
            truncated = True
            break
        item = dict(_json_safe(record))
        text = str(item.get(text_key) or "")
        if not text:
            selected.append(item)
            continue
        bounded, was_truncated = _fit_text(text, remaining)
        item[text_key] = bounded
        used = _estimate_tokens(bounded)
        if used > remaining:
            truncated = True
            break
        selected.append(item)
        remaining -= used
        truncated = truncated or was_truncated
    return selected, remaining, truncated


def _node_text(node: Mapping[str, Any]) -> str:
    return f"{node.get('title', '')} {node.get('content', '')}".casefold()


def _is_living_synthesis(node: Mapping[str, Any]) -> bool:
    metadata = _metadata(node)
    return bool(metadata.get("living_synthesis")) or (
        str(node.get("semantic_type")) == "OUTPUT"
        and str(metadata.get("result_type")) == "LIVING_SYNTHESIS"
    )


def _context_role(node: Mapping[str, Any]) -> str:
    semantic_type = str(node.get("semantic_type") or "")
    metadata = _metadata(node)
    if semantic_type == "OUTPUT":
        return "PRIOR_SYNTHESIS" if _is_living_synthesis(node) else "HISTORICAL_OUTPUT"
    if semantic_type == "RESOURCE":
        return "REFERENCE_ONLY"
    if semantic_type == "CONCEPT":
        return "CONFIRMED_CONCEPT" if str(node.get("assurance_status") or "") in {"HUMAN_CONFIRMED", "VALIDATED", "PROMOTED"} else "PROPOSED_CONCEPT"
    if semantic_type == "IDEA":
        return "ROOT_QUESTION" if metadata.get("current_objective") or metadata.get("root_question") else "CURRENT_FOCUS"
    if semantic_type == "ACTION":
        return "USER_CONSTRAINT"
    if semantic_type == "EXPERT":
        return "EXPERT_REFERENCE"
    return "MEMORY"


def _deduplicate_nodes(nodes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen_ids: set[str] = set()
    seen_resource_hashes: set[str] = set()
    result: list[Mapping[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id and node_id in seen_ids:
            continue
        if str(node.get("semantic_type") or "") == "RESOURCE":
            metadata = _metadata(node)
            content_hash = str(metadata.get("extracted_text_hash") or metadata.get("content_hash") or "")
            if content_hash and content_hash in seen_resource_hashes:
                continue
            if content_hash:
                seen_resource_hashes.add(content_hash)
        if node_id:
            seen_ids.add(node_id)
        result.append(node)
    return result


def _references(provider: ContextProvider, workspace_id: str, query: str) -> list[dict[str, Any]]:
    return [
        dict(_json_safe(reference))
        for reference in provider.retrieve(
            workspace_id=workspace_id,
            query=query,
            limit=_MAX_REFERENCES,
        )
        if isinstance(reference, Mapping)
    ][:_MAX_REFERENCES]


class CognitiveContextAssembler:
    """Assemble bounded graph context with a stable, auditable identity."""

    def __init__(
        self,
        *,
        memory_provider: ContextProvider | None = None,
        corpus_provider: ContextProvider | None = None,
        evidence_resolver: EvidenceResolver | None = None,
    ) -> None:
        self.memory_provider = memory_provider or EmptyContextProvider()
        self.corpus_provider = corpus_provider or EmptyContextProvider()
        self.evidence_resolver = evidence_resolver or EvidenceResolver()

    def assemble(
        self,
        *,
        workspace_id: str,
        root_node_ids: Sequence[str],
        intent: str,
        action: str,
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        previous_synthesis: Mapping[str, Any] | None = None,
        prior_synthesis_version: int | None = None,
    ) -> dict[str, Any]:
        node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
        roots = [
            node_by_id[node_id]
            for node_id in root_node_ids
            if node_id in node_by_id and str(node_by_id[node_id].get("semantic_type") or "") != "OUTPUT"
        ]
        root_ids = [str(node.get("id")) for node in roots]
        root_text = " ".join(_node_text(node) for node in roots)
        root_terms = _tokens(root_text)
        direct_ids = set(root_ids)
        for edge in edges:
            source = str(edge.get("source_node_id"))
            target = str(edge.get("target_node_id"))
            if source in direct_ids:
                direct_ids.add(target)
            if target in direct_ids:
                direct_ids.add(source)

        scored: list[tuple[int, str, Mapping[str, Any]]] = []
        for node_id, node in node_by_id.items():
            if str(node.get("semantic_type") or "") == "OUTPUT":
                continue
            text = _node_text(node)
            overlap = sum(1 for term in root_terms if term in text or any(term in candidate for candidate in _tokens(text)))
            score = 0
            if node_id in root_ids:
                score = 1000
            elif node_id in direct_ids:
                score = 700
            elif str(node.get("semantic_type")) == "RESOURCE":
                # Resources are resolved by bounded evidence relevance below;
                # do not discard a fetched source merely because its title is
                # different from the user's wording.
                score = 80
            elif overlap:
                score = 100 + overlap
            if score:
                scored.append((score, node_id, node))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected_nodes = _deduplicate_nodes([item[2] for item in scored[:_MAX_NODES]])
        selected_ids = {str(node.get("id")) for node in selected_nodes}
        selected_edges = [
            edge
            for edge in edges
            if str(edge.get("source_node_id")) in selected_ids
            and str(edge.get("target_node_id")) in selected_ids
        ]
        selected_edges = sorted(selected_edges, key=lambda edge: str(edge.get("id") or ""))[:_MAX_EDGES]

        query = " ".join(str(node.get("title") or "") for node in roots).strip()
        memory_refs = [
            {
                "source_id": str(reference.get("source_id") or reference.get("id") or ""),
                "title": str(reference.get("title") or "Memory"),
                "summary": str(reference.get("summary") or reference.get("content") or "")[:800],
                "content_hash": reference.get("content_hash") or reference.get("source_hash"),
                "context_role": "MEMORY",
            }
            for reference in _references(self.memory_provider, workspace_id, query)
        ]
        corpus_refs = [
            {
                "source_id": str(reference.get("source_id") or reference.get("id") or ""),
                "title": str(reference.get("title") or "Corpus source"),
                "summary": str(reference.get("summary") or reference.get("content") or "")[:800],
                "content_hash": reference.get("content_hash") or reference.get("source_hash"),
                "context_role": "REFERENCE_ONLY",
            }
            for reference in _references(self.corpus_provider, workspace_id, query)
        ]
        resolved_sources = self.evidence_resolver.resolve(selected_nodes, query=query)
        resource_references = resolved_sources["references"]
        evidence = resolved_sources["evidence"]
        gaps = resolved_sources["gaps"]
        bounded_evidence, _, evidence_truncated = _fit_records(
            evidence,
            budget=CONTEXT_SEGMENT_BUDGETS["evidence"],
            text_key="excerpt",
        )
        bounded_memory, _, memory_truncated = _fit_records(
            memory_refs,
            budget=CONTEXT_SEGMENT_BUDGETS["memory"],
            text_key="summary",
        )
        bounded_references, _, references_truncated = _fit_records(
            resource_references + corpus_refs,
            budget=CONTEXT_SEGMENT_BUDGETS["references"],
            text_key="title",
        )
        node_projections = [node_semantic_projection(node) for node in selected_nodes]
        edge_projections = [edge_semantic_projection(edge) for edge in selected_edges]
        node_versions = [
            {
                "id": projection["id"],
                "version": int(node_by_id[projection["id"]].get("version") or 1),
                "semantic_type": projection["semantic_type"],
                "semantic_hash": _node_semantic_hash(node_by_id[projection["id"]]),
            }
            for projection in node_projections
        ]
        edge_versions = [
            {"id": projection["id"], "semantic_hash": _edge_semantic_hash(edge)}
            for projection, edge in zip(edge_projections, selected_edges, strict=True)
        ]
        resource_versions = [
            {
                "id": projection["id"],
                "version": int(node_by_id[projection["id"]].get("version") or 1),
                "content_hash": projection["content_hash"],
                "semantic_hash": node_versions[index]["semantic_hash"],
            }
            for index, projection in enumerate(node_projections)
            if projection["semantic_type"] == "RESOURCE"
        ]
        source_snapshot: dict[str, Any] = {
            "root_node_ids": root_ids,
            "node_versions": node_versions,
            "edge_versions": edge_versions,
            "resource_versions": resource_versions,
            "memory_refs": bounded_memory,
            "corpus_refs": bounded_references,
            "resource_references": resource_references,
            "evidence": bounded_evidence,
            "gaps": gaps,
            "prior_synthesis_version": prior_synthesis_version,
            "intent": intent,
            "action": action,
        }
        fingerprint_payload = {
            "root_node_ids": root_ids,
            "nodes": node_projections,
            "edges": edge_projections,
            "memory_refs": bounded_memory,
            "corpus_refs": bounded_references,
            "resource_references": resource_references,
            "evidence": bounded_evidence,
            "gaps": gaps,
            "prior_synthesis_version": prior_synthesis_version,
            "intent": intent,
            "action": action,
        }
        source_summary = {
            "concepts": sum(1 for node in selected_nodes if str(node.get("semantic_type")) == "CONCEPT"),
            "resources": sum(1 for node in selected_nodes if str(node.get("semantic_type")) == "RESOURCE"),
            "experts": sum(1 for node in selected_nodes if str(node.get("semantic_type")) == "EXPERT"),
            "actions": sum(1 for node in selected_nodes if str(node.get("semantic_type")) == "ACTION"),
            "memory": len(bounded_memory),
            "corpus": sum(
                1
                for item in bounded_references
                if str(item.get("source_id") or item.get("source_node_id") or "")
                not in {
                    str(reference.get("source_id") or reference.get("source_node_id") or "")
                    for reference in resource_references
                }
            ),
            "references": len(resource_references),
            "evidence_units": len(bounded_evidence),
            "gaps": len(gaps),
            "previous_outputs": 1 if previous_synthesis else 0,
        }
        provider_candidates = [
            node
            for node in selected_nodes
            if str(node.get("semantic_type") or "") not in {"RESOURCE", "OUTPUT"}
        ]
        provider_records = [
            {
                "id": str(node.get("id") or ""),
                "semantic_type": str(node.get("semantic_type") or ""),
                "context_role": _context_role(node),
                "title": str(node.get("title") or "")[:240],
                "content": str(node.get("content") or "")[:4_000],
                "provenance": str(node.get("provenance") or ""),
                "assurance_status": str(node.get("assurance_status") or ""),
                "version": int(node.get("version") or 1),
            }
            for node in provider_candidates
        ]
        root_records = [item for item in provider_records if item["id"] in root_ids]
        concept_records = [item for item in provider_records if item["id"] not in root_ids]
        bounded_roots, _, root_truncated = _fit_records(
            root_records,
            budget=CONTEXT_SEGMENT_BUDGETS["question"],
            text_key="content",
        )
        bounded_concepts, _, concepts_truncated = _fit_records(
            concept_records,
            budget=CONTEXT_SEGMENT_BUDGETS["concepts"],
            text_key="content",
        )
        prompt_nodes = bounded_roots + bounded_concepts
        prior_payload: dict[str, Any] | None = None
        prior_truncated = False
        if previous_synthesis:
            prior_content, prior_truncated = _fit_text(
                str(previous_synthesis.get("content") or ""),
                CONTEXT_SEGMENT_BUDGETS["prior_synthesis"],
            )
            prior_payload = {
                **dict(_json_safe(previous_synthesis)),
                "context_role": "PRIOR_SYNTHESIS",
                "content": prior_content,
            }
        context_receipt = {
            "budget_tokens": DEFAULT_CONTEXT_TOKEN_BUDGET,
            "segment_budgets": dict(CONTEXT_SEGMENT_BUDGETS),
            "estimated_input_tokens": _estimate_tokens(
                _canonical(
                    {
                        "nodes": prompt_nodes,
                        "evidence": bounded_evidence,
                        "references": bounded_references,
                        "memory": bounded_memory,
                        "prior": prior_payload,
                    }
                )
            ),
            "evidence_spans": len(bounded_evidence),
            "concepts": len(bounded_concepts),
            "prior_synthesis_included": prior_payload is not None,
            "references_included": len(bounded_references),
            "truncation": {
                "question": root_truncated,
                "evidence": evidence_truncated,
                "concepts": concepts_truncated,
                "prior_synthesis": prior_truncated,
                "memory": memory_truncated,
                "references": references_truncated,
            },
        }
        fingerprint_payload["context_receipt"] = context_receipt
        return {
            "root_node_ids": root_ids,
            "root_context": [str(node.get("title") or "") for node in roots],
            "intent": intent,
            "action": action,
            "nodes": prompt_nodes,
            "selected_node_ids": [str(node.get("id")) for node in selected_nodes if node.get("id")],
            "edges": edge_projections,
            "memory_refs": bounded_memory,
            "corpus_refs": bounded_references,
            "resource_references": resource_references,
            "evidence": bounded_evidence,
            "evidence_state": resolved_sources.get("evidence_state", "NO_SOURCE"),
            "gaps": gaps,
            "previous_synthesis": prior_payload,
            "source_snapshot": source_snapshot,
            "context_fingerprint": _digest(fingerprint_payload),
            "source_summary": source_summary,
            "context_budget": dict(CONTEXT_SEGMENT_BUDGETS) | {"total": DEFAULT_CONTEXT_TOKEN_BUDGET},
            "context_receipt": context_receipt,
        }


def _snapshot_hashes(snapshot: Mapping[str, Any], key: str) -> dict[str, str]:
    values = snapshot.get(key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return {}
    result: dict[str, str] = {}
    for value in values:
        if isinstance(value, Mapping) and value.get("id"):
            result[str(value["id"])] = str(value.get("semantic_hash") or value.get("content_hash") or "")
    return result


def _stale_summary(
    event: DirtyEvent,
    *,
    added_nodes: Sequence[str],
    changed_nodes: Sequence[str],
    added_resources: Sequence[str],
    changed_resources: Sequence[str],
    changed_edges: Sequence[str],
) -> str:
    parts: list[str] = []
    concept_count = len([node_id for node_id in added_nodes if node_id not in added_resources])
    if concept_count:
        parts.append(f"新增 {concept_count} 個相關概念")
    if len(added_resources) + len(changed_resources):
        parts.append(f"新增或更新 {len(added_resources) + len(changed_resources)} 份資料來源")
    if changed_nodes and not concept_count:
        parts.append(f"更新 {len(changed_nodes)} 個既有脈絡")
    if changed_edges:
        parts.append(f"更新 {len(changed_edges)} 個關係")
    return "、".join(parts) or f"相關脈絡已變更（{event.value}）"


def compare_synthesis_context(
    metadata: Mapping[str, Any],
    current_context: Mapping[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Compare semantic context while ignoring position/version churn from cosmetic edits."""

    old_fingerprint = str(metadata.get("context_fingerprint") or "")
    current_fingerprint = str(current_context.get("context_fingerprint") or "")
    if not old_fingerprint or old_fingerprint == current_fingerprint:
        return False, None
    old_snapshot = metadata.get("source_snapshot")
    current_snapshot = current_context.get("source_snapshot")
    old_snapshot = old_snapshot if isinstance(old_snapshot, Mapping) else {}
    current_snapshot = current_snapshot if isinstance(current_snapshot, Mapping) else {}
    old_nodes = _snapshot_hashes(old_snapshot, "node_versions")
    new_nodes = _snapshot_hashes(current_snapshot, "node_versions")
    old_edges = _snapshot_hashes(old_snapshot, "edge_versions")
    new_edges = _snapshot_hashes(current_snapshot, "edge_versions")
    added_nodes = sorted(set(new_nodes) - set(old_nodes))
    changed_nodes = sorted(node_id for node_id in set(old_nodes) & set(new_nodes) if old_nodes[node_id] != new_nodes[node_id])
    changed_edges = sorted(
        edge_id
        for edge_id in set(old_edges) | set(new_edges)
        if old_edges.get(edge_id) != new_edges.get(edge_id)
    )
    old_resources = _snapshot_hashes(old_snapshot, "resource_versions")
    new_resources = _snapshot_hashes(current_snapshot, "resource_versions")
    added_resources = sorted(set(new_resources) - set(old_resources))
    changed_resources = sorted(
        resource_id
        for resource_id in set(old_resources) & set(new_resources)
        if old_resources[resource_id] != new_resources[resource_id]
    )
    if added_resources or changed_resources:
        event = DirtyEvent.SOURCE_CHANGE
    elif changed_edges:
        event = DirtyEvent.STRUCTURAL_CHANGE
    else:
        event = DirtyEvent.SEMANTIC_CHANGE
    reason = {
        "kind": event.value,
        "added_node_ids": added_nodes,
        "changed_node_ids": changed_nodes,
        "changed_edge_ids": changed_edges,
        "added_resource_ids": added_resources,
        "changed_resource_ids": changed_resources,
        "summary": _stale_summary(
            event,
            added_nodes=added_nodes,
            changed_nodes=changed_nodes,
            added_resources=added_resources,
            changed_resources=changed_resources,
            changed_edges=changed_edges,
        ),
    }
    return True, reason


def classify_node_change(before: Mapping[str, Any], after: Mapping[str, Any]) -> DirtyEvent:
    if node_semantic_projection(before) == node_semantic_projection(after):
        return DirtyEvent.COSMETIC_CHANGE
    if str(before.get("semantic_type")) == "RESOURCE" or str(after.get("semantic_type")) == "RESOURCE":
        return DirtyEvent.SOURCE_CHANGE
    return DirtyEvent.SEMANTIC_CHANGE


def classify_edge_change(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> DirtyEvent:
    if before is not None and after is not None and edge_semantic_projection(before) == edge_semantic_projection(after):
        return DirtyEvent.COSMETIC_CHANGE
    return DirtyEvent.STRUCTURAL_CHANGE
