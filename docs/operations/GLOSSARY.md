# Operability glossary (OP-010)

One name per idea in code, logs, docs and handoffs. The "Not" column lists the synonyms to avoid in new work. Use the
canonical term in field names, log keys and headings. Existing durable field names are not renamed: renaming a
persisted or sealed field changes identities.

## Health

| Term | Meaning | Not |
|---|---|---|
| liveness | The process is up and serving (`/health/live`). It says nothing about dependencies. | ping, alive check |
| readiness | Every required dependency answered a bounded, non-mutating probe (`/health/ready`). | health check (when readiness is meant) |
| check | One named readiness probe with `status`, `required` and `latency_ms` (`pados.health.v1`). | probe result, signal |
| degraded | Serving, but an optional dependency is down. Never used for a required one. | partially healthy |

See [HEALTH.md](HEALTH.md).

## Identity and tracing

| Term | Meaning | Not |
|---|---|---|
| request id | One transport request (`X-Request-ID`). | trace id, call id |
| correlation id | One business operation across requests and jobs (`X-Correlation-ID`). | session id, flow id |
| trace id / span id | W3C trace context for logs and downstream calls only. Never persisted beside records. | request id |
| trace | The identifiers persisted beside a durable record (`trace_fields()`: request and correlation id). | context, metadata |

Context ids are never authority, proof or idempotency. See [CONTEXT_AND_ERROR_RUNBOOK.md](CONTEXT_AND_ERROR_RUNBOOK.md)
and [METRICS_AND_TRACE.md](METRICS_AND_TRACE.md).

## Errors

| Term | Meaning | Not |
|---|---|---|
| error code | A stable `UPPER_SNAKE` identifier, the only thing clients branch on. | error message, reason string |
| error envelope | The `error` object (`pados.error.v1`): code, category, safe message, retryable, write outcome, ids. | error detail |
| detail | The pre-existing `detail` field, kept for compatibility. New clients read the envelope. | error body |
| write outcome | `write_occurred`: `YES`, `NO` or `UNKNOWN`. `UNKNOWN` means read state before retrying. | committed flag |
| refusal | A fail-closed rejection (validation, authorization, authority, conflict). Never retried. | error (when a refusal is meant) |

## Work and scheduling

| Term | Meaning | Not |
|---|---|---|
| loop | A TaskLoop: the single scheduler's unit of supervised work. | job (for a loop), pipeline |
| iteration / step | One scheduler advance of a loop. | tick, cycle |
| job | One worker execution routed by the worker router. | task (for a worker job) |
| run | One capability execution under the governed executor. | job (for a capability run) |
| lease | A loop's exclusive, renewed claim by one owner instance. A lapsed lease may be taken over. | lock |
| idempotency key | The client-supplied key that makes a write safe to repeat. The same key with a different payload is refused. | request id |
| deadline | The caller's remaining time budget (`X-Request-Deadline-Ms`). It can only shorten a timeout. | timeout (for the parent budget) |

See [DEADLINE_RETRY_IDEMPOTENCY_LEASE.md](DEADLINE_RETRY_IDEMPOTENCY_LEASE.md).

## Evidence and authority

| Term | Meaning | Not |
|---|---|---|
| receipt | The sealed, hashed record of a governed action. The authority for what happened. | log, event |
| event | An append-only progress record (loop or job events). Observational, not authority. | receipt, audit log |
| evidence | A file that a lane or review points to, bound by commit, platform, command, exit code and hash. | proof (evidence binds; review judges) |
| lane | A named runtime validation path with its own platform, claims and staleness window. | environment, suite |
| execution complete | The work ran to the end. It is not acceptance. | done, accepted |
| acceptance | A human or authority decision on the result through the existing approval and acceptance paths. | pass, green |
| approval / grant | Human authorization bound to a subject (approval), and the capability permission it yields (grant). | consent, token |

See [RUNTIME_LANE_GATE.md](RUNTIME_LANE_GATE.md) and [PACK_OPERABILITY_CONTRACT.md](PACK_OPERABILITY_CONTRACT.md).

## Checking

`python scripts/release/doc_lint.py <out.json>` checks that every relative link and backticked repository path in
`docs/` and the root runbooks resolves. CI runs it. A doc that deliberately names absent paths (an export exclusion
list) carries `<!-- doc-lint: names-absent-paths -->`.
