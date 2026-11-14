# Metrics and trace context (OP-008)

Code: `src/pados/operability/metrics.py`, `src/pados/operability/context.py`, `src/pados/operability/http.py`.
Tests: `tests/operability/test_op008_metrics_and_trace.py`.

This is a foundation, not monitoring. It gives stable names and labels to bind dashboards and alerts to later. No
exporter, collector, dashboard or alert is configured; those need deployment infrastructure (see "Not done").

## Metrics

The core API serves `GET /metrics` in the Prometheus text format (0.0.4). The counters live in-process and reset when
the process restarts.

| Metric | Kind | Labels |
|---|---|---|
| `pados_http_requests_total` | counter | `method`, `route` (template), `status_class`, `outcome` |
| `pados_http_request_duration_seconds` | histogram | `method`, `route` |
| `pados_http_errors_total` | counter | `route`, `error_code` |
| `pados_dependency_failures_total` | counter | `dependency`, `error_code` |
| `pados_retry_attempts_total` | counter | `operation`, `result` (`retried` / `exhausted`) |
| `pados_deadline_exceeded_total` | counter | `component` (`worker`, `compute_node`) |
| `pados_operation_duration_seconds` | histogram | `operation`, `outcome` (`ok` / `error`) |
| `pados_metrics_rejected_total` | counter | `reason` |

Rules:
- **Names are a contract.** A metric changes meaning only under a new name. Counters end in `_total` and histograms
  in `_seconds`.
- **Bounded cardinality.**
  - `route` is the route template (`/control/approvals/{approval_id}`), never the raw path.
  - A label value outside `[A-Za-z0-9_.:/{}-]{1,96}` becomes `__invalid__`, so ids, file paths, secrets and free text
    never become series.
  - `method` is one of the standard HTTP methods or `OTHER`, so made-up methods cannot add series.
  - Each metric keeps at most 2048 series (about 120 route templates by status class and method); further
    combinations collapse into one `__overflow__` series.
- **Recording never raises.** An undeclared metric or a wrong label set is dropped and counted in
  `pados_metrics_rejected_total`. Treat a non-zero value there as a code defect.
- **Observation only.** No authorization, approval, scheduling, idempotency, receipt or CAS code reads a metric.
- **Exposure.** `/metrics` is in the same class as `/health`: counts by template, status class and stable code, with
  no ids or bodies. Do not expose the core API port beyond the operator machine. Scraping it remotely needs the same
  network controls as the API itself.

## Trace context

- The HTTP adapter accepts a W3C `traceparent` (version `00`, lower-case hex, non-zero ids). A valid one continues
  its trace id; anything else starts a new trace and is never echoed.
- Each bound operation (`operation_scope`) gets its own span id within the trace.
- Structured logs carry `trace_id` and `span_id` beside `request_id` and `correlation_id`.
- `downstream_headers()` adds `traceparent: 00-<trace_id>-<span_id>-01` to calls made inside a bound context.
- The trace persisted beside durable records (`trace_fields()`: TaskLoop events, worker job events, capability
  result normalisation) stays `request_id` and `correlation_id` only. Trace ids never enter a receipt, a capability
  ledger entry, an idempotency key or any sealed hash, so the same work traced twice keeps one identity.

## Not done (needs infrastructure or a later decision)

- An exporter (OTLP or a Prometheus scrape config), dashboards and alert rules.
- Span export and sampling. Spans exist only as ids in logs and headers; there is no span duration export apart from
  `pados_operation_duration_seconds`.
- Metrics for the remote overlay, compute-node service and corpus lane processes. The catalog is ready for them;
  each process would serve its own `/metrics`.
- Persistence across restarts. That is deliberate: these are process metrics, not records.
