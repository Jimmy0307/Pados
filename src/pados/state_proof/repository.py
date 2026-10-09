# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from pados.state_proof.object_store import ObjectRef


def register_object(session: Session, ref: ObjectRef) -> dict[str, Any]:
    session.execute(
        text(
            """
            INSERT INTO state_proof_objects
                (id, content_hash, size_bytes, media_type, storage_uri, object_kind)
            VALUES
                (CAST(:id AS uuid), :content_hash, :size_bytes, :media_type, :storage_uri, :object_kind)
            ON CONFLICT (content_hash) DO NOTHING
            """
        ),
        {
            "id": ref.object_id,
            "content_hash": ref.content_hash,
            "size_bytes": ref.size_bytes,
            "media_type": ref.media_type,
            "storage_uri": ref.storage_uri,
            "object_kind": ref.object_kind,
        },
    )
    return dict(
        session.execute(
            text("SELECT * FROM state_proof_objects WHERE content_hash = :content_hash"),
            {"content_hash": ref.content_hash},
        ).mappings().one()
    )


def ensure_scope(session: Session, scope_id: str) -> dict[str, Any]:
    session.execute(
        text(
            """
            INSERT INTO state_proof_scope_heads(scope_id)
            VALUES (:scope_id)
            ON CONFLICT (scope_id) DO NOTHING
            """
        ),
        {"scope_id": scope_id},
    )
    existing = session.execute(
        text("SELECT * FROM state_proof_scope_heads WHERE scope_id = :scope_id"),
        {"scope_id": scope_id},
    ).mappings().one()
    initial = session.execute(
        text(
            """
            INSERT INTO state_proof_scope_versions
                (id, scope_id, version, parent_version, commit_id, manifest_hash)
            VALUES (CAST(:id AS uuid), :scope_id, 0, NULL, NULL, 'INITIAL_SCOPE')
            ON CONFLICT (scope_id, version) DO NOTHING
            """
        ),
        {"id": str(uuid4()), "scope_id": scope_id},
    )
    del initial
    return dict(existing)


def get_scope_head(session: Session, scope_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text("SELECT * FROM state_proof_scope_heads WHERE scope_id = :scope_id"),
        {"scope_id": scope_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_semantic_audit_by_key(
    session: Session, *, scope_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT * FROM state_proof_semantic_audit
            WHERE scope_id = :scope_id AND idempotency_key = :idempotency_key
            """
        ),
        {"scope_id": scope_id, "idempotency_key": idempotency_key},
    ).mappings().first()
    return dict(row) if row is not None else None


def lock_semantic_audit_head(session: Session, scope_id: str) -> str | None:
    session.execute(
        text(
            """
            INSERT INTO state_proof_semantic_audit_heads(scope_id)
            VALUES (:scope_id) ON CONFLICT (scope_id) DO NOTHING
            """
        ),
        {"scope_id": scope_id},
    )
    head = session.execute(
        text(
            """
            SELECT last_hash FROM state_proof_semantic_audit_heads
            WHERE scope_id = :scope_id FOR UPDATE
            """
        ),
        {"scope_id": scope_id},
    ).mappings().one()
    return None if head["last_hash"] is None else str(head["last_hash"])


def append_semantic_audit(
    session: Session, record: Mapping[str, Any]
) -> dict[str, Any]:
    """Append one immutable, idempotent audit record under its scope lock."""
    scope_id = str(record["scope_id"])
    key = str(record["idempotency_key"])
    existing = get_semantic_audit_by_key(session, scope_id=scope_id, idempotency_key=key)
    if existing is not None:
        for field in (
            "actor", "action", "target", "reason", "request_id", "outcome",
            "approval_ref", "rollback_ref",
        ):
            if existing[field] != record[field]:
                raise ValueError("AUDIT_IDEMPOTENCY_CONFLICT")
        return existing

    head_hash = lock_semantic_audit_head(session, scope_id)
    if head_hash != record["previous_hash"]:
        raise ValueError("AUDIT_CHAIN_HEAD_CHANGED")
    row = session.execute(
        text(
            """
            INSERT INTO state_proof_semantic_audit(
                scope_id, actor, action, target, reason, created_at, request_id,
                idempotency_key, outcome, before_state, after_state, approval_ref,
                rollback_ref, previous_hash, record_hash
            ) VALUES (
                :scope_id, :actor, :action, :target, :reason, CAST(:timestamp AS timestamptz),
                :request_id, :idempotency_key, :outcome, CAST(:before_state AS jsonb),
                CAST(:after_state AS jsonb), :approval_ref, :rollback_ref,
                :previous_hash, :record_hash
            ) RETURNING *
            """
        ),
        {
            **dict(record),
            "timestamp": record["timestamp"],
            "before_state": json.dumps(dict(record["before_state"]), sort_keys=True),
            "after_state": json.dumps(dict(record["after_state"]), sort_keys=True),
        },
    ).mappings().one()
    sequence_id = int(row["sequence_id"])
    session.execute(
        text(
            """
            UPDATE state_proof_semantic_audit_heads
            SET last_sequence_id = :sequence_id, last_hash = :record_hash, updated_at = now()
            WHERE scope_id = :scope_id
            """
        ),
        {"scope_id": scope_id, "sequence_id": sequence_id, "record_hash": record["record_hash"]},
    )
    return dict(row)


def semantic_audit_records(session: Session, scope_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT scope_id, actor, action, target, reason, created_at, request_id,
                   idempotency_key, outcome, before_state, after_state, approval_ref,
                   rollback_ref, previous_hash, record_hash
            FROM state_proof_semantic_audit
            WHERE scope_id = :scope_id ORDER BY sequence_id ASC
            """
        ),
        {"scope_id": scope_id},
    ).mappings().all()
    result = []
    for row in rows:
        value = dict(row)
        timestamp = value.pop("created_at")
        if isinstance(timestamp, datetime):
            value["timestamp"] = timestamp.astimezone(UTC).isoformat(timespec="microseconds")
        else:
            value["timestamp"] = str(timestamp)
        result.append(value)
    return result


def create_result(
    session: Session,
    *,
    result_id: str,
    operation_id: str,
    attempt: int,
    operation_type: str,
    run_id: str | None,
    correlation_id: str | None,
    scope_id: str,
    input_scope_version: int,
    input_manifest_hash: str,
    input_manifest_object_id: str,
    context_fingerprint: str | None,
    context_receipt: Mapping[str, Any],
    provider_id: str | None,
    runtime_id: str | None,
    model_id: str | None,
    contract_id: str | None,
    determinism_class: str,
    output_object_id: str | None,
    status: str,
    parsed_payload: Mapping[str, Any] | None,
    failure_classification: str | None,
    duration_ms: int | None,
) -> dict[str, Any]:
    session.execute(
        text(
            """
            INSERT INTO state_proof_results(
                id, operation_id, attempt, operation_type, run_id, correlation_id,
                scope_id, input_scope_version, input_manifest_hash, input_manifest_object_id,
                context_fingerprint, context_receipt_json, provider_id, runtime_id, model_id,
                contract_id, determinism_class, output_object_id, status, parsed_payload_json,
                failure_classification, duration_ms
            ) VALUES (
                CAST(:id AS uuid), :operation_id, :attempt, :operation_type,
                CAST(:run_id AS uuid), :correlation_id, :scope_id, :input_scope_version,
                :input_manifest_hash, CAST(:input_manifest_object_id AS uuid), :context_fingerprint,
                CAST(:context_receipt_json AS jsonb), :provider_id, :runtime_id, :model_id,
                :contract_id, :determinism_class, CAST(:output_object_id AS uuid), :status,
                CAST(:parsed_payload_json AS jsonb), :failure_classification, :duration_ms
            )
            """
        ),
        {
            "id": result_id,
            "operation_id": operation_id,
            "attempt": attempt,
            "operation_type": operation_type,
            "run_id": run_id,
            "correlation_id": correlation_id,
            "scope_id": scope_id,
            "input_scope_version": input_scope_version,
            "input_manifest_hash": input_manifest_hash,
            "input_manifest_object_id": input_manifest_object_id,
            "context_fingerprint": context_fingerprint,
            "context_receipt_json": json.dumps(dict(context_receipt), ensure_ascii=False, sort_keys=True),
            "provider_id": provider_id,
            "runtime_id": runtime_id,
            "model_id": model_id,
            "contract_id": contract_id,
            "determinism_class": determinism_class,
            "output_object_id": output_object_id,
            "status": status,
            "parsed_payload_json": (
                json.dumps(dict(parsed_payload), ensure_ascii=False, sort_keys=True)
                if parsed_payload is not None
                else None
            ),
            "failure_classification": failure_classification,
            "duration_ms": duration_ms,
        },
    )
    return get_result(session, result_id) or {}


def get_result(session: Session, result_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT r.*, o.content_hash AS output_content_hash, o.storage_uri AS output_storage_uri,
                   o.size_bytes AS output_size_bytes, o.media_type AS output_media_type
            FROM state_proof_results r
            LEFT JOIN state_proof_objects o ON o.id = r.output_object_id
            WHERE r.id = CAST(:result_id AS uuid)
            """
        ),
        {"result_id": result_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_result_for_operation(
    session: Session, operation_id: str, attempt: int
) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT r.*, o.content_hash AS output_content_hash, o.storage_uri AS output_storage_uri,
                   o.size_bytes AS output_size_bytes, o.media_type AS output_media_type
            FROM state_proof_results r
            LEFT JOIN state_proof_objects o ON o.id = r.output_object_id
            WHERE r.operation_id = :operation_id AND r.attempt = :attempt
            """
        ),
        {"operation_id": operation_id, "attempt": attempt},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_commit_for_result(session: Session, result_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT * FROM state_proof_commits
            WHERE result_id = CAST(:result_id AS uuid)
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"result_id": result_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_projection_for_result(session: Session, result_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT * FROM state_proof_projections
            WHERE result_id = CAST(:result_id AS uuid) AND status = 'MATERIALIZED'
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"result_id": result_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_object(session: Session, object_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text("SELECT * FROM state_proof_objects WHERE id = CAST(:object_id AS uuid)"),
        {"object_id": object_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def register_product_read_index(
    session: Session,
    *,
    run_id: str,
    scope_id: str,
    idempotency_key: str,
    context_object_id: str,
    context_hash: str,
) -> dict[str, Any]:
    session.execute(
        text(
            """
            INSERT INTO p6_21h_product_read_index
                (run_id, scope_id, idempotency_key, context_object_id, context_hash)
            VALUES
                (CAST(:run_id AS uuid), :scope_id, :idempotency_key,
                 CAST(:context_object_id AS uuid), :context_hash)
            ON CONFLICT (run_id) DO NOTHING
            """
        ),
        {
            "run_id": run_id,
            "scope_id": scope_id,
            "idempotency_key": idempotency_key,
            "context_object_id": context_object_id,
            "context_hash": context_hash,
        },
    )
    return get_product_read_index(session, run_id) or {}


def update_product_read_index(
    session: Session,
    *,
    run_id: str,
    latest_snapshot_object_id: str | None = None,
    latest_snapshot_hash: str | None = None,
    final_report_object_id: str | None = None,
    final_report_hash: str | None = None,
) -> dict[str, Any]:
    values: dict[str, Any] = {"run_id": run_id}
    assignments: list[str] = []
    if latest_snapshot_object_id is not None and latest_snapshot_hash is not None:
        assignments.extend(
            [
                "latest_snapshot_object_id = CAST(:latest_snapshot_object_id AS uuid)",
                "latest_snapshot_hash = :latest_snapshot_hash",
            ]
        )
        values.update(
            {
                "latest_snapshot_object_id": latest_snapshot_object_id,
                "latest_snapshot_hash": latest_snapshot_hash,
            }
        )
    if final_report_object_id is not None and final_report_hash is not None:
        assignments.extend(
            [
                "final_report_object_id = CAST(:final_report_object_id AS uuid)",
                "final_report_hash = :final_report_hash",
            ]
        )
        values.update(
            {
                "final_report_object_id": final_report_object_id,
                "final_report_hash": final_report_hash,
            }
        )
    if assignments:
        assignments.append("updated_at = now()")
        session.execute(
            text(
                "UPDATE p6_21h_product_read_index SET "
                + ", ".join(assignments)
                + " WHERE run_id = CAST(:run_id AS uuid)"
            ),
            values,
        )
    return get_product_read_index(session, run_id) or {}


def get_product_read_index(session: Session, run_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT i.*,
                   c.content_hash AS context_content_hash,
                   c.size_bytes AS context_size_bytes,
                   c.media_type AS context_media_type,
                   c.storage_uri AS context_storage_uri,
                   c.object_kind AS context_object_kind,
                   s.content_hash AS snapshot_content_hash,
                   s.size_bytes AS snapshot_size_bytes,
                   s.media_type AS snapshot_media_type,
                   s.storage_uri AS snapshot_storage_uri,
                   s.object_kind AS snapshot_object_kind,
                   f.content_hash AS report_content_hash,
                   f.size_bytes AS report_size_bytes,
                   f.media_type AS report_media_type,
                   f.storage_uri AS report_storage_uri,
                   f.object_kind AS report_object_kind
            FROM p6_21h_product_read_index i
            JOIN state_proof_objects c ON c.id = i.context_object_id
            LEFT JOIN state_proof_objects s ON s.id = i.latest_snapshot_object_id
            LEFT JOIN state_proof_objects f ON f.id = i.final_report_object_id
            WHERE i.run_id = CAST(:run_id AS uuid)
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def get_results_for_run(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT r.*,
                   i.content_hash AS input_content_hash,
                   i.size_bytes AS input_size_bytes,
                   i.media_type AS input_media_type,
                   i.storage_uri AS input_storage_uri,
                   i.object_kind AS input_object_kind,
                   o.content_hash AS output_content_hash,
                   o.size_bytes AS output_size_bytes,
                   o.media_type AS output_media_type,
                   o.storage_uri AS output_storage_uri,
                   o.object_kind AS output_object_kind
            FROM state_proof_results r
            JOIN state_proof_objects i ON i.id = r.input_manifest_object_id
            LEFT JOIN state_proof_objects o ON o.id = r.output_object_id
            WHERE r.run_id = CAST(:run_id AS uuid)
            ORDER BY r.attempt ASC, r.created_at ASC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def get_commits_for_run(session: Session, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT c.*, r.attempt, r.run_id
            FROM state_proof_commits c
            JOIN state_proof_results r ON r.id = c.result_id
            WHERE r.run_id = CAST(:run_id AS uuid)
            ORDER BY c.created_at ASC
            """
        ),
        {"run_id": run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def commit_result(
    session: Session,
    *,
    scope_id: str,
    expected_version: int,
    result_id: str,
    candidate_ref: str,
    manifest_hash: str,
    actor_type: str = "SYSTEM",
    gate: str | None = None,
) -> dict[str, Any]:
    existing = session.execute(
        text(
            "SELECT * FROM state_proof_commits WHERE scope_id = :scope_id AND result_id = CAST(:result_id AS uuid)"
        ),
        {"scope_id": scope_id, "result_id": result_id},
    ).mappings().first()
    if existing is not None:
        return dict(existing)

    commit_id = str(uuid4())
    head = get_scope_head(session, scope_id)
    if head is None:
        raise ValueError("SCOPE_NOT_FOUND")

    new_version = expected_version + 1
    updated = cast(CursorResult[Any], session.execute(
        text(
            """
            UPDATE state_proof_scope_heads
            SET current_version = :new_version,
                current_commit_id = CAST(:commit_id AS uuid),
                updated_at = now()
            WHERE scope_id = :scope_id AND current_version = :expected_version
            """
        ),
        {
            "scope_id": scope_id,
            "new_version": new_version,
            "commit_id": commit_id,
            "expected_version": expected_version,
        },
    ))
    if updated.rowcount != 1:
        session.execute(
            text(
                """
                INSERT INTO state_proof_commits
                    (id, scope_id, expected_parent_version, result_id, candidate_ref, new_version,
                     status, actor_type, gate)
                VALUES (CAST(:id AS uuid), :scope_id, :expected_version, CAST(:result_id AS uuid),
                        :candidate_ref, NULL, 'STALE_CANDIDATE', :actor_type, :gate)
                """
            ),
            {
                "id": commit_id,
                "scope_id": scope_id,
                "expected_version": expected_version,
                "result_id": result_id,
                "candidate_ref": candidate_ref,
                "actor_type": actor_type,
                "gate": gate,
            },
        )
        return dict(
            session.execute(
                text("SELECT * FROM state_proof_commits WHERE id = CAST(:id AS uuid)"),
                {"id": commit_id},
            ).mappings().one()
        )
    session.execute(
        text(
            """
            INSERT INTO state_proof_commits
                (id, scope_id, expected_parent_version, result_id, candidate_ref, new_version,
                 status, actor_type, gate)
            VALUES (CAST(:id AS uuid), :scope_id, :expected_version, CAST(:result_id AS uuid),
                    :candidate_ref, :new_version, 'COMMITTED', :actor_type, :gate)
            """
        ),
        {
            "id": commit_id,
            "scope_id": scope_id,
            "expected_version": expected_version,
            "result_id": result_id,
            "candidate_ref": candidate_ref,
            "new_version": new_version,
            "actor_type": actor_type,
            "gate": gate,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO state_proof_scope_versions
                (id, scope_id, version, parent_version, commit_id, manifest_hash)
            VALUES (CAST(:id AS uuid), :scope_id, :new_version, :expected_version,
                    CAST(:commit_id AS uuid), :manifest_hash)
            """
        ),
        {
            "id": str(uuid4()),
            "scope_id": scope_id,
            "new_version": new_version,
            "expected_version": expected_version,
            "commit_id": commit_id,
            "manifest_hash": manifest_hash,
        },
    )
    return dict(
        session.execute(
            text("SELECT * FROM state_proof_commits WHERE id = CAST(:id AS uuid)"),
            {"id": commit_id},
        ).mappings().one()
    )


def create_projection(
    session: Session,
    *,
    scope_id: str,
    scope_version: int,
    result_id: str,
    projection_kind: str,
    title: str,
    content: str,
    metadata: Mapping[str, Any],
    contract_id: str,
) -> dict[str, Any]:
    current = session.execute(
        text(
            """
            SELECT * FROM state_proof_projections
            WHERE scope_id = :scope_id AND scope_version = :scope_version
              AND result_id = CAST(:result_id AS uuid)
              AND projection_kind = :projection_kind AND status = 'MATERIALIZED'
            ORDER BY created_at DESC LIMIT 1
            """
        ),
        {
            "scope_id": scope_id,
            "scope_version": scope_version,
            "result_id": result_id,
            "projection_kind": projection_kind,
        },
    ).mappings().first()
    if current is not None:
        return dict(current)
    projection_id = str(uuid4())
    session.execute(
        text(
            """
            INSERT INTO state_proof_projections
                (id, scope_id, scope_version, result_id, projection_kind, title, content,
                 projection_metadata_json, projection_contract_id)
            VALUES (CAST(:id AS uuid), :scope_id, :scope_version, CAST(:result_id AS uuid),
                    :projection_kind, :title, :content, CAST(:metadata AS jsonb), :contract_id)
            """
        ),
        {
            "id": projection_id,
            "scope_id": scope_id,
            "scope_version": scope_version,
            "result_id": result_id,
            "projection_kind": projection_kind,
            "title": title,
            "content": content,
            "metadata": json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True),
            "contract_id": contract_id,
        },
    )
    return dict(
        session.execute(
            text("SELECT * FROM state_proof_projections WHERE id = CAST(:id AS uuid)"),
            {"id": projection_id},
        ).mappings().one()
    )


def delete_projection(session: Session, projection_id: str) -> None:
    session.execute(
        text("DELETE FROM state_proof_projections WHERE id = CAST(:id AS uuid)"),
        {"id": projection_id},
    )


def get_projection(session: Session, projection_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text("SELECT * FROM state_proof_projections WHERE id = CAST(:id AS uuid)"),
        {"id": projection_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def count_rows(session: Session, table_name: str) -> int:
    allowed = {
        "state_proof_objects",
        "state_proof_results",
        "state_proof_commits",
        "state_proof_scope_versions",
        "state_proof_projections",
        "p6_21h_product_read_index",
        "memory_records",
        "memory_relationships",
    }
    if table_name not in allowed:
        raise ValueError("UNAPPROVED_COUNT_TABLE")
    return int(session.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one())
