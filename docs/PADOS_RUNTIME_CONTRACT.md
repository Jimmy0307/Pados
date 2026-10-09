# PADOS Runtime Contract

Date: 2026-07-10

## Canonical Contract

PADOS core validation is capability-based. Docker Desktop is not a core requirement.

The required core runtime capabilities are:

- PostgreSQL connectivity to the configured database.
- Alembic migration access against that PostgreSQL database.
- A local Python application runtime that can start the FastAPI app without Docker.
- Application health checks against the PADOS API and database connectivity, not container state.
- Backup and restore through PostgreSQL-native tools or a selected runtime adapter.

## Runtime Modes

`PADOS_RUNTIME_MODE` selects the runtime adapter. The default is `native`.

| Mode | Status | Contract |
| --- | --- | --- |
| `native` | Core default | Use Windows-local Python and a reachable PostgreSQL endpoint. Start and stop only the PADOS API process. Backup and restore use `pg_dump`, `dropdb`, `createdb`, and `pg_restore`. |
| `wsl` | Supported contract, adapter pending | Use the same PostgreSQL connectivity contract inside WSL. Windows scripts return a clear adapter-required status instead of treating Docker as required. |
| `docker` | Optional profile | Use Docker Compose as an adapter for PostgreSQL and app support services. Docker checks are required only in this mode. |

## Database Contract

Validation should use an isolated PostgreSQL database named `pados_p6_6_validation`.

PostgreSQL 18 is a valid PostgreSQL runtime if the configured credentials can connect, run migrations, execute tests, and complete backup/restore smoke validation. If PostgreSQL exists but credentials are unavailable or rejected, the correct status is `BLOCKED_ON_POSTGRES_CREDENTIALS`, not `BLOCKED_ON_DOCKER`.

## Script Contract

- `scripts/start.ps1`: in `native` mode, starts the PADOS API process, records the PID, and does not start or stop PostgreSQL.
- `scripts/healthcheck.ps1`: checks `/health` and `pados doctor --json`.
- `scripts/stop.ps1`: in `native` mode, stops the recorded PADOS API process only.
- `scripts/backup.ps1`: in `native` mode, uses `pg_dump` with configured PostgreSQL settings.
- `scripts/restore.ps1`: in `native` mode, uses `dropdb`, `createdb`, and `pg_restore`; restoring over `pados` requires explicit override.

## Doctor Contract

`pados doctor --json` must report:

- runtime mode and supported modes;
- database connectivity status;
- Alembic and migration file availability;
- PostgreSQL tool availability;
- provider configuration;
- Docker availability only as required evidence when `PADOS_RUNTIME_MODE=docker`.

Native and WSL doctor checks must not fail solely because Docker Desktop is absent or stopped.
