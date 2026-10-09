# SPDX-License-Identifier: AGPL-3.0-only
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.memory.retrieval import POLICY_VERSION, RetrievalRequest, retrieve_with_budget
from pados.memory.security import assert_no_secret


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def assemble_context_packet(
    session: Session,
    *,
    request: RetrievalRequest,
    request_id: str,
) -> dict[str, Any]:
    budget = retrieve_with_budget(session, request)
    packet_id = session.execute(
        text(
            """
            INSERT INTO context_packets
              (project_id, request_id, purpose, token_budget, estimated_tokens,
               assembly_policy_version)
            VALUES
              (CAST(:project_id AS uuid), :request_id, :purpose, :token_budget,
               :estimated_tokens, :policy_version)
            RETURNING id
            """
        ),
        {
            "project_id": request.project_id,
            "request_id": request_id,
            "purpose": request.purpose,
            "token_budget": request.max_tokens,
            "estimated_tokens": sum(item.estimated_tokens for item in budget.selected),
            "policy_version": POLICY_VERSION,
        },
    ).scalar_one()
    packet_id_text = str(packet_id)
    for rank, candidate in enumerate(budget.selected, start=1):
        assert_no_secret(candidate.content_summary, field_name="content_snapshot")
        session.execute(
            text(
                """
                INSERT INTO context_packet_items
                  (context_packet_id, source_memory_id, rank, reason, estimated_tokens,
                   content_snapshot, source_hash, verification_status,
                   authority_event_id, authority_state, authority_version,
                   origin_type, origin_actor_type, origin_actor_id, actor_type, actor_id,
                   source_ref, evidence_ref, evidence_status, result_ref, run_id,
                   provider, model, runtime, contract_id, promotion_gate, context_eligible)
                VALUES
                  (CAST(:packet_id AS uuid), CAST(:source_memory_id AS uuid), :rank,
                   :reason, :estimated_tokens, :content_snapshot, :source_hash,
                   :verification_status, CAST(:authority_event_id AS uuid), :authority_state,
                   :authority_version, :origin_type, :origin_actor_type, :origin_actor_id,
                   :actor_type, :actor_id, :source_ref, :evidence_ref, :evidence_status,
                   :result_ref, CAST(:run_id AS uuid), :provider, :model, :runtime,
                   :contract_id, :promotion_gate, :context_eligible)
                """
            ),
            {
                "packet_id": packet_id_text,
                "source_memory_id": candidate.memory_id,
                "rank": rank,
                "reason": candidate.retrieval_reason,
                "estimated_tokens": candidate.estimated_tokens,
                "content_snapshot": candidate.content_summary,
                "source_hash": candidate.source_hash,
                "verification_status": candidate.verification_status,
                "authority_event_id": candidate.authority_event_id,
                "authority_state": candidate.authority_state,
                "authority_version": candidate.authority_version,
                "origin_type": candidate.origin_type,
                "origin_actor_type": candidate.origin_actor_type,
                "origin_actor_id": candidate.origin_actor_id,
                "actor_type": candidate.actor_type,
                "actor_id": candidate.actor_id,
                "source_ref": candidate.source_ref,
                "evidence_ref": candidate.evidence_ref,
                "evidence_status": candidate.evidence_status,
                "result_ref": candidate.result_ref,
                "run_id": candidate.run_id,
                "provider": candidate.provider,
                "model": candidate.model,
                "runtime": candidate.runtime,
                "contract_id": candidate.contract_id,
                "promotion_gate": candidate.promotion_gate,
                "context_eligible": candidate.context_eligible,
            },
        )

    return context_packet_json(session, packet_id_text, excluded=budget.excluded)


def context_packet_json(
    session: Session,
    packet_id: str,
    *,
    excluded: Any = (),
) -> dict[str, Any]:
    packet = session.execute(
        text(
            """
            SELECT cp.id, cp.project_id, cp.purpose, cp.token_budget, cp.estimated_tokens,
                   cp.assembly_policy_version, cp.created_at, p.name, p.status, p.last_verified_at
            FROM context_packets cp
            JOIN projects p ON p.id = cp.project_id
            WHERE cp.id = CAST(:packet_id AS uuid)
            """
        ),
        {"packet_id": packet_id},
    ).mappings().one()
    rows = session.execute(
        text(
            """
            SELECT cpi.rank, cpi.reason, cpi.estimated_tokens, cpi.content_snapshot,
                   cpi.source_hash, cpi.verification_status, cpi.authority_event_id,
                   cpi.authority_state, cpi.authority_version, cpi.origin_type,
                   cpi.origin_actor_type, cpi.origin_actor_id, cpi.actor_type, cpi.actor_id,
                   cpi.source_ref, cpi.evidence_ref, cpi.evidence_status, cpi.result_ref,
                   cpi.run_id, cpi.provider, cpi.model, cpi.runtime, cpi.contract_id,
                   cpi.promotion_gate, cpi.context_eligible, mr.memory_type, mr.title,
                   mr.source_type, mr.source_uri, mr.source_trust_level, mr.freshness_status
            FROM context_packet_items cpi
            LEFT JOIN memory_records mr ON mr.id = cpi.source_memory_id
            WHERE cpi.context_packet_id = CAST(:packet_id AS uuid)
            ORDER BY cpi.rank
            """
        ),
        {"packet_id": packet_id},
    ).mappings().all()
    selected = [
        {
            "rank": int(row["rank"]),
            "title": str(row["title"]),
            "memory_type": str(row["memory_type"]),
            "source_type": str(row["source_type"]),
            "summary": str(row["content_snapshot"]),
            "reason": str(row["reason"]),
            "estimated_tokens": int(row["estimated_tokens"]),
            "source_uri": str(row["source_uri"]),
            "source_hash": row["source_hash"],
            "verification_status": str(row["verification_status"]),
            "freshness_status": str(row["freshness_status"]),
            "source_trust_level": str(row["source_trust_level"]),
            "authority_event_id": str(row["authority_event_id"])
            if row["authority_event_id"] is not None
            else None,
            "authority_state": str(row["authority_state"]),
            "authority_version": int(row["authority_version"])
            if row["authority_version"] is not None
            else None,
            "context_eligible": bool(row["context_eligible"]),
            "origin_type": str(row["origin_type"]),
            "origin_actor_type": str(row["origin_actor_type"])
            if row["origin_actor_type"] is not None
            else None,
            "origin_actor_id": str(row["origin_actor_id"])
            if row["origin_actor_id"] is not None
            else None,
            "actor_type": str(row["actor_type"]) if row["actor_type"] is not None else None,
            "actor_id": str(row["actor_id"]) if row["actor_id"] is not None else None,
            "source_ref": str(row["source_ref"]) if row["source_ref"] is not None else None,
            "evidence_ref": str(row["evidence_ref"])
            if row["evidence_ref"] is not None
            else None,
            "evidence_status": str(row["evidence_status"]),
            "result_ref": str(row["result_ref"]) if row["result_ref"] is not None else None,
            "run_id": str(row["run_id"]) if row["run_id"] is not None else None,
            "provider": str(row["provider"]) if row["provider"] is not None else None,
            "model": str(row["model"]) if row["model"] is not None else None,
            "runtime": str(row["runtime"]) if row["runtime"] is not None else None,
            "contract_id": str(row["contract_id"]) if row["contract_id"] is not None else None,
            "promotion_gate": str(row["promotion_gate"])
            if row["promotion_gate"] is not None
            else None,
        }
        for row in rows
    ]
    excluded_sources = [
        {"source": item.title, "memory_id": item.memory_id, "reason": item.reason.value}
        for item in excluded
    ]
    by_type: dict[str, list[dict[str, Any]]] = {
        "verified_facts": [],
        "locked_decisions": [],
        "recent_events": [],
        "relevant_artifacts": [],
        "procedural_memory": [],
        "known_failures": [],
        "unresolved_conflicts": [],
    }
    for item in selected:
        if item["verification_status"] == "CONFLICTED":
            by_type["unresolved_conflicts"].append(item)
        elif item["memory_type"] == "decision":
            by_type["locked_decisions"].append(item)
        elif item["memory_type"] == "artifact":
            by_type["relevant_artifacts"].append(item)
        elif item["memory_type"] == "procedural":
            by_type["procedural_memory"].append(item)
        elif item["source_type"].lower() in {
            "failure",
            "known_failure",
            "validation_failure",
            "defect",
            "error",
        }:
            by_type["known_failures"].append(item)
        else:
            by_type["verified_facts"].append(item)
    generated_at = datetime.now(UTC).isoformat()
    return {
        "context_packet": {
            "packet_id": str(packet["id"]),
            "project": {
                "project_id": str(packet["project_id"]),
                "name": str(packet["name"]),
            },
            "purpose": str(packet["purpose"]),
            "token_budget": {"max_tokens": int(packet["token_budget"])},
            "estimated_tokens": int(packet["estimated_tokens"]),
            "project_state": {
                "status": str(packet["status"]),
                "last_verified_at": str(packet["last_verified_at"])
                if packet["last_verified_at"] is not None
                else None,
            },
            "verified_facts": by_type["verified_facts"],
            "locked_decisions": by_type["locked_decisions"],
            "recent_events": by_type["recent_events"],
            "relevant_artifacts": by_type["relevant_artifacts"],
            "procedural_memory": by_type["procedural_memory"],
            "known_failures": by_type["known_failures"],
            "unresolved_conflicts": by_type["unresolved_conflicts"],
            "excluded_sources": excluded_sources,
            "provenance": {
                "policy_version": str(packet["assembly_policy_version"]),
                "generated_at": generated_at,
                "selected_count": len(selected),
                "excluded_count": len(excluded_sources),
                "retrieval_reasons": sorted({item["reason"] for item in selected}),
                "vector_status": "disabled_mock_embedding_not_semantic",
                "multilingual_status": "ascii_keyword_fallback_only",
            },
        }
    }


def write_packet_artifacts(packet: dict[str, Any], *, json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(_stable_json(packet), encoding="utf-8")
    markdown_path.write_text(packet_markdown(packet), encoding="utf-8")


def packet_markdown(packet: dict[str, Any]) -> str:
    body = packet["context_packet"]
    lines = [
        f"# Context Packet {body['packet_id']}",
        "",
        f"- Project: {body['project']['name']}",
        f"- Purpose: {body['purpose']}",
        f"- Estimated tokens: {body['estimated_tokens']}",
        f"- Policy: {body['provenance']['policy_version']}",
        "",
        "## Selected Items",
    ]
    selected_sections = [
        "verified_facts",
        "locked_decisions",
        "recent_events",
        "relevant_artifacts",
        "procedural_memory",
        "known_failures",
        "unresolved_conflicts",
    ]
    for section in selected_sections:
        for item in body[section]:
            lines.append(f"- [{section}] {item['title']} ({item['verification_status']})")
    lines.extend(["", "## Excluded Sources"])
    for item in body["excluded_sources"]:
        lines.append(f"- {item['source']}: {item['reason']}")
    lines.extend(["", "## Provenance", f"- Generated: {body['provenance']['generated_at']}"])
    return "\n".join(lines) + "\n"
