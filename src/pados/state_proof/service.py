# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pados.graph.cognition import (
    CognitiveResponseError,
    parse_cognitive_response,
    validate_evidence_refs,
)
from pados.state_proof import repository
from pados.state_proof.audit_chain import build_audit_record, verify_audit_chain
from pados.state_proof.object_store import ObjectRef, ObjectStore


class CognitiveProofProvider(Protocol):
    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        """Return one raw provider response for the bounded proof operation."""


class StateProofService:
    """Small coordinator for immutable results, CAS commits, and projections."""

    def __init__(self, engine: Engine, object_store: ObjectStore) -> None:
        self.engine = engine
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self.object_store = object_store

    def _session(self) -> Session:
        return self.session_factory()

    def ensure_scope(self, scope_id: str) -> dict[str, Any]:
        with self._session() as session:
            head = repository.ensure_scope(session, scope_id)
            session.commit()
            return head

    async def execute(
        self,
        *,
        scope_id: str,
        operation_id: str,
        specification: Mapping[str, Any],
        context_receipt: Mapping[str, Any],
        provider: CognitiveProofProvider,
        operation_type: str = "COGNITIVE_SYNTHESIS",
        expected_version: int | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        provider_id: str | None = "UNKNOWN",
        runtime_id: str | None = "UNKNOWN",
        model_id: str | None = "UNKNOWN",
        contract_id: str = "P6.17_COGNITIVE_RESPONSE_V1",
        attempt: int = 1,
        auto_commit: bool = True,
        materialize_projection: bool = True,
    ) -> dict[str, Any]:
        with self._session() as session:
            head = repository.ensure_scope(session, scope_id)
            existing = repository.get_result_for_operation(session, operation_id, attempt)
            if existing is not None:
                commit = repository.get_commit_for_result(session, str(existing["id"]))
                projection = repository.get_projection_for_result(session, str(existing["id"]))
                session.commit()
                return {"result": existing, "commit": commit, "projection": projection}
            session.commit()
        input_version = int(expected_version if expected_version is not None else head["current_version"])
        manifest_value = {
            "scope_id": scope_id,
            "scope_version": input_version,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "specification": dict(specification),
            "context_receipt": dict(context_receipt),
            "contract_id": contract_id,
        }
        manifest_ref = self.object_store.put_json(manifest_value, object_kind="INPUT_MANIFEST")
        output_ref: ObjectRef | None = None
        parsed: dict[str, Any] | None = None
        status = "SUCCEEDED"
        failure: str | None = None
        started = time.perf_counter()
        raw_response: str | None = None
        try:
            raw_response = await provider.synthesize(manifest_value)
            output_ref = self.object_store.put_bytes(
                raw_response.encode("utf-8"),
                media_type="application/json",
                object_kind="RESULT_PAYLOAD",
            )
            parsed = parse_cognitive_response(raw_response)
            evidence_ids = [
                str(item.get("evidence_id"))
                for item in specification.get("evidence", [])
                if isinstance(item, Mapping) and item.get("evidence_id")
            ]
            validate_evidence_refs(parsed, evidence_ids)
        except CognitiveResponseError as exc:
            status = "INVALID_SCHEMA"
            failure = exc.code
        except Exception:  # noqa: BLE001 - provider failure is recorded as a FAILED receipt
            status = "FAILED"
            failure = "PROVIDER_FAILURE" if raw_response is None else "RESULT_VALIDATION_FAILURE"
        duration_ms = int((time.perf_counter() - started) * 1000)
        result_id = str(uuid4())
        with self._session() as session:
            manifest_row = repository.register_object(session, manifest_ref)
            if output_ref is not None:
                output_row = repository.register_object(session, output_ref)
                output_id = str(output_row["id"])
            else:
                output_id = None
            result = repository.create_result(
                session,
                result_id=result_id,
                operation_id=operation_id,
                attempt=attempt,
                operation_type=operation_type,
                run_id=run_id,
                correlation_id=correlation_id,
                scope_id=scope_id,
                input_scope_version=input_version,
                input_manifest_hash=manifest_ref.content_hash,
                input_manifest_object_id=str(manifest_row["id"]),
                context_fingerprint=str(context_receipt.get("fingerprint") or "") or None,
                context_receipt=context_receipt,
                provider_id=provider_id,
                runtime_id=runtime_id,
                model_id=model_id,
                contract_id=contract_id,
                determinism_class="NDETERMINISTIC",
                output_object_id=output_id,
                status=status,
                parsed_payload=parsed,
                failure_classification=failure,
                duration_ms=duration_ms,
            )
            session.commit()
        if status != "SUCCEEDED" or not auto_commit:
            return {"result": result, "commit": None, "projection": None}
        commit = self.commit_result(
            result_id=result_id,
            scope_id=scope_id,
            expected_version=input_version,
            candidate_ref=result_id,
            manifest_hash=manifest_ref.content_hash,
            request_id=run_id or operation_id,
            idempotency_key=operation_id,
            reason="validated result candidate",
        )
        projection = None
        if commit["status"] == "COMMITTED" and materialize_projection:
            projection = self.rebuild_projection(result_id=result_id, projection_kind="OUTPUT")
        return {"result": result, "commit": commit, "projection": projection}

    def commit_result(
        self,
        *,
        result_id: str,
        scope_id: str,
        expected_version: int,
        candidate_ref: str,
        manifest_hash: str,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        actor: str = "PADOS_STATE_PROOF_SERVICE",
        action: str = "SEMANTIC_COMMIT",
        reason: str = "validated result candidate",
        approval_ref: str | None = None,
        rollback_ref: str | None = None,
    ) -> dict[str, Any]:
        request_value = request_id or result_id
        idempotency_value = idempotency_key or result_id
        target = f"state_proof_result:{result_id}"
        with self._session() as session:
            prior_audit = repository.get_semantic_audit_by_key(
                session, scope_id=scope_id, idempotency_key=idempotency_value
            )
            if prior_audit is not None:
                expected_before = prior_audit["before_state"]
                if (
                    prior_audit["target"] != target
                    or prior_audit["actor"] != actor
                    or prior_audit["action"] != action
                    or prior_audit["reason"] != reason
                    or prior_audit["request_id"] != request_value
                    or prior_audit["approval_ref"] != approval_ref
                    or prior_audit["rollback_ref"] != rollback_ref
                    or int(expected_before.get("expected_scope_version", -1)) != expected_version
                ):
                    raise ValueError("AUDIT_IDEMPOTENCY_CONFLICT")
                existing_commit = repository.get_commit_for_result(session, result_id)
                if existing_commit is None:
                    raise ValueError("AUDIT_COMMIT_REFERENCE_MISSING")
                session.commit()
                return existing_commit

            result = repository.get_result(session, result_id)
            if result is None:
                raise ValueError("RESULT_NOT_FOUND")
            if result["status"] != "SUCCEEDED":
                raise ValueError("RESULT_NOT_COMMITTABLE")
            if str(result["scope_id"]) != scope_id or candidate_ref != result_id:
                raise ValueError("RESULT_SCOPE_OR_CANDIDATE_MISMATCH")
            if int(result["input_scope_version"]) != expected_version:
                raise ValueError("RESULT_SCOPE_VERSION_MISMATCH")
            if str(result["input_manifest_hash"]) != manifest_hash:
                raise ValueError("RESULT_MANIFEST_MISMATCH")
            before_head = repository.get_scope_head(session, scope_id)
            if before_head is None:
                raise ValueError("SCOPE_NOT_FOUND")
            commit = repository.commit_result(
                session,
                scope_id=scope_id,
                expected_version=expected_version,
                result_id=result_id,
                candidate_ref=candidate_ref,
                manifest_hash=manifest_hash,
            )
            after_head = repository.get_scope_head(session, scope_id)
            if after_head is None:
                raise ValueError("SCOPE_NOT_FOUND")
            previous_hash = repository.lock_semantic_audit_head(session, scope_id)
            record = build_audit_record(
                scope_id=scope_id,
                actor=actor,
                action=action,
                target=target,
                reason=reason,
                timestamp=datetime.now(UTC).isoformat(timespec="microseconds"),
                request_id=request_value,
                idempotency_key=idempotency_value,
                outcome=str(commit["status"]),
                before_state={
                    "expected_scope_version": expected_version,
                    "observed_scope_version": int(before_head["current_version"]),
                    "result_id": result_id,
                    "input_manifest_hash": manifest_hash,
                },
                after_state={
                    "scope_version": int(after_head["current_version"]),
                    "commit_id": str(commit["id"]),
                    "result_id": result_id,
                },
                approval_ref=approval_ref,
                rollback_ref=rollback_ref,
                previous_hash=previous_hash,
            )
            repository.append_semantic_audit(session, record)
            session.commit()
            return commit

    def record_semantic_rejection(
        self,
        *,
        scope_id: str,
        target: str,
        actor: str,
        reason: str,
        request_id: str,
        idempotency_key: str,
        approval_ref: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Append a governed rejection to the semantic audit chain; never moves ScopeVersion."""
        with self._session() as session:
            prior = repository.get_semantic_audit_by_key(
                session, scope_id=scope_id, idempotency_key=idempotency_key
            )
            if prior is not None:
                if (
                    prior["target"] != target
                    or prior["actor"] != actor
                    or prior["action"] != "SEMANTIC_REJECT"
                    or prior["approval_ref"] != approval_ref
                ):
                    raise ValueError("AUDIT_IDEMPOTENCY_CONFLICT")
                session.commit()
                return prior
            head = repository.get_scope_head(session, scope_id)
            observed = 0 if head is None else int(head["current_version"])
            previous_hash = repository.lock_semantic_audit_head(session, scope_id)
            record = build_audit_record(
                scope_id=scope_id,
                actor=actor,
                action="SEMANTIC_REJECT",
                target=target,
                reason=reason,
                timestamp=datetime.now(UTC).isoformat(timespec="microseconds"),
                request_id=request_id,
                idempotency_key=idempotency_key,
                outcome="REJECTED",
                before_state={"observed_scope_version": observed, **dict(evidence)},
                after_state={"scope_version": observed, "semantic_state": "UNCHANGED"},
                approval_ref=approval_ref,
                rollback_ref=None,
                previous_hash=previous_hash,
            )
            row = repository.append_semantic_audit(session, record)
            session.commit()
            return row

    def verify_semantic_audit_chain(self, scope_id: str) -> bool:
        with self._session() as session:
            records = repository.semantic_audit_records(session, scope_id)
        return verify_audit_chain(records)

    def rebuild_projection(self, *, result_id: str, projection_kind: str = "OUTPUT") -> dict[str, Any]:
        with self._session() as session:
            result = repository.get_result(session, result_id)
            if result is None:
                raise ValueError("RESULT_NOT_FOUND")
            commit = session.execute(
                text(
                    """
                    SELECT * FROM state_proof_commits
                    WHERE result_id = CAST(:result_id AS uuid) AND status = 'COMMITTED'
                    ORDER BY new_version DESC LIMIT 1
                    """
                ),
                {"result_id": result_id},
            ).mappings().first()
            if commit is None:
                raise ValueError("RESULT_NOT_COMMITTED")
            payload = result.get("parsed_payload_json") or {}
            title = str(payload.get("title") or "Cognitive result")
            content = str(payload.get("answer") or "")
            metadata = {
                "result_id": result_id,
                "result_content_hash": result.get("output_content_hash"),
                "scope_id": str(result["scope_id"]),
                "scope_version": int(commit["new_version"]),
                "context_fingerprint": result.get("context_fingerprint"),
                "candidate_memory": {
                    "authority_state": "CANDIDATE",
                    "result_id": result_id,
                    "promotion_contract": "P6.20B_REQUIRED",
                },
                "proposed_graph_changes": payload.get("proposed_graph_changes", []),
                "graph_authority": "PROPOSAL_ONLY",
            }
            if projection_kind == "LIVING_SYNTHESIS":
                metadata["living_synthesis"] = True
                metadata["synthesis_status"] = "PROVISIONAL"
            projection = repository.create_projection(
                session,
                scope_id=str(result["scope_id"]),
                scope_version=int(commit["new_version"]),
                result_id=result_id,
                projection_kind=projection_kind,
                title=title,
                content=content,
                metadata=metadata,
                contract_id="P6.19B_DERIVED_PROJECTION_V1",
            )
            session.commit()
            return projection

    def delete_projection(self, projection_id: str) -> None:
        with self._session() as session:
            repository.delete_projection(session, projection_id)
            session.commit()

    def get_result_for_operation(self, operation_id: str, attempt: int = 1) -> dict[str, Any] | None:
        with self._session() as session:
            return repository.get_result_for_operation(session, operation_id, attempt)

    def get_result(self, result_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            return repository.get_result(session, result_id)

    def get_projection(self, projection_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            return repository.get_projection(session, projection_id)


def build_proof_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})


def read_scope_version(engine: Engine, scope_id: str) -> int | None:
    """Read the authoritative ScopeVersion without creating or changing a scope."""
    with Session(engine) as session:
        head = repository.get_scope_head(session, scope_id)
    return None if head is None else int(head["current_version"])
