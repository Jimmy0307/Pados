# Contract: `pados.product_loop.state`

## Purpose

Durable state gateway for the product loop: the `ProductLoopService` state substrate. It persists run context, read snapshots and reports as immutable content-addressed objects, seals attempts as ResultReceipts, and performs the semantic CAS commit, all through the PADOS proof service (`pados.state_proof`: `StateProofService`, `repository`, `ObjectStore`). **Execution state never replaces semantic authority**: only `commit` can advance a ScopeVersion, and only through `StateProofService.commit_result`.

## Public API (compatibility)

- `provider_identity(runtime_result: RuntimeResult) -> str`: the `provider_id` string from `runtime_result.execution_receipt` if it is a non-empty string, else `"UNVERIFIED_PROVIDER"`.
- `class ReceiptGateway(Protocol)` with:
  - `semantic_context(scope_id) -> dict`
  - `async persist_run_context(payload) -> dict`
  - `async persist_read_snapshot(*, run_id, payload) -> dict`
  - `async record_attempt(*, run_id, attempt, contract, runtime_result) -> ReceiptRecord`
  - `async commit(*, receipt, contract) -> dict`
  - `async rebuild_projection(*, receipt, contract) -> dict`
  - `async persist_report(*, run_id, payload) -> dict`
  - `read_run(run_id) -> DurableProductRead | None`
  - `read_run_scope(run_id) -> str | None`
- `class P6_19BStateGateway(proof_service: StateProofService, operational_engine: Engine | None = None)`: implements the protocol, plus `resolve_project_context(project_id) -> dict` and `verify_approval(approval_ref, scope_id) -> None`. It exposes attributes `proof_service` and `operational_engine`.
- `class InMemoryP6_19BStateGateway(object_root: Path)`: a deterministic test seam implementing the protocol. Public attributes used by tests: `object_store` (an `ObjectStore`), `versions: dict[scope_id, int]`, `receipts`, `commits: dict[receipt_id, dict]`, `projections`, `read_index: dict[run_id, dict]` (each entry has `context_ref`, `scope_id`, `idempotency_key`, and optionally `snapshot_ref` and `report_ref`).

## Object reference payload (returned by all `persist_*`)

`{"object_id", "content_hash", "size_bytes", "media_type", "storage_uri", "object_kind"}`. `object_id` is the registered database row id when one exists, otherwise the ObjectRef id. JSON objects are written with `ObjectStore.put_json(payload, object_kind="OTHER")`.

## `P6_19BStateGateway` behaviour

- **Sessions.** The proof lane uses `proof_service._session()`. The operational lane uses `Session(operational_engine)`; when that is `None`, the proof session is used (single-database fixtures).
- **`semantic_context(scope)`.** Returns `{"scope_version": current_version}` from `repository.get_scope_head`, or 0 when there is no head.
- **`resolve_project_context(project_id)`.** Delegates to `pados.execution.semantic_context.resolve_database_semantic_context(operational_session, project_id, statement_factory=sqlalchemy.text, proof_session=proof_session)`.
- **`verify_approval(ref, scope)`.** Raises `ProductAuthorizationError` with one of these codes:
  - `APPROVAL_REF_INVALID`: `ref` is not a UUID;
  - `APPROVAL_REF_NOT_BOUND_TO_SCOPE`: no row in `control_approvals` with that id whose `project_id` equals `scope`;
  - `APPROVAL_REF_NOT_APPROVED`: the status is not `APPROVED`.
- **`persist_run_context(payload)`.** Put the object, `repository.register_object`, then `repository.register_product_read_index(run_id, scope_id, idempotency_key, context_object_id, context_hash)` from the payload, and commit.
- **`persist_read_snapshot` / `persist_report`.** Put the object, register it, and `repository.update_product_read_index` with `latest_snapshot_object_id`/`latest_snapshot_hash`, or `final_report_object_id`/`final_report_hash` respectively.
- **`record_attempt`.** Call `await proof_service.execute(...)` with:
  - `scope_id = contract.intent.scope_id`, `operation_id = f"{contract.idempotency_key}:{attempt}"`;
  - `specification = {goal: parent_goal, intent, work_contract, runtime_result (JSON dumps), evidence: [{"evidence_id": ref} for evidence_refs]}`;
  - `context_receipt = {fingerprint: context_fingerprint, context_ref, scope_id: scope.workspace_root}`;
  - a provider object whose `async synthesize(specification) -> str` returns the JSON synthesis document below;
  - `operation_type="DIRECT_MAKER"`, `expected_version = semantic_base_scope_version`;
  - `run_id`, `correlation_id`, `provider_id = provider_identity(runtime_result)`, `runtime_id`, `model_id="NONE"`, `contract_id`, `attempt`;
  - `auto_commit=False`, `materialize_projection=False`.

  It returns `ReceiptRecord` from the result row:
  - `receipt_id = str(id)`, `status`, `input_scope_version`, `input_manifest_hash`;
  - `output_content_hash`, or `None` when the row has none;
  - `runtime_status` and `failure_classification` from the runtime result.
- **Synthesis document (persisted format).** A JSON object with keys:
  - `title`: a short string;
  - `answer`: **the runtime result's JSON (`model_dump(mode="json")`) serialized with sorted keys, as a string**. The read model parses `answer` back into a `RuntimeResult`;
  - `subquestions`, `gaps`, `claims`, `evidence_refs`, `proposed_graph_changes`: empty lists;
  - `limitations`: `["RUNTIME_RESULT_IS_NOT_SEMANTIC_ACCEPTANCE"]`.
- **`commit(receipt, contract)`.** Call `proof_service.commit_result` with:
  - `result_id = receipt_id`, `scope_id = intent.scope_id`, `expected_version = receipt.input_scope_version`;
  - `candidate_ref = receipt_id`, `manifest_hash = input_manifest_hash`;
  - `request_id = correlation_id`, `idempotency_key`;
  - `actor="PADOS_PRODUCT_LOOP"`, a short reason stating that the receipt was verified and validation passed;
  - `approval_ref`, `rollback_ref` from the contract.

  It returns the service result unchanged.
- **`rebuild_projection`.** `proof_service.rebuild_projection(result_id=receipt_id, projection_kind="OUTPUT")`.
- **`read_run(run_id)`.** `pados.product_loop.read_model.ProductLoopReadModel(proof_service).get_run(run_id)`.
- **`read_run_scope(run_id)`.** The `scope_id` string from `repository.get_product_read_index`, or `None`.

## `InMemoryP6_19BStateGateway` behaviour

- **`semantic_context`.** Returns `{"scope_version": versions.get(scope, 0)}`.
- **`record_attempt`.** Idempotent per `f"{idempotency_key}:{attempt}"`: a repeat returns the same record.
  - The scope version defaults to 0.
  - It stores an `INPUT_MANIFEST` JSON object (scope, base version, operation id, contract, runtime result) and a `RESULT_PAYLOAD` object (runtime result).
  - It returns `ReceiptRecord(status="SUCCEEDED", input_scope_version=base version, input_manifest_hash=manifest hash, output_content_hash=payload hash, runtime_status, failure_classification)` with a fresh unique `receipt_id`.
- **`commit`.** Idempotent per receipt.
  - If the current version equals `receipt.input_scope_version`, the version increments and it returns `{"status": "COMMITTED", "result_id", "expected_parent_version": current, "new_version": current + 1}`.
  - Otherwise it returns `{"status": "STALE_CANDIDATE", "result_id", "expected_parent_version": receipt.input_scope_version, "new_version": None}` and the version is unchanged.
- **`rebuild_projection`.** Idempotent. It requires a `COMMITTED` commit, else `ValueError("RESULT_NOT_COMMITTED")`. It returns `{"id", "result_id", "projection_kind": "OUTPUT", "scope_version": new_version, "candidate_memory": {"authority_state": "CANDIDATE"}, "graph_authority": "PROPOSAL_ONLY", "contract_id"}`.
- **`persist_*`.** Put a JSON object (`OTHER`) and record it under `read_index[run_id]`: `context_ref` with `scope_id` and `idempotency_key` for the run context, `snapshot_ref`, or `report_ref`.
- **`read_run(run_id)`.** `None` when unknown. If a report exists, return `read_model.read_from_report(ProductLoopReport(report JSON), started_at=context["started_at"], report_object_ref=<ref payload>)`. Else, if a snapshot exists, return `DurableProductRead(snapshot JSON)`. Else return `read_model.read_from_context(context JSON)`.
- **`read_run_scope`.** The stored `scope_id`, or `None`.

**Tests:** `tests/unit/test_consolidation_product_loop.py`, `tests/unit/test_p6_21g_product_loop.py`, `tests/security/test_p6_21m_product_auth.py`, `tests/integration/test_p6_21g_state_integration.py`, `tests/integration/test_p6_21h_durable_read_model.py`, `tests/integration/test_final_closure_approval_binding.py`, `tests/integration/test_final_closure_semantic_authority.py`, `tests/unit/test_final_closure_provider_identity.py`, and the enterprise and compute-node product-loop tests.
