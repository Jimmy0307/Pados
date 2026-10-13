# SPDX-License-Identifier: AGPL-3.0-only
"""State gateways that give the product loop its durable substrate.

A gateway stores run context, read snapshots and final reports as immutable
content-addressed objects, seals every execution attempt as a result receipt,
and performs the semantic compare-and-set commit. Execution state never stands
in for semantic authority: a scope version only advances through ``commit``,
which in the database-backed gateway goes through the proof service's
``commit_result``.

``P6_19BStateGateway`` is the database-backed implementation.
``InMemoryP6_19BStateGateway`` is a deterministic, file-backed test seam with
the same observable contract.
"""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pados.execution.semantic_context import resolve_database_semantic_context
from pados.product_loop import read_model
from pados.product_loop.authorization import ProductAuthorizationError
from pados.product_loop.schemas import (
    DurableProductRead,
    ProductLoopReport,
    ReceiptRecord,
    RuntimeResult,
    WorkContract,
)
from pados.state_proof import repository
from pados.state_proof.object_store import ObjectRef, ObjectStore
from pados.state_proof.service import StateProofService

UNVERIFIED_PROVIDER = "UNVERIFIED_PROVIDER"
LOOP_ACTOR = "PADOS_PRODUCT_LOOP"
_COMMIT_REASON = "execution receipt verified and validation passed"
_SYNTHESIS_TITLE = "Product loop attempt result"
_SYNTHESIS_LIMITATION = "RUNTIME_RESULT_IS_NOT_SEMANTIC_ACCEPTANCE"


def provider_identity(runtime_result: RuntimeResult) -> str:
    """Name the provider recorded in the execution receipt, or mark it unverified."""
    receipt = runtime_result.execution_receipt
    provider = receipt.get("provider_id") if isinstance(receipt, Mapping) else None
    return provider if isinstance(provider, str) and provider else UNVERIFIED_PROVIDER


class ReceiptGateway(Protocol):
    def semantic_context(self, scope_id: str) -> dict[str, Any]: ...

    async def persist_run_context(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    async def persist_read_snapshot(
        self, *, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def record_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        contract: WorkContract,
        runtime_result: RuntimeResult,
    ) -> ReceiptRecord: ...

    async def commit(self, *, receipt: ReceiptRecord, contract: WorkContract) -> dict[str, Any]: ...

    async def rebuild_projection(
        self, *, receipt: ReceiptRecord, contract: WorkContract
    ) -> dict[str, Any]: ...

    async def persist_report(self, *, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...

    def read_run(self, run_id: str) -> DurableProductRead | None: ...

    def read_run_scope(self, run_id: str) -> str | None: ...


def _ref_payload(ref: ObjectRef, object_id: str | None = None) -> dict[str, Any]:
    return {
        "object_id": object_id if object_id is not None else ref.object_id,
        "content_hash": ref.content_hash,
        "size_bytes": ref.size_bytes,
        "media_type": ref.media_type,
        "storage_uri": ref.storage_uri,
        "object_kind": ref.object_kind,
    }


def _operation_id(contract: WorkContract, attempt: int) -> str:
    return f"{contract.idempotency_key}:{attempt}"


def _synthesis_document(runtime_result: RuntimeResult) -> str:
    """The persisted result document; ``answer`` carries the runtime result JSON."""
    answer = json.dumps(runtime_result.model_dump(mode="json"), sort_keys=True)
    return json.dumps(
        {
            "title": _SYNTHESIS_TITLE,
            "answer": answer,
            "subquestions": [],
            "gaps": [],
            "claims": [],
            "evidence_refs": [],
            "proposed_graph_changes": [],
            "limitations": [_SYNTHESIS_LIMITATION],
        }
    )


class _RuntimeResultProvider:
    """Proof provider that records an already-produced runtime result verbatim."""

    def __init__(self, runtime_result: RuntimeResult) -> None:
        self._document = _synthesis_document(runtime_result)

    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        del specification
        return self._document


class P6_19BStateGateway:
    """Database-backed gateway over the proof service and its object store."""

    def __init__(
        self, proof_service: StateProofService, operational_engine: Engine | None = None
    ) -> None:
        self.proof_service = proof_service
        self.operational_engine = operational_engine

    @contextmanager
    def _operational_session(self) -> Iterator[Session]:
        if self.operational_engine is None:
            with self.proof_service._session() as session:
                yield session
        else:
            with Session(self.operational_engine) as session:
                yield session

    def semantic_context(self, scope_id: str) -> dict[str, Any]:
        with self.proof_service._session() as session:
            head = repository.get_scope_head(session, scope_id)
        return {"scope_version": int(head["current_version"]) if head is not None else 0}

    def resolve_project_context(self, project_id: str) -> dict[str, Any]:
        with self.proof_service._session() as proof_session:
            if self.operational_engine is None:
                return resolve_database_semantic_context(
                    proof_session, project_id, statement_factory=text, proof_session=proof_session
                )
            with Session(self.operational_engine) as operational_session:
                return resolve_database_semantic_context(
                    operational_session,
                    project_id,
                    statement_factory=text,
                    proof_session=proof_session,
                )

    def verify_approval(self, approval_ref: str, scope_id: str) -> None:
        try:
            UUID(str(approval_ref))
        except ValueError as exc:
            raise ProductAuthorizationError("APPROVAL_REF_INVALID") from exc
        with self._operational_session() as session:
            row = session.execute(
                text(
                    "SELECT project_id::text AS project_id, status FROM control_approvals "
                    "WHERE id = CAST(:approval_id AS uuid)"
                ),
                {"approval_id": str(approval_ref)},
            ).mappings().first()
        if row is None or str(row["project_id"]) != scope_id:
            raise ProductAuthorizationError("APPROVAL_REF_NOT_BOUND_TO_SCOPE")
        if row["status"] != "APPROVED":
            raise ProductAuthorizationError("APPROVAL_REF_NOT_APPROVED")

    async def persist_run_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ref = self.proof_service.object_store.put_json(dict(payload), object_kind="OTHER")
        with self.proof_service._session() as session:
            row = repository.register_object(session, ref)
            repository.register_product_read_index(
                session,
                run_id=str(payload["run_id"]),
                scope_id=str(payload["scope_id"]),
                idempotency_key=str(payload["idempotency_key"]),
                context_object_id=str(row["id"]),
                context_hash=ref.content_hash,
            )
            session.commit()
        return _ref_payload(ref, str(row["id"]))

    async def persist_read_snapshot(
        self, *, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        ref = self.proof_service.object_store.put_json(dict(payload), object_kind="OTHER")
        with self.proof_service._session() as session:
            row = repository.register_object(session, ref)
            repository.update_product_read_index(
                session,
                run_id=run_id,
                latest_snapshot_object_id=str(row["id"]),
                latest_snapshot_hash=ref.content_hash,
            )
            session.commit()
        return _ref_payload(ref, str(row["id"]))

    async def persist_report(self, *, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        ref = self.proof_service.object_store.put_json(dict(payload), object_kind="OTHER")
        with self.proof_service._session() as session:
            row = repository.register_object(session, ref)
            repository.update_product_read_index(
                session,
                run_id=run_id,
                final_report_object_id=str(row["id"]),
                final_report_hash=ref.content_hash,
            )
            session.commit()
        return _ref_payload(ref, str(row["id"]))

    async def record_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        contract: WorkContract,
        runtime_result: RuntimeResult,
    ) -> ReceiptRecord:
        specification = {
            "goal": contract.parent_goal,
            "intent": contract.intent.model_dump(mode="json"),
            "work_contract": contract.model_dump(mode="json"),
            "runtime_result": runtime_result.model_dump(mode="json"),
            "evidence": [{"evidence_id": ref} for ref in contract.evidence_refs],
        }
        context_receipt = {
            "fingerprint": contract.context_fingerprint,
            "context_ref": contract.context_ref,
            "scope_id": contract.scope.workspace_root,
        }
        outcome = await self.proof_service.execute(
            scope_id=contract.intent.scope_id,
            operation_id=_operation_id(contract, attempt),
            specification=specification,
            context_receipt=context_receipt,
            provider=_RuntimeResultProvider(runtime_result),
            operation_type="DIRECT_MAKER",
            expected_version=contract.semantic_base_scope_version,
            run_id=run_id,
            correlation_id=contract.correlation_id,
            provider_id=provider_identity(runtime_result),
            runtime_id=runtime_result.runtime_id,
            model_id="NONE",
            contract_id=contract.work_contract_id,
            attempt=attempt,
            auto_commit=False,
            materialize_projection=False,
        )
        row = outcome["result"]
        output_hash = row.get("output_content_hash")
        return ReceiptRecord(
            receipt_id=str(row["id"]),
            status=str(row["status"]),
            runtime_status=runtime_result.status,
            failure_classification=runtime_result.failure_classification,
            input_scope_version=int(row["input_scope_version"]),
            input_manifest_hash=str(row["input_manifest_hash"]),
            output_content_hash=str(output_hash) if output_hash else None,
        )

    async def commit(self, *, receipt: ReceiptRecord, contract: WorkContract) -> dict[str, Any]:
        return self.proof_service.commit_result(
            result_id=receipt.receipt_id,
            scope_id=contract.intent.scope_id,
            expected_version=receipt.input_scope_version,
            candidate_ref=receipt.receipt_id,
            manifest_hash=receipt.input_manifest_hash,
            request_id=contract.correlation_id,
            idempotency_key=contract.idempotency_key,
            actor=LOOP_ACTOR,
            reason=_COMMIT_REASON,
            approval_ref=contract.approval_ref,
            rollback_ref=contract.rollback_ref,
        )

    async def rebuild_projection(
        self, *, receipt: ReceiptRecord, contract: WorkContract
    ) -> dict[str, Any]:
        del contract
        return self.proof_service.rebuild_projection(
            result_id=receipt.receipt_id, projection_kind="OUTPUT"
        )

    def read_run(self, run_id: str) -> DurableProductRead | None:
        return read_model.ProductLoopReadModel(self.proof_service).get_run(run_id)

    def read_run_scope(self, run_id: str) -> str | None:
        with self.proof_service._session() as session:
            index = repository.get_product_read_index(session, run_id)
        scope = index.get("scope_id") if index is not None else None
        return scope if isinstance(scope, str) else None


class InMemoryP6_19BStateGateway:
    """Deterministic gateway for tests: objects on disk, state in dictionaries."""

    def __init__(self, object_root: Path) -> None:
        self.object_store = ObjectStore(object_root)
        self.versions: dict[str, int] = {}
        self.receipts: dict[str, ReceiptRecord] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.projections: dict[str, dict[str, Any]] = {}
        self.read_index: dict[str, dict[str, Any]] = {}

    def semantic_context(self, scope_id: str) -> dict[str, Any]:
        return {"scope_version": self.versions.get(scope_id, 0)}

    async def persist_run_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ref = self.object_store.put_json(dict(payload), object_kind="OTHER")
        entry = self.read_index.setdefault(str(payload["run_id"]), {})
        entry.update(
            context_ref=ref,
            scope_id=str(payload["scope_id"]),
            idempotency_key=str(payload["idempotency_key"]),
        )
        return _ref_payload(ref)

    async def persist_read_snapshot(
        self, *, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        ref = self.object_store.put_json(dict(payload), object_kind="OTHER")
        self.read_index.setdefault(run_id, {})["snapshot_ref"] = ref
        return _ref_payload(ref)

    async def persist_report(self, *, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        ref = self.object_store.put_json(dict(payload), object_kind="OTHER")
        self.read_index.setdefault(run_id, {})["report_ref"] = ref
        return _ref_payload(ref)

    async def record_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        contract: WorkContract,
        runtime_result: RuntimeResult,
    ) -> ReceiptRecord:
        del run_id
        operation_id = _operation_id(contract, attempt)
        existing = self.receipts.get(operation_id)
        if existing is not None:
            return existing
        scope_id = contract.intent.scope_id
        base_version = contract.semantic_base_scope_version
        runtime_json = runtime_result.model_dump(mode="json")
        manifest = self.object_store.put_json(
            {
                "scope_id": scope_id,
                "scope_version": base_version,
                "operation_id": operation_id,
                "contract": contract.model_dump(mode="json"),
                "runtime_result": runtime_json,
            },
            object_kind="INPUT_MANIFEST",
        )
        output = self.object_store.put_json(runtime_json, object_kind="RESULT_PAYLOAD")
        record = ReceiptRecord(
            receipt_id=str(uuid4()),
            status="SUCCEEDED",
            runtime_status=runtime_result.status,
            failure_classification=runtime_result.failure_classification,
            input_scope_version=base_version,
            input_manifest_hash=manifest.content_hash,
            output_content_hash=output.content_hash,
        )
        self.receipts[operation_id] = record
        return record

    async def commit(self, *, receipt: ReceiptRecord, contract: WorkContract) -> dict[str, Any]:
        existing = self.commits.get(receipt.receipt_id)
        if existing is not None:
            return existing
        scope_id = contract.intent.scope_id
        current = self.versions.get(scope_id, 0)
        if current == receipt.input_scope_version:
            self.versions[scope_id] = current + 1
            result: dict[str, Any] = {
                "status": "COMMITTED",
                "result_id": receipt.receipt_id,
                "expected_parent_version": current,
                "new_version": current + 1,
            }
        else:
            result = {
                "status": "STALE_CANDIDATE",
                "result_id": receipt.receipt_id,
                "expected_parent_version": receipt.input_scope_version,
                "new_version": None,
            }
        self.commits[receipt.receipt_id] = result
        return result

    async def rebuild_projection(
        self, *, receipt: ReceiptRecord, contract: WorkContract
    ) -> dict[str, Any]:
        existing = self.projections.get(receipt.receipt_id)
        if existing is not None:
            return existing
        commit = self.commits.get(receipt.receipt_id)
        if commit is None or commit.get("status") != "COMMITTED":
            raise ValueError("RESULT_NOT_COMMITTED")
        projection = {
            "id": str(uuid4()),
            "result_id": receipt.receipt_id,
            "projection_kind": "OUTPUT",
            "scope_version": commit["new_version"],
            "candidate_memory": {"authority_state": "CANDIDATE"},
            "graph_authority": "PROPOSAL_ONLY",
            "contract_id": contract.work_contract_id,
        }
        self.projections[receipt.receipt_id] = projection
        return projection

    def _load(self, ref: ObjectRef) -> dict[str, Any]:
        value = json.loads(self.object_store.read(ref).decode("utf-8"))
        if not isinstance(value, dict):
            raise TypeError("READ_OBJECT_NOT_OBJECT")
        return value

    def read_run(self, run_id: str) -> DurableProductRead | None:
        entry = self.read_index.get(run_id)
        if entry is None or "context_ref" not in entry:
            return None
        context = self._load(entry["context_ref"])
        report_ref = entry.get("report_ref")
        if report_ref is not None:
            return read_model.read_from_report(
                ProductLoopReport.model_validate(self._load(report_ref)),
                started_at=str(context["started_at"]),
                report_object_ref=_ref_payload(report_ref),
            )
        snapshot_ref = entry.get("snapshot_ref")
        if snapshot_ref is not None:
            return DurableProductRead.model_validate(self._load(snapshot_ref))
        return read_model.read_from_context(context)

    def read_run_scope(self, run_id: str) -> str | None:
        entry = self.read_index.get(run_id)
        scope = entry.get("scope_id") if entry is not None else None
        return scope if isinstance(scope, str) else None
