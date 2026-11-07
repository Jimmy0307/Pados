# Health semantics (OP-003)

Contract: [`PADOS_SERVICE_ENGINEERING_STANDARD.md`](../PADOS_SERVICE_ENGINEERING_STANDARD.md) §6. Code:
`src/pados/operability/health.py`, `src/pados/api/app.py`. Tests: `tests/operability/test_op003_health.py`.

Health reports observe. They never authorize, schedule or accept anything, and they never mutate state or take a lease.

| Endpoint | Meaning | Status codes |
|---|---|---|
| `GET /health/live` | Liveness: the process serves requests. It probes and claims no dependency. | Always 200, `status: ALIVE` |
| `GET /health/ready` | Readiness of the core API, schema `pados.health.v1`. Bounded probes (5 s total). | 200 `OK` or `DEGRADED`; 503 `NOT_READY` |
| `GET /ready` | Product Loop readiness. It keeps its existing decision (proof database, object root, product configuration and provider are required) and adds the same decision in `health` (`pados.health.v1`). | 200 / 503 |
| `GET /health` | Legacy summary: status, version, environment and a database ping. A failure reads `{"status": "error", "error": "DATABASE_UNAVAILABLE"}`; driver text is never echoed. | Always 200; use `/health/ready` for readiness |

## `pados.health.v1`

Each check has `name`, `required`, `status` (`OK`, `DEGRADED`, `FAILED`, `NOT_CONFIGURED`), a stable `reason` code and
`latency_ms`. They aggregate as follows:
- any **required** check that is not `OK` → `NOT_READY` (503);
- otherwise any **optional** check that is `DEGRADED` or `FAILED` → `DEGRADED` (200);
- an optional check that is only `NOT_CONFIGURED` degrades nothing;
- otherwise → `OK`.

| Check | Required on `/health/ready` | Failure codes |
|---|---|---|
| `operational_database` | always | `OPERATIONAL_DB_UNAVAILABLE`, `OPERATIONAL_MIGRATION_STALE`, `PROBE_TIMEOUT`, `PROBE_FAILED` |
| `proof_database` | only when `PADOS_PRODUCT_LOOP_ENABLED=true` | `PROOF_DB_UNCONFIGURED` (not configured), `PROOF_DB_NOT_SEPARATE`, `PROOF_DB_UNAVAILABLE`, `PROOF_MIGRATION_STALE` |
| `product_object_root` | only with the Product Loop | `OBJECT_ROOT_UNUSABLE` |
| `product_loop_configuration` | only with the Product Loop | `PRODUCT_LOOP_UNCONFIGURED` (not configured) |
| `provider_routing` | only with the Product Loop | `PROVIDER_UNAVAILABLE` (not configured while the Product Loop is disabled) |
| `corpus_database` | never | `CORPUS_DB_UNAVAILABLE`, `CORPUS_MIGRATION_STALE` (degraded), `CORPUS_DB_UNCONFIGURED` (not configured). Retrieval and ingestion only; the core loop does not need it. |
| `chat_provider` | never | `MOCK_CHAT_PROVIDER_SELECTED` (degraded), `CHAT_PROVIDER_UNCONFIGURED` (not configured). `OK` means configured, not that a live call succeeded. |

A probe that does not finish in time reports `PROBE_TIMEOUT`. One that raises reports `PROBE_FAILED`, and its message
is never shown. Credentials, connection strings, hosts and exception text never appear in any health response.

## Operator use

- Process supervisors and restart policies: `/health/live`.
- Routing work or deciding whether the core API can take requests: `/health/ready`. Act on the first required check
  that is not `OK`.
- Before enabling the Product Loop: `/ready` must be 200.
