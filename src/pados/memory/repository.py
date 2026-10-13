# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.models.context_memory import MemoryCandidate
from pados.retrieval.indexer import mock_embedding, sha256_bytes


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def normalize_root(path: Path) -> str:
    return str(path.resolve())


def get_project_root(session: Session, project_id: str) -> Path:
    root = session.execute(
        text("SELECT canonical_root FROM context_project_registry WHERE project_id = :project_id"),
        {"project_id": project_id},
    ).scalar_one()
    return Path(str(root)).resolve()


def register_context_project(
    session: Session,
    *,
    project_id: str,
    canonical_root: Path,
    classification: str,
) -> None:
    resolved = normalize_root(canonical_root)
    existing = session.execute(
        text(
            """
            SELECT project_id FROM context_project_registry
            WHERE canonical_root = :canonical_root AND project_id <> CAST(:project_id AS uuid)
            """
        ),
        {"canonical_root": resolved, "project_id": project_id},
    ).scalar()
    if existing is not None:
        raise ValueError("duplicate canonical root rejected")
    session.execute(
        text(
            """
            INSERT INTO context_project_registry
              (project_id, canonical_root, classification, status, last_verified_at)
            VALUES
              (CAST(:project_id AS uuid), :canonical_root, :classification, 'active', now())
            ON CONFLICT (project_id)
            DO UPDATE SET
              classification = EXCLUDED.classification,
              last_verified_at = now(),
              updated_at = now()
            WHERE context_project_registry.canonical_root = EXCLUDED.canonical_root
            """
        ),
        {"project_id": project_id, "canonical_root": resolved, "classification": classification},
    )
    session.execute(
        text(
            """
            UPDATE projects
            SET classification = :classification, last_verified_at = now(), updated_at = now()
            WHERE id = CAST(:project_id AS uuid)
            """
        ),
        {"project_id": project_id, "classification": classification},
    )


def register_source(
    session: Session,
    *,
    project_id: str,
    source_type: str,
    source_uri: str,
    canonical_path: str | None,
    source_hash: str | None,
    verification_status: str,
    freshness_status: str,
    trust_level: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    source_id = session.execute(
        text(
            """
            INSERT INTO context_sources
              (project_id, source_type, source_uri, canonical_path, source_hash,
               verification_status, freshness_status, trust_level, metadata)
            VALUES
              (CAST(:project_id AS uuid), :source_type, :source_uri, :canonical_path, :source_hash,
               :verification_status, :freshness_status, :trust_level, CAST(:metadata AS jsonb))
            ON CONFLICT (project_id, source_uri)
            DO UPDATE SET
              source_type = EXCLUDED.source_type,
              canonical_path = EXCLUDED.canonical_path,
              source_hash = EXCLUDED.source_hash,
              verification_status = EXCLUDED.verification_status,
              freshness_status = EXCLUDED.freshness_status,
              trust_level = EXCLUDED.trust_level,
              metadata = EXCLUDED.metadata,
              updated_at = now()
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "source_type": source_type,
            "source_uri": source_uri,
            "canonical_path": canonical_path,
            "source_hash": source_hash,
            "verification_status": verification_status,
            "freshness_status": freshness_status,
            "trust_level": trust_level,
            "metadata": _json(metadata or {}),
        },
    ).scalar_one()
    return str(source_id)


def insert_memory(
    session: Session,
    *,
    project_id: str,
    memory_type: str,
    title: str,
    content_summary: str,
    source_type: str,
    source_uri: str,
    source_hash: str | None,
    source_trust_level: str,
    verification_status: str,
    freshness_status: str,
    importance: int,
    retrieval_priority: int,
    supersedes_memory_id: str | None = None,
) -> str:
    embedding = mock_embedding(f"{title}\n{content_summary}")
    memory_id = session.execute(
        text(
            """
            INSERT INTO memory_records
              (project_id, memory_type, title, content_summary, source_type, source_uri,
               source_hash, source_trust_level, verification_status, freshness_status,
               importance, retrieval_priority, supersedes_memory_id, embedding)
            VALUES
              (CAST(:project_id AS uuid), :memory_type, :title, :content_summary, :source_type,
               :source_uri, :source_hash, :source_trust_level, :verification_status,
               :freshness_status, :importance, :retrieval_priority,
               CAST(:supersedes_memory_id AS uuid), CAST(:embedding AS vector))
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "memory_type": memory_type,
            "title": title,
            "content_summary": content_summary,
            "source_type": source_type,
            "source_uri": source_uri,
            "source_hash": source_hash,
            "source_trust_level": source_trust_level,
            "verification_status": verification_status,
            "freshness_status": freshness_status,
            "importance": importance,
            "retrieval_priority": retrieval_priority,
            "supersedes_memory_id": supersedes_memory_id,
            "embedding": embedding,
        },
    ).scalar_one()
    return str(memory_id)


def get_current_authority(
    session: Session,
    *,
    memory_id: str,
    lock: bool = False,
) -> dict[str, Any] | None:
    if lock:
        session.execute(
            text(
                "SELECT id FROM memory_records WHERE id = CAST(:memory_id AS uuid) FOR UPDATE"
            ),
            {"memory_id": memory_id},
        ).scalar_one_or_none()
    row = session.execute(
        text(
            """
            SELECT id, memory_id, project_id, version, authority_state,
                   origin_type, origin_actor_type, origin_actor_id,
                   actor_type, actor_id, source_ref, evidence_ref, evidence_status,
                   result_ref, run_id, provider, model, runtime, contract_id,
                   promotion_gate, promotion_reason, supersedes_memory_id,
                   superseded_by_memory_id, previous_event_id, operation_key, created_at
            FROM memory_authority_events
            WHERE memory_id = CAST(:memory_id AS uuid)
            ORDER BY version DESC
            LIMIT 1
            """
        ),
        {"memory_id": memory_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_authority_event_by_operation(session: Session, *, operation_key: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT id, memory_id, project_id, version, authority_state,
                   origin_type, origin_actor_type, origin_actor_id,
                   actor_type, actor_id, source_ref, evidence_ref, evidence_status,
                   result_ref, run_id, provider, model, runtime, contract_id,
                   promotion_gate, promotion_reason, supersedes_memory_id,
                   superseded_by_memory_id, previous_event_id, operation_key, created_at
            FROM memory_authority_events
            WHERE operation_key = :operation_key
            """
        ),
        {"operation_key": operation_key},
    ).mappings().first()
    return dict(row) if row is not None else None


def insert_authority_event(
    session: Session,
    *,
    memory_id: str,
    project_id: str,
    version: int,
    authority_state: str,
    origin_type: str,
    origin_actor_type: str | None,
    origin_actor_id: str | None,
    actor_type: str | None,
    actor_id: str | None,
    source_ref: str | None,
    evidence_ref: str | None,
    evidence_status: str,
    result_ref: str | None,
    run_id: str | None,
    provider: str | None,
    model: str | None,
    runtime: str | None,
    contract_id: str | None,
    promotion_gate: str | None,
    promotion_reason: str | None,
    supersedes_memory_id: str | None,
    superseded_by_memory_id: str | None,
    previous_event_id: str | None,
    operation_key: str,
) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            INSERT INTO memory_authority_events
              (memory_id, project_id, version, authority_state, origin_type,
               origin_actor_type, origin_actor_id, actor_type, actor_id,
               source_ref, evidence_ref, evidence_status, result_ref, run_id,
               provider, model, runtime, contract_id, promotion_gate,
               promotion_reason, supersedes_memory_id, superseded_by_memory_id,
               previous_event_id, operation_key)
            VALUES
              (CAST(:memory_id AS uuid), CAST(:project_id AS uuid), :version,
               :authority_state, :origin_type, :origin_actor_type, :origin_actor_id,
               :actor_type, :actor_id, :source_ref, :evidence_ref, :evidence_status,
               :result_ref, CAST(:run_id AS uuid), :provider, :model, :runtime,
               :contract_id, :promotion_gate, :promotion_reason,
               CAST(:supersedes_memory_id AS uuid),
               CAST(:superseded_by_memory_id AS uuid),
               CAST(:previous_event_id AS uuid), :operation_key)
            RETURNING id, memory_id, project_id, version, authority_state,
                      origin_type, origin_actor_type, origin_actor_id,
                      actor_type, actor_id, source_ref, evidence_ref, evidence_status,
                      result_ref, run_id, provider, model, runtime, contract_id,
                      promotion_gate, promotion_reason, supersedes_memory_id,
                      superseded_by_memory_id, previous_event_id, operation_key, created_at
            """
        ),
        {
            "memory_id": memory_id,
            "project_id": project_id,
            "version": version,
            "authority_state": authority_state,
            "origin_type": origin_type,
            "origin_actor_type": origin_actor_type,
            "origin_actor_id": origin_actor_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "source_ref": source_ref,
            "evidence_ref": evidence_ref,
            "evidence_status": evidence_status,
            "result_ref": result_ref,
            "run_id": run_id,
            "provider": provider,
            "model": model,
            "runtime": runtime,
            "contract_id": contract_id,
            "promotion_gate": promotion_gate,
            "promotion_reason": promotion_reason,
            "supersedes_memory_id": supersedes_memory_id,
            "superseded_by_memory_id": superseded_by_memory_id,
            "previous_event_id": previous_event_id,
            "operation_key": operation_key,
        },
    ).mappings().one()
    return dict(row)


def update_memory_compatibility(
    session: Session,
    *,
    memory_id: str,
    source_trust_level: str | None = None,
    verification_status: str | None = None,
    freshness_status: str | None = None,
) -> None:
    session.execute(
        text(
            """
            UPDATE memory_records
            SET source_trust_level = COALESCE(:source_trust_level, source_trust_level),
                verification_status = COALESCE(:verification_status, verification_status),
                freshness_status = COALESCE(:freshness_status, freshness_status),
                updated_at = now()
            WHERE id = CAST(:memory_id AS uuid)
            """
        ),
        {
            "memory_id": memory_id,
            "source_trust_level": source_trust_level,
            "verification_status": verification_status,
            "freshness_status": freshness_status,
        },
    )


def update_memory_status(
    session: Session,
    *,
    memory_id: str,
    verification_status: str | None = None,
    freshness_status: str | None = None,
    expected_authority_version: int | None = None,
) -> None:
    if verification_status is not None:
        if expected_authority_version is None:
            raise ValueError("expected_authority_version is required for authority status changes")
        current = get_current_authority(session, memory_id=memory_id, lock=True)
        if current is None or int(current["version"]) != expected_authority_version:
            raise ValueError("STALE_MEMORY_VERSION")
    session.execute(
        text(
            """
            UPDATE memory_records
            SET verification_status = COALESCE(:verification_status, verification_status),
                freshness_status = COALESCE(:freshness_status, freshness_status),
                updated_at = now()
            WHERE id = CAST(:memory_id AS uuid)
            """
        ),
        {
            "memory_id": memory_id,
            "verification_status": verification_status,
            "freshness_status": freshness_status,
        },
    )


def insert_relationship(
    session: Session,
    *,
    project_id: str,
    from_memory_id: str,
    to_memory_id: str,
    relationship_type: str,
    evidence_uri: str | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO memory_relationships
              (project_id, from_memory_id, to_memory_id, relationship_type, evidence_uri)
            VALUES
              (CAST(:project_id AS uuid), CAST(:from_memory_id AS uuid), CAST(:to_memory_id AS uuid),
               :relationship_type, :evidence_uri)
            ON CONFLICT (from_memory_id, to_memory_id, relationship_type) DO NOTHING
            """
        ),
        {
            "project_id": project_id,
            "from_memory_id": from_memory_id,
            "to_memory_id": to_memory_id,
            "relationship_type": relationship_type,
            "evidence_uri": evidence_uri,
        },
    )


def create_run(
    session: Session,
    *,
    project_id: str,
    run_type: str,
    worker_type: str,
    status: str = "CREATED",
) -> str:
    run_id = session.execute(
        text(
            """
            INSERT INTO context_runs (project_id, run_type, worker_type, status)
            VALUES (CAST(:project_id AS uuid), :run_type, :worker_type, :status)
            RETURNING id
            """
        ),
        {"project_id": project_id, "run_type": run_type, "worker_type": worker_type, "status": status},
    ).scalar_one()
    return str(run_id)


def append_event(
    session: Session,
    *,
    run_id: str,
    project_id: str,
    event_type: str,
    payload_json: dict[str, Any],
    evidence_uri: str | None = None,
) -> str:
    session.execute(
        text("SELECT id FROM context_runs WHERE id = CAST(:run_id AS uuid) FOR UPDATE"),
        {"run_id": run_id},
    ).scalar_one()
    sequence_number = session.execute(
        text(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1
            FROM run_events
            WHERE run_id = CAST(:run_id AS uuid)
            """
        ),
        {"run_id": run_id},
    ).scalar_one()
    event_id = session.execute(
        text(
            """
            INSERT INTO run_events
              (run_id, project_id, event_type, sequence_number, payload_json, evidence_uri)
            VALUES
              (CAST(:run_id AS uuid), CAST(:project_id AS uuid), :event_type, :sequence_number,
               CAST(:payload_json AS jsonb), :evidence_uri)
            RETURNING id
            """
        ),
        {
            "run_id": run_id,
            "project_id": project_id,
            "event_type": event_type,
            "sequence_number": sequence_number,
            "payload_json": _json(payload_json),
            "evidence_uri": evidence_uri,
        },
    ).scalar_one()
    return str(event_id)


def insert_decision(
    session: Session,
    *,
    project_id: str,
    run_id: str | None,
    decision_type: str,
    question: str,
    decision: str,
    rationale: str,
    status: str,
    decided_by: str | None,
    evidence_uri: str | None,
    supersedes_decision_id: str | None = None,
) -> str:
    decision_id = session.execute(
        text(
            """
            INSERT INTO memory_decisions
              (project_id, run_id, decision_type, question, decision, rationale, status,
               decided_by, evidence_uri, decided_at, supersedes_decision_id)
            VALUES
              (CAST(:project_id AS uuid), CAST(:run_id AS uuid), :decision_type, :question,
               :decision, :rationale, :status, :decided_by, :evidence_uri,
               CASE WHEN :status IN ('APPROVED', 'REJECTED') THEN now() ELSE NULL END,
               CAST(:supersedes_decision_id AS uuid))
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "run_id": run_id,
            "decision_type": decision_type,
            "question": question,
            "decision": decision,
            "rationale": rationale,
            "status": status,
            "decided_by": decided_by,
            "evidence_uri": evidence_uri,
            "supersedes_decision_id": supersedes_decision_id,
        },
    ).scalar_one()
    return str(decision_id)


def insert_artifact(
    session: Session,
    *,
    project_id: str,
    run_id: str | None,
    artifact_type: str,
    canonical_path: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    verification_status: str,
) -> str:
    artifact_id = session.execute(
        text(
            """
            INSERT INTO memory_artifacts
              (project_id, run_id, artifact_type, canonical_path, sha256, size_bytes,
               media_type, verification_status)
            VALUES
              (CAST(:project_id AS uuid), CAST(:run_id AS uuid), :artifact_type,
               :canonical_path, :sha256, :size_bytes, :media_type, :verification_status)
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "canonical_path": canonical_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "verification_status": verification_status,
        },
    ).scalar_one()
    return str(artifact_id)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_hash_for_text(text_value: str) -> str:
    return sha256_bytes(text_value.encode("utf-8"))


def search_memory_rows(
    session: Session,
    *,
    project_id: str,
    query: str,
    memory_types: tuple[str, ...],
    include_cross_project: bool = False,
    limit: int = 50,
) -> list[MemoryCandidate]:
    if not query.strip():
        raise ValueError("query must not be empty")

    project_filter = "" if include_cross_project else "AND mr.project_id = CAST(:project_id AS uuid)"
    type_filter = "AND mr.memory_type = ANY(:memory_types)" if memory_types else ""
    common_select = """
        SELECT mr.id, mr.project_id, mr.memory_type, mr.title, mr.content_summary, mr.source_type,
               mr.source_uri, mr.source_hash, mr.source_trust_level, mr.verification_status,
               mr.freshness_status, mr.importance, mr.retrieval_priority,
               authority.id AS authority_event_id,
               authority.authority_state,
               authority.version AS authority_version,
               authority.origin_type,
               authority.origin_actor_type,
               authority.origin_actor_id,
               authority.actor_type,
               authority.actor_id,
               authority.source_ref,
               authority.evidence_ref,
               authority.evidence_status,
               authority.result_ref,
               authority.run_id,
               authority.provider,
               authority.model,
               authority.runtime,
               authority.contract_id,
               authority.promotion_gate,
               CASE WHEN authority.authority_state IN ('PROMOTED', 'HUMAN_CONFIRMED')
                    THEN TRUE ELSE FALSE END AS context_eligible,
               {keyword_rank} AS keyword_rank,
               {lexical_match_score} AS lexical_match_score,
                CASE mr.source_trust_level
                 WHEN 'CANONICAL' THEN 60
                 WHEN 'VERIFIED_GENERATED' THEN 45
                 WHEN 'USER_CONFIRMED' THEN 40
                 WHEN 'OBSERVED' THEN 30
                 WHEN 'MODEL_INFERRED' THEN 10
                 ELSE 0
               END AS trust_score,
                CASE mr.verification_status WHEN 'VERIFIED' THEN 100 ELSE 0 END AS verified_score,
                CASE mr.freshness_status WHEN 'CURRENT' THEN 50 WHEN 'UNKNOWN' THEN 10 ELSE -50 END
                 AS freshness_score,
               :retrieval_reason AS retrieval_reason
         FROM memory_records mr
         LEFT JOIN LATERAL (
             SELECT mae.id, mae.authority_state, mae.version, mae.origin_type,
                    mae.origin_actor_type, mae.origin_actor_id, mae.actor_type,
                    mae.actor_id, mae.source_ref, mae.evidence_ref,
                    mae.evidence_status, mae.result_ref, mae.run_id, mae.provider,
                    mae.model, mae.runtime, mae.contract_id, mae.promotion_gate
             FROM memory_authority_events mae
             WHERE mae.memory_id = mr.id
             ORDER BY mae.version DESC
             LIMIT 1
         ) authority ON TRUE
         WHERE 1 = 1
          {project_filter}
          {type_filter}
          AND ({condition})
        ORDER BY
           verified_score DESC,
           freshness_score DESC,
           trust_score DESC,
           lexical_match_score DESC,
           keyword_rank DESC,
           mr.importance DESC,
           mr.retrieval_priority DESC,
           mr.updated_at DESC
        LIMIT :limit
    """
    exact_query = common_select.format(
        keyword_rank="ts_rank(mr.search_vector, plainto_tsquery('english', :query))",
        lexical_match_score="0",
        condition="mr.search_vector @@ plainto_tsquery('english', :query)",
        project_filter=project_filter,
        type_filter=type_filter,
    )
    parameters: dict[str, Any] = {
        "project_id": project_id,
        "query": query,
        "memory_types": list(memory_types),
        "limit": limit,
        "retrieval_reason": "exact_fts",
    }
    rows = session.execute(text(exact_query), parameters).mappings().all()

    if not rows:
        tokens = _retrieval_tokens(query)
        if tokens:
            token_predicates = " OR ".join(
                    f"lower(mr.title) LIKE :token_{index} OR lower(mr.content_summary) LIKE :token_{index}"
                for index in range(len(tokens))
            )
            relaxed_query = common_select.format(
                keyword_rank="0",
                lexical_match_score=" + ".join(
                    f"CASE WHEN lower(mr.title) LIKE :token_{index} THEN 2 ELSE 0 END + "
                    f"CASE WHEN lower(mr.content_summary) LIKE :token_{index} THEN 1 ELSE 0 END"
                    for index in range(len(tokens))
                ),
                condition=token_predicates.replace("lower(title)", "lower(mr.title)").replace(
                    "lower(content_summary)", "lower(mr.content_summary)"
                ),
                project_filter=project_filter,
                type_filter=type_filter,
            )
            relaxed_parameters: dict[str, Any] = {
                "project_id": project_id,
                "memory_types": list(memory_types),
                "limit": limit,
                "retrieval_reason": "relaxed_fts",
            }
            relaxed_parameters.update(
                {f"token_{index}": f"%{token}%" for index, token in enumerate(tokens)}
            )
            rows = session.execute(text(relaxed_query), relaxed_parameters).mappings().all()

    candidates: list[MemoryCandidate] = []
    for row in rows:
        summary = str(row["content_summary"])
        estimated = max(1, (len(summary) + len(str(row["title"])) + 3) // 4)
        score = (
            float(row["keyword_rank"] or 0)
            + float(row["lexical_match_score"] or 0)
            + float(row["importance"])
            + float(row["retrieval_priority"])
        )
        candidates.append(
            MemoryCandidate(
                memory_id=str(row["id"]),
                project_id=str(row["project_id"]),
                memory_type=str(row["memory_type"]),
                title=str(row["title"]),
                content_summary=summary,
                source_type=str(row["source_type"]),
                source_uri=str(row["source_uri"]),
                source_hash=str(row["source_hash"]) if row["source_hash"] is not None else None,
                source_trust_level=str(row["source_trust_level"]),
                verification_status=str(row["verification_status"]),
                freshness_status=str(row["freshness_status"]),
                importance=int(row["importance"]),
                retrieval_priority=int(row["retrieval_priority"]),
                estimated_tokens=estimated,
                score=score,
                retrieval_reason=str(row["retrieval_reason"]),
                authority_event_id=str(row["authority_event_id"])
                if row["authority_event_id"] is not None
                else None,
                authority_state=str(row["authority_state"] or "LEGACY_UNKNOWN"),
                authority_version=int(row["authority_version"])
                if row["authority_version"] is not None
                else None,
                context_eligible=bool(row["context_eligible"]),
                origin_type=str(row["origin_type"] or "UNKNOWN"),
                origin_actor_type=str(row["origin_actor_type"])
                if row["origin_actor_type"] is not None
                else None,
                origin_actor_id=str(row["origin_actor_id"])
                if row["origin_actor_id"] is not None
                else None,
                actor_type=str(row["actor_type"]) if row["actor_type"] is not None else None,
                actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
                source_ref=str(row["source_ref"]) if row["source_ref"] is not None else None,
                evidence_ref=str(row["evidence_ref"])
                if row["evidence_ref"] is not None
                else None,
                evidence_status=str(row["evidence_status"] or "UNKNOWN"),
                result_ref=str(row["result_ref"]) if row["result_ref"] is not None else None,
                run_id=str(row["run_id"]) if row["run_id"] is not None else None,
                provider=str(row["provider"]) if row["provider"] is not None else None,
                model=str(row["model"]) if row["model"] is not None else None,
                runtime=str(row["runtime"]) if row["runtime"] is not None else None,
                contract_id=str(row["contract_id"]) if row["contract_id"] is not None else None,
                promotion_gate=str(row["promotion_gate"])
                if row["promotion_gate"] is not None
                else None,
            )
        )
    return candidates


def _retrieval_tokens(query: str) -> list[str]:
    """Return safe ASCII lexical tokens for the controlled fallback.

    The local mock embedding is a deterministic hash, not a semantic embedding,
    so multilingual semantic retrieval remains intentionally disabled. This
    fallback only prevents mixed-language operator queries from becoming a
    silent zero-result when they contain recognizable project keywords.
    """

    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query.casefold())
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))
