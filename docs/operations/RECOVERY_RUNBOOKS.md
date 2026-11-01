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
| A step is refused | `LOOP_LEASE_HELD` | Another owner holds a live lease. Wait, or let it lapse (≤ 5 min) when that owner has crashed, then resume with an explicit owner | `task_loop.lease.held` / `.renewed` for the new owner; iterations continue from the persisted ones |
| A step answers `WAITING_HUMAN` | approval reasons (`APPROVAL_REVOKED`, `APPROVAL_EXPIRED`, `APPROVAL_CONSUMED`) | Human approval: create and decide a new approval and start a new loop. Never edit the approval row | The new loop routes under the new approval |
| A job stays non-terminal past its deadline | `JOB_DEADLINE_EXCEEDED` (`TIMED_OUT`) | The next step expires it automatically; then step again | `worker_jobs.status = TIMED_OUT`, and the loop decides on it |

Injected by: `tests/integration/test_op004_lease_postgres.py`, `tests/integration/test_sec4_approval_lifecycle_postgres.py`,
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
