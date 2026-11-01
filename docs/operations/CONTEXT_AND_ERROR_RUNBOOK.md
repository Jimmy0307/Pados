# Request context and error envelope (OP-001)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §3.1 and §4. Code:
`src/pados/operability/` (`context.py`, `errors.py`, `http.py`). Tests:
`tests/operability/test_op001_context_and_errors.py`.

These are observability and error semantics only. They authorize, schedule, approve and accept nothing. Every route
keeps its own authorization, approval, TaskLoop, evidence and CAS behaviour, and its status code.

## Where the context exists

| Boundary | Behaviour |
|---|---|
| Core API (`pados serve`) and `/remote/v1` mounted on it | `install_operability(app, component="api")` is the outermost layer |
| Standalone remote listener (`create_remote_app`) | `component="remote"` |
| Compute node service (`create_node_app`) | `component="compute_node"` |
| Home Base → compute node calls (`NodeClient`) | The caller's `X-Request-ID` / `X-Correlation-ID` are forwarded. They never replace the node credential. |
| TaskLoop (`run_step`, `run_one_iteration`) | Runs under the caller's operation as `component="task_loop"`, or under a generated one for CLI and background work. Every `task_loop_events` row gets `payload_json.trace = {request_id, correlation_id}`. Logs: `task_loop.iteration.finished` or `.refused` (stable `error_code`, never message text). |
| Worker routing and deferred model jobs | `component="worker"`. `worker_job_events` rows get `trace`. The independent `ROUTE_REFUSED` audit row in `control_sync_events` gets `trace`. Logs: `worker.job.routed`, `worker.route.refused`, `worker.model_job.finished`. |
| P6.10 capability receipt (`CAPABILITY_RESULT_NORMALIZED`) | `trace` is a top-level key **beside** the sealed `execution_receipt` and `capability_result`, never inside them. Their hashes and every acceptance and lineage check are unchanged. |

The trace is identifiers only. Nothing reads it as authorization, approval, idempotency or evidence. A refusal is
decided exactly as before, even when the request id equals an approval id. The same work traced under two requests
keeps one receipt identity. Proof: `tests/integration/test_op001_trace_continuity_postgres.py`, which covers the API →
TaskLoop → job → receipt chain, a retry under one correlation, a generated CLI trace, a traced refusal and an unhandled
failure whose text never leaks.

Not yet covered: the P7 capability executor's execution ledger (`src/pados/capability/executor.py`), CLI and
healthcheck output (the CLI path persists a generated trace but does not print it), and the browser client (OP-007).

To find everything one request did: `SELECT event_type FROM task_loop_events WHERE payload_json->'trace'->>'request_id'
= '<id>'`. Do the same for `worker_job_events`, and for `control_sync_events` refusals.

## Identifiers

- `X-Request-ID` names one transport request; `X-Correlation-ID` names an operation that may span requests. An
  inbound value is accepted only if it matches `^[A-Za-z0-9._:-]{1,128}$`. Anything else is replaced by a generated
  32-hex id and is never echoed. Without a correlation id, the request id is used.
- Both are echoed on every response, including errors.
- They are not authorization, not idempotency keys and not proof. Never use them to decide access.

## Error envelope `pados.error.v1`

Error responses keep the existing `detail` and add `error`:

| Field | Meaning |
|---|---|
| `code` | Stable `UPPER_SNAKE_CASE`. A code-shaped `detail` (also the part before `:`) becomes the code; prose falls back to the status's generic code (`FORBIDDEN`, `CONFLICT`, `VALIDATION_FAILED`, …). |
| `category` | `VALIDATION` 400/422 · `AUTHORIZATION` 401/403 · `AUTHORITY` 403 · `NOT_FOUND` 404 · `CONFLICT` 409 · `DEPENDENCY` 502/503 · `TIMEOUT` 504 · `RESOURCE` 413/429/507 · `INTERNAL` 500. An existing route keeps its status; the category follows from it. |
| `message` | A fixed safe sentence per category. Prose details and exception text never appear here. |
| `retryable` | Always `false` for validation, authorization, authority, not-found and conflict. For other categories it is `true` only when the raiser declares it (`PadosError`). |
| `write_occurred` | `NO` for GET/HEAD/OPTIONS. Otherwise `UNKNOWN`, unless the raiser declares `YES` or `NO`. Treat `UNKNOWN` as "check state before retrying". |
| `request_id`, `correlation_id` | The same values as the response headers and the log line. |
| `details` | Only what a `PadosError` declares, already redacted. |

An unhandled exception becomes `500 INTERNAL_ERROR`. The server log records only the exception type, never its text.

## One structured log line per request

`http.request.finished` carries `request_id`, `correlation_id`, `component`, `method`, the route template (or
`UNMATCHED`), `status_code`, `outcome` (`completed` < 400, `rejected` 4xx, `failed` 5xx), `error_code` and
`duration_ms`. Headers, bodies, query strings and credentials are never logged.

## Redacted example

The ids below are synthetic. A request that carries no control credential:

```http
GET /memory/search?project_id=…&query=… HTTP/1.1
X-Request-ID: req-7f3a9c

HTTP/1.1 403 Forbidden
X-Request-ID: req-7f3a9c
X-Correlation-ID: req-7f3a9c

{"detail": "missing control token",
 "error": {"schema_version": "pados.error.v1", "code": "FORBIDDEN", "category": "AUTHORIZATION",
           "message": "The request is not authorized.", "retryable": false, "request_id": "req-7f3a9c",
           "correlation_id": "req-7f3a9c", "write_occurred": "NO", "details": {}}}
```

```json
{"event": "http.request.finished", "component": "api", "request_id": "req-7f3a9c", "correlation_id": "req-7f3a9c",
 "method": "GET", "route": "/memory/search", "status_code": 403, "outcome": "rejected", "error_code": "FORBIDDEN",
 "duration_ms": 1.9, "level": "info", "timestamp": "2026-11-01T13:00:00Z"}
```

## Operator use

1. Take the `X-Request-ID` from the failing response (or the `error.request_id`).
2. Search the JSON log for that `request_id`: the `http.request.finished` line gives the route, status, outcome and
   code, and the node log carries the same id for forwarded calls.
3. Act on the category: fix the input (`VALIDATION`), the credential or approval (`AUTHORIZATION`, `AUTHORITY`), or
   the state (`CONFLICT`). Retry only when `retryable` is `true`, and when `write_occurred` is `UNKNOWN` read the
   current state first.
