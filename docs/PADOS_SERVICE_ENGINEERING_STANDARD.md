# PADOS Service Engineering Standard

Status: canonical engineering baseline (additive, fail-closed, no authority or scheduler change)
Version: 1.0
Effective date: 2026-11-01
Owner: PADOS maintainers

## 1. Purpose and boundary

This standard defines the minimum operational contract for a PADOS service, worker, adapter, capability pack, CLI surface, or browser-facing API. It is designed to keep a feature that is functionally correct from becoming opaque, non-recoverable, or unsafe to operate.

The standard does not create another scheduler, authority, policy engine, approval path, or semantic acceptance layer. Existing capability authority, approval gates, task-loop ownership, evidence receipts, CAS/state-proof boundaries, and fail-closed behavior remain authoritative. A component may add evidence and observability only through existing boundaries.

Every implementation must be assessable as one of:

- `GREEN`: required contract is implemented, tested, and operationally documented.
- `YELLOW`: implemented, but inconsistent, partial, or not enforced at all boundaries.
- `ORANGE`: useful functionality exists, but a material operational contract is missing.
- `RED`: production-readiness blocker or credible path to data, authority, or recovery failure.
- `N/A`: the field is genuinely not applicable; the reason must be recorded.

`P0` means stop-ship or acceptance-blocking risk. `P1` means before broad production/Kubernetes use. `P2` means planned reliability and operator experience work. `P3` means consistency or polish.

## 2. Required service contract

Each component must have a small contract record in its module documentation or an adjacent design document. The record must name:

| Field | Required content |
|---|---|
| Purpose | The single operational responsibility and explicit non-responsibilities. |
| Inputs | Schema, size limits, authentication context, correlation fields, and validation rules. |
| Outputs | Schema, status transitions, evidence/receipt behavior, and redaction rules. |
| State | Persistent and in-memory state, owner, lifecycle, consistency boundary, and recovery source. |
| Dependencies | Database lane, filesystem/CAS, provider, worker, network, human approval, and optional dependencies. |
| API/request contract | Route or callable name, method, authorization, deadline, response/error envelope, and versioning. |
| Events | Events consumed/emitted, schema/version, ordering assumptions, deduplication key, and replay behavior. |
| Resource requirements | CPU, memory, disk, connection pool, upload/output limits, concurrency, and graceful degradation. |
| Security | Identity, permission, project scope, secret reference, audit evidence, and forbidden data exposure. |

The contract must state what the component does when a dependency is unavailable. Unknown, malformed, unauthorized, or stale authority input must fail closed and must never be silently reclassified as success.

## 3. Observability contract

### 3.1 Structured logs

Operational logs are JSON objects, not prose-only lines. Every request, job, loop iteration, capability execution, approval decision, and recovery transition must carry the fields that apply:

```json
{
  "event_name": "task_loop.iteration.finished",
  "timestamp": "RFC3339 UTC",
  "service": "pados-api",
  "component": "task_loop",
  "level": "info",
  "request_id": "transport request id",
  "correlation_id": "cross-boundary operation id",
  "run_id": "domain run or job id",
  "project_id": "scoped project id",
  "outcome": "completed|rejected|failed|degraded",
  "error_code": null,
  "duration_ms": 0,
  "retry_attempt": 0,
  "dependency": "database|provider|worker|filesystem",
  "redaction": "none|partial"
}
```

Rules:

1. Bind or generate `request_id` at the transport boundary; preserve it across calls.
2. Use `correlation_id` for a business operation that crosses requests or queues. Do not use it as a substitute for authorization or an idempotency key.
3. Never log tokens, private keys, raw credentials, full prompts, unrestricted file contents, or unredacted provider payloads.
4. Log refusal, timeout, retry, lease loss, rollback, and recovery as first-class events.
5. A successful response without a corresponding outcome log is an observability defect.

PADOS currently has a `structlog` JSON configuration and several domain receipts, but not every boundary binds these fields. New code must follow this contract even before a central collector exists.

### 3.2 Metrics

Metrics use stable names and bounded labels. Minimum recommended metrics are:

- `pados_requests_total{service,route,outcome}`
- `pados_request_duration_seconds{service,route}
- `pados_errors_total{service,component,error_code,retryable}`
- `pados_dependency_requests_total{dependency,outcome}`
- `pados_dependency_duration_seconds{dependency}`
- `pados_jobs_inflight{service,queue}`
- `pados_jobs_total{service,outcome}`
- `pados_retries_total{component,error_code}`
- `pados_lease_expirations_total{component}`
- `pados_health_status{service,dependency}`

Do not use unbounded labels such as prompt text, file path, request body, user input, or arbitrary exception strings. A component may expose metrics through an existing adapter; it must not introduce a second monitoring authority.

### 3.3 Traces and propagation

HTTP, queue, worker, provider, database, and filesystem boundaries must preserve a W3C-compatible trace context when tracing is enabled. At minimum, create spans for request ingress, authorization/approval, database transaction, external provider call, queue/job execution, lease acquisition/heartbeat, and evidence publication. A missing tracer must not change business semantics; the component records a bounded `trace_unavailable` signal instead.

## 4. Error contract

Every externally visible failure has a stable `UPPER_SNAKE_CASE` code, a category, retryability, safe operator detail, and a causal receipt where the operation can produce evidence.

Recommended categories:

| Category | Meaning | Default retry |
|---|---|---|
| `VALIDATION` | Input/schema/size/contract failure. | No |
| `AUTHORIZATION` | Identity, project scope, approval, or step-up failure. | No |
| `AUTHORITY` | Capability revocation, policy mismatch, stale plan, or semantic boundary failure. | No |
| `CONFLICT` | Idempotency/content/lease/state transition conflict. | Only after caller reconciliation |
| `DEPENDENCY` | Database, network, provider, filesystem, or worker unavailability. | Bounded, if explicitly retryable |
| `TIMEOUT` | Deadline expired or dependency exceeded its budget. | Bounded, if operation is safe |
| `RESOURCE` | Queue, memory, disk, upload, rate, or concurrency limit. | Backoff or operator action |
| `INTERNAL` | Unexpected defect. | No automatic retry unless classified |

The response envelope should include `code`, `message` safe for the caller, `retryable`, `request_id`, `write_occurred`, and a redacted `details` object. HTTP mapping is explicit: validation `400`, authorization `401/403`, conflict `409`, dependency/timeout `502/503/504`, resource `429/507`, internal `500`. The mapping may be narrower for an existing route, but must be documented.

Unknown exceptions must not be converted to a successful or retryable response by default. A boundary must preserve whether a write occurred and whether a receipt/evidence record was emitted.

## 5. Retry, timeout, idempotency, and leases

1. Every network, provider, queue, database, and subprocess call has an explicit deadline. A child deadline cannot exceed the remaining parent budget.
2. Retries are bounded by attempt count and elapsed budget, use backoff with jitter, and emit a retry event/metric. Authorization, validation, authority, schema, and content-conflict failures are not automatically retried.
3. A mutating request declares an idempotency key or is explicitly documented as non-idempotent. The same key with a different canonical content hash is a conflict, never a second execution.
4. A lease names owner, acquisition time, expiry, heartbeat time, and fencing/revalidation behavior. Lease loss prevents publication of untrusted output.
5. Heartbeats are observable and bounded. A heartbeat failure is a state transition (`LEASE_LOST` or equivalent), not a hidden log message.
6. Recovery is safe to repeat. If it is not safe to repeat, the component must require reconciliation or human approval.

The existing task-loop lease, idempotency records, capability revalidation, and remote step-up proof are evidence to preserve and extend; they do not remove the need for common request/error/metric fields.

## 6. Health semantics

All long-running or externally callable services define:

- **Startup**: configuration and dependency wiring have completed; startup failure is explicit.
- **Liveness**: the process/event loop is alive. It must not claim database or provider correctness.
- **Readiness**: the service can safely accept the class of work advertised by the endpoint. A required dependency failure returns non-ready; an optional dependency is `DEGRADED` with a reason.
- **Dependency detail**: status, bounded latency, last success, and safe reason; never credentials or raw connection strings.

`/health` and `/ready` must have stable schemas, bounded probe timeouts, and documented status codes. A readiness failure must not be hidden behind HTTP 200. Health checks must not perform mutating work or acquire long-lived leases. The existing API has `/health`, `/ready`, and a richer operations health projection; future work should converge their semantics without changing authority.

## 7. Testing and failure injection

Each component needs tests for:

- happy path and schema/authorization boundaries;
- timeout, dependency failure, malformed response, cancellation, and resource exhaustion;
- retry budget and no-retry error classes;
- duplicate request and same-key/different-content conflict;
- lease acquisition, heartbeat, expiry, fencing, and recovery;
- evidence/receipt presence and redaction;
- health startup/liveness/readiness/degraded behavior;
- restart/resume/rollback or an explicit proof that recovery is not applicable.

Failure injection must be deterministic and local: fake clock, fake dependency, bounded network failure, temporary database disconnect, provider timeout, or controlled process termination. Tests must not assert fabricated live-provider success. A skipped test is a reported limitation, not a pass.

## 8. Runbook minimum

Every production-facing component has a runbook with:

1. ownership and escalation;
2. startup, readiness, and safe shutdown commands;
3. links to logs, metrics, traces, receipts, and relevant tables/artifacts;
4. symptom-to-error-code and symptom-to-dependency mapping;
5. stuck job/lease/retry recovery procedure;
6. data-safety and authority boundaries during recovery;
7. rollback/forward-fix procedure;
8. post-incident evidence and verification checklist.

Runbooks must distinguish local dogfood, disposable test infrastructure, and production-like evidence. “Restart it” is not a recovery procedure unless the state and idempotency consequences are documented.

## 9. CI/CD gates

The existing PADOS workflows remain the enforcement point. Additive operability gates should be introduced in this order:

1. static contract inventory: required service record, stable error codes, route health schema, and bounded resource settings;
2. unit/contract tests for errors, idempotency, leases, and health;
3. deterministic failure-injection and recovery tests;
4. lint, typecheck, migration, security, secret-scan, packaging, and frontend gates already present;
5. artifact provenance, manifest/hash verification, and no unreviewed generated output;
6. platform-specific evidence for Windows/Linux or a clear `NOT_VALIDATED` result;
7. release rule: no production claim when a required runtime lane is skipped or only mocked.

CI must report `PASS`, `FAIL`, `SKIPPED`, or `NOT_VALIDATED` separately. A passing static validator cannot replace missing raw runtime evidence. The gate must fail closed on missing manifests, inconsistent counts, stale hashes, or an unclassified error path.

## 10. Adoption checklist

Before a component is called production-ready, reviewers must be able to answer “yes” to every applicable item:

- [ ] Purpose, I/O, state, dependencies, API, events, resources, and security are documented.
- [ ] Request/correlation/run/project IDs are preserved across boundaries.
- [ ] Logs are structured, redacted, and emitted for outcome, refusal, retry, timeout, lease loss, and recovery.
- [ ] Metrics and trace spans have bounded labels and documented ownership.
- [ ] Errors have stable codes, category, retryability, safe message, and write/evidence semantics.
- [ ] Deadline, retry, idempotency, and lease/heartbeat behavior is explicit and tested.
- [ ] Startup/liveness/readiness/degraded health semantics are tested.
- [ ] Failure injection and recovery tests exist, or N/A is justified.
- [ ] A runbook exists and distinguishes evidence from assumptions.
- [ ] CI/CD gates enforce the contract and report skipped/not-validated lanes.

This checklist is a baseline for the audit and backlog. It is not permission to bypass existing approval, capability authority, or evidence gates.
