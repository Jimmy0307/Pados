# Recovery runbooks (OP-005)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §7–§8. These runbooks
apply to the local, supervised, single-operator dogfood. They are not a production on-call procedure.

Every entry follows **symptom → code → where to look → safe recovery → verification**. "Safe recovery" states whether a
retry, a reconciliation, a human approval, a rollback or a forward fix is the right move. **No recovery step bypasses an
approval, a receipt check or the CAS.** If a step would need that, stop and ask the owner.

Where to look, for every component:
- the `X-Request-ID` of the failing response (or `error.request_id`);
- the structured log lines that carry it (`http.request.finished` plus the component's own records);
- the durable rows that carry `payload_json.trace.request_id` (see [`CONTEXT_AND_ERROR_RUNBOOK.md`](CONTEXT_AND_ERROR_RUNBOOK.md));
- `GET /health/ready` (see [`HEALTH.md`](HEALTH.md)).

Deterministic injection of these failures is in `tests/operability/test_op005_failure_injection.py`
(`@pytest.mark.failure_injection`) and the tests named per row.

## Core API

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| Requests answer 503 | `DATABASE_UNAVAILABLE` (`DEPENDENCY`) | Restore PostgreSQL first. A GET may be retried (`retryable: true`). For other methods `write_occurred` is `UNKNOWN`: read the record's state before re-sending, and re-send with the same idempotency key | `/health/ready` reports `operational_database: OK` |
| 500 on a route | `INTERNAL_ERROR` | Not retryable. The log has the exception type for that `request_id`, never its text. Forward-fix the defect | The same request succeeds after the fix |
| 409 on a create | `*_IDEMPOTENCY_CONTENT_CONFLICT` | The key was already used for different content: reconcile and use a new key for new content | A `GET` shows exactly one record per key |

Injected by: `test_database_disconnect_is_a_dependency_failure_never_an_internal_error`, and
`tests/integration/test_op004_idempotency_postgres.py`.

## TaskLoop

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| A step is refused | `LOOP_LEASE_HELD` | Another owner holds a live lease (an owner-less step acts as the loop's starting owner and is fenced like any other). Wait, or let it lapse (≤ 5 min) when that owner has crashed, then resume | `task_loop.lease.held` for the new owner; iterations continue from the persisted ones |
| A model job is `ROUTED` again right after its call | `LOOP_LEASE_LOST` (`JOB_QUEUED` event, `result_discarded`) | The lease was taken over while the model ran; the output was discarded, not recorded. Step again as the current holder, which runs the job again | `worker.model_job.fenced` in the log; no `stdout` artifact from the discarded call |
| A step answers `WAITING_HUMAN` | approval reasons (`APPROVAL_REVOKED`, `APPROVAL_EXPIRED`, `APPROVAL_CONSUMED`) | Human approval: create and decide a new approval and start a new loop. Never edit the approval row | The new loop routes under the new approval |
| A job stays non-terminal past its deadline | `JOB_DEADLINE_EXCEEDED` (`TIMED_OUT`) | The next step expires it automatically; then step again | `worker_jobs.status = TIMED_OUT`, and the loop decides on it |

Injected by: `tests/integration/test_op004_lease_postgres.py`, `tests/integration/test_op004_lease_model_takeover_postgres.py`, `tests/integration/test_sec4_approval_lifecycle_postgres.py`,
`tests/integration/test_sec9_model_call_outside_transaction_postgres.py`, and
`test_an_expired_parent_deadline_refuses_a_node_call_without_sending_it`.

## Workers and providers

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| Model job `TIMED_OUT` | `TIMED_OUT` / `PARENT_DEADLINE_EXCEEDED` | No partial output is kept as evidence. Retry the step (a new iteration), or raise `X-Request-Deadline-Ms` | The job is `SUCCEEDED` with one stdout artifact |
| Model job `FAILED` | `OUTPUT_CONTRACT_FAILED`, `MODEL_PROVIDER_FAILED`, measured-binding codes | Not retryable as-is. Check the provider and the measured binding (`pados doctor`), then step again. Model output is never acceptance | `worker.model_job.finished` with `job_status=SUCCEEDED` |
| Route refused | `ROUTE_HAS_NO_EXECUTOR`, `APPROVAL_*` | Fix the plan or the approval; nothing ran | The `ROUTE_REFUSED` audit row explains the refusal |

Injected by: the three `worker_provider` cases in the OP-005 test file,
`tests/integration/test_rc2_exec1_no_executor_route_postgres.py`, and
`tests/integration/test_sec9_model_call_outside_transaction_postgres.py`.

## Remote operations

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| 401 on `/remote/v1` | `REMOTE_AUTH_REQUIRED`, `REMOTE_AUTH_INVALID`, `SESSION_UNAUTHENTICATED` | Re-authenticate. A standing session never decides | A new session lists only the actor's projects |
| A decision is refused | `APPROVAL_STEP_UP_PROOF_REQUIRED`, `STEP_UP_PROOF_INVALID` | A fresh WebAuthn step-up is required; a proof is single-use and is never replayed | The decision commits once through the CAS |
| `/remote/v1` answers 503 | `REMOTE_LISTENER_*` | The listener is not on its configured private address. Fix the bind; do not widen it | `remote: ENABLED` |

Injected by: `tests/remote/` (step-up, replay and listener tests) and the local dogfood E2E steps
`standing_session_cannot_decide` and `consumed_proof_cannot_be_replayed`.

## Compute node

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| Node unreachable | `NODE_OFFLINE`, `NODE_TIMEOUT` | Status reads retry once inside the budget. Restart the node; execution is never retried automatically | `pados node health` is `ONLINE` |
| Node answer refused | `NODE_SCHEMA_INVALID`, `NODE_RECEIPT_INVALID`, `ARTIFACT_HASH_MISMATCH` | Treat it as untrusted. No output is accepted; investigate the node | A new execution verifies its record MAC and artifact hash |
| Call refused before sending | `NODE_DEADLINE_EXCEEDED` | The caller's budget was spent; nothing was sent | Retry with a larger budget |

Injected by: the `compute_node` cases in the OP-005 test file and `tests/compute_node/`.

## Corpus

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| Ingest failed | `INGEST_FAILED` | Use the batch's own retry action (`POST /api/corpus/batches/{id}/retry`); it is state-based and reconciles first | The batch reaches its published state |
| Not authorized | `BATCH_NOT_AUTHORIZED`, `BATCH_AUTHORIZATION_EXPIRED` | Human approval: authorize the batch again | The batch shows a current authorization |
| Corpus lane down | `CORPUS_UNAVAILABLE`, `CORPUS_SCHEMA_INCOMPLETE` | Restore or migrate the corpus database lane (`pados migrate`) | The corpus endpoints answer 200 |
| `/health/ready` is `DEGRADED` with `corpus_database` not OK | `CORPUS_DB_UNAVAILABLE`, `CORPUS_MIGRATION_STALE` (`CORPUS_DB_UNCONFIGURED` is `NOT_CONFIGURED`) | The core loop keeps working; only retrieval and ingestion are affected. Start the database or run `pados migrate` for the corpus lane | `corpus_database` reads `OK` and readiness returns to `OK` |

Injected by: `tests/unit/test_p6_15_corpus.py`, `tests/unit/test_p6_15_corpus_runtime.py` and the web corpus tests. These
need the corpus database lane; locally they fall into the documented environment baseline (R19-3).

## Evidence, receipts and CAS

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| A receipt does not verify | `verify_execution_receipt` is false, `PERSISTED_EXECUTION_RECEIPT_MISSING`, `PERSISTED_CAPABILITY_RESULT_HASH_MISMATCH` | Never accept. Re-run the step to produce new evidence. Altered evidence is a security event: keep it, do not repair it | The new receipt verifies; acceptance re-checks it |
| Acceptance refused | `RESULT_ACCEPTANCE_CANDIDATE_CHANGED`, `RESULT_ACCEPTANCE_WINDOW_EXPIRED`, `STALE_CANDIDATE`, `RESULT_CANDIDATE_ALREADY_DECIDED` | Reconcile: compose a new candidate against the current ScopeVersion and obtain a new human approval. Never force the CAS | ScopeVersion advances exactly once |
| Ledger refused | `EXECUTION_LEDGER_CORRUPT`, `EXECUTION_LEDGER_CONFLICT`, `EXECUTION_LEDGER_FULL`, `AUDIT_CHAIN_HEAD_CHANGED` | Stop and preserve the files. Rollback is not available; recovery is a forward-fix by the owner | The ledger reader verifies the chain |

Injected by: `test_partial_or_altered_evidence_never_verifies`, `tests/capability/test_execution_ledger.py`, and the
governed acceptance suite in `tests/integration/test_final_convergence_product_loop_postgres.py`.

## Product Loop

Disabled by default (`PADOS_PRODUCT_LOOP_ENABLED=false`). While it is disabled no Product Loop route is registered
and nothing below can happen. Its external runtimes (REA, OpenMAIC, AI-Scientist-v3) and governed model-output
acceptance are `NOT_VALIDATED` runtime lanes, so this section covers the refusals the code makes, not a validated
production path.

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| The loop is enabled but its routes are absent | `PRODUCT_WORKSPACE_ROOT_REQUIRED`, `PRODUCT_OBJECT_ROOT_REQUIRED`, `PRODUCT_DATABASE_NAME_REQUIRED`, `PROOF_DATABASE_MUST_BE_SEPARATE`, `PRODUCT_DATABASE_MUST_BE_LOCAL` | Nothing was written. Set `PADOS_PRODUCT_WORKSPACE_ROOT`, `PADOS_PRODUCT_OBJECT_ROOT` and a separate local `PADOS_PRODUCT_DATABASE_NAME`, then restart | `/health/ready`: `product_loop_configuration` and `proof_database` read `OK` (required only while the loop is enabled) |
| `POST /product-loop/runs` answers 403 | `APPROVAL_SUBJECT_MISMATCH`, `APPROVAL_REF_NOT_BOUND_TO_SCOPE`, `APPROVAL_REF_NOT_APPROVED`, `APPROVAL_CONSUMED`, `APPROVAL_VERIFICATION_UNAVAILABLE` | Refused before any run exists. Obtain a new human approval for exactly this operation; an approval is never edited or reused | The new run appears in `GET /product-loop/runs/{run_id}` |
| A run ends `AUTHORITY_BLOCK` before the cloud call | `CLOUD_EGRESS_CLASSIFICATION_REFUSED`, `CLOUD_EGRESS_CEILING_INVALID`, `CLOUD_EGRESS_CLASSIFICATION_UNAVAILABLE` | The project's classification is above the cloud ceiling, or the ceiling is invalid (RESTRICTED is never valid). Raising `PADOS_PRODUCT_LOOP_CLOUD_MAX_CLASSIFICATION` is an owner decision, not a recovery step. No reclassify command exists | The run status in `GET /product-loop/runs/{run_id}` |
| The provider is refused | `PRODUCT_LOOP_PROVIDER_INVALID`, `PRODUCT_LOOP_PROVIDER_UNAVAILABLE`, `CODEX_EXECUTABLE_UNAVAILABLE` | Only the Codex CLI has an adapter. Set `PADOS_PRODUCT_LOOP_PROVIDER` to `AUTO` or `CODEX` and install the CLI | `provider_routing` in `/health/ready` |
| Provider output is refused | `CODEX_OUTPUT_PATH_SCOPE_MISMATCH`, `CODEX_OUTPUT_BUDGET_EXCEEDED`, `CODEX_OUTPUT_SECRET_SHAPED`, `CODEX_OUTPUT_NOT_UTF8` | Fails closed: nothing reaches the workspace. Correct the declared paths or the request and submit a new run | The new run's status |

Injected by: `tests/security/test_rc2_arch1_product_dispatch_authority.py`,
`tests/security/test_rc2_sec2_cloud_egress_classification.py`.

## Learning loop and anchors

The outcome store, advice log and anchors are a library (`src/pados/learning/`). The advisor is off by default, and no
CLI or API drives it today, so every recovery below is an owner action in Python, not an operator command.
`ADAPTER_UNAVAILABLE` and `ADAPTER_FAILURE` are outcome classifications that the store records, not refusals.

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| An append is refused because a store is full | `OUTCOME_STORE_FULL_PRUNE_REQUIRED`, `OUTCOME_INGESTION_LEDGER_FULL_PRUNE_REQUIRED`, `ADVICE_LOG_FULL_PRUNE_REQUIRED`, `EVALUATION_STORE_FULL` | Nothing was evicted. Run the store's own `prune()`; purging an evaluation store is a human decision | The next append succeeds |
| A re-ingest is refused | `OUTCOME_ALREADY_INGESTED`, `OUTCOME_RECORD_ALREADY_EXPIRED`, `LEARNING_OUTCOME_UNAUTHENTICATED` | By design; nothing was written and nothing needs doing | No new record |
| The advice log head is not anchored after a crash | `ADVICE_LOG_HEAD_NOT_ANCHORED` | Fails closed; an unanchored suffix is never adopted. The owner inspects and truncates the unanchored entry (`docs/learning/OUTCOME_LEARNING_V1.md`). No tool does this today | `AdviceLog.verify()` reports `ok` and `ANCHORED` |
| An anchor update is refused or unreachable | `ADVICE_LOG_ANCHOR_REFUSED`, `ADVICE_LOG_ANCHOR_UNAVAILABLE`, `ANCHOR_CONFLICT` | The previous file content was restored. Retry once the anchor is reachable | `AdviceLog.verify()` |
| The anchor is lost or invalid, or the chain is broken | `ANCHOR_HEAD_LOST`, `ANCHOR_STATE_INVALID`, `ANCHOR_STATE_INSECURE`, `ADVICE_LOG_REWRITTEN`, `ADVICE_LOG_TRUNCATED`, `ADVICE_LOG_CHAIN_BROKEN` | Never re-created or repaired silently. Preserve the files and treat it as a security event. Only an empty log's empty anchor may be cleared by the owner | `AdviceLog.verify()` |

Injected by: `tests/learning`, `tests/capability_loop`.

## Capability packs and adapters

Packs are registered by the owner (`register_pack`); nothing registers one automatically. The OP-009 contract
(`docs/operations/PACK_OPERABILITY_CONTRACT.md`) covers what a pack declares; this section covers recovery.

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| A pack is rejected at registration | `PACK_MANIFEST_INVALID` (or the first `PACK_*` validation code), `PACK_CAPABILITY_CONFLICT`, `PACK_STALE_REGISTRATION` | The registry was not touched. Fix the manifest; a capability another pack defines is never redefined | `pack_operability.check()` passes and the pack registers |
| Registration stopped partway | `PACK_REGISTRATION_INCOMPLETE` | Every adapter that call registered was revoked again; register again | The pack's adapters are listed once |
| Registration stopped and its undo failed | `PACK_REGISTRATION_COMPENSATION_FAILED` | The registry may hold part of the pack. Stop, and call `disable_pack` for that pack (idempotent; receipts are kept) | No adapter of the pack remains registered |
| An adapter fails at run time | `PACK_ADAPTER_FAILED` | Not retryable. Read the declared error code in the result (declared codes keep their own `retryable` flag) and fix the cause before running again | A new run's result |
| A pack is not plannable | `ADAPTER_UNAVAILABLE` | Its runtime reports unavailable, so planning fails closed. Restore the runtime; the AI-Scientist-v3 runtime is `NOT_VALIDATED` | The pack plans again |

Injected by: `tests/capability/test_pack_boundaries.py`, `tests/capability/test_op009_pack_operability.py`.

## Optional projections

Enterprise, connectivity, machines and compute nodes are optional (`PADOS_ENTERPRISE_CONFIG`,
`PADOS_CONNECTIVITY_CONFIG`, `PADOS_MACHINES_CONFIG`, `PADOS_COMPUTE_NODES_CONFIG`, all unset by default). Unset or
broken, they never change Core readiness. `pados doctor --json` reports each one outside its `ok` verdict.

| Symptom | Code | Safe recovery | Verify |
|---|---|---|---|
| A cloud provider is unreachable | `PROVIDER_UNREACHABLE` | The run fails as `RUNTIME_FAILURE` (`retryable`); nothing is rerouted to another provider. Restore the connection and run again | `pados doctor`: `connectivity.wan` is up and not `stale` |
| Every Product Loop dispatch is blocked | `CONNECTIVITY_CONFIG_INVALID` | The connectivity file is invalid, so nothing is probed and dispatch fails closed. Fix the file or unset the variable | `pados doctor`: the `connectivity` section is valid |
| An enterprise capability is refused | `ENTERPRISE_EXTENSION_INVALID`, `REMOTE_EGRESS_NOT_APPROVED`, `SECRET_REFERENCE_NOT_AUTHORIZED`, `SECRET_REFERENCE_UNAVAILABLE` | Reported only. Remote egress needs `allow_remote_egress: true`, and every secret reference must be listed as authorized: owner decisions in the enterprise config | `pados doctor`: `enterprise.status` is `ENTERPRISE_EXTENSION_READY` |
| A machine or node projection is invalid | `MACHINE_INVENTORY_INVALID`, `COMPUTE_NODES_INVALID` | Projection only. Fix the inventory file | `pados doctor`: `machines` / `compute_nodes` observed |

Injected by: `tests/connectivity`, `tests/enterprise`, `tests/machines`.
