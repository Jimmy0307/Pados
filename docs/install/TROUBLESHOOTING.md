# Troubleshooting the npm installation

Start with `pados doctor` (add `--json` for scripts). Every failure prints a cause, a suggested action and a code.
Add `--debug` (or set `PADOS_DEBUG=1`) to see details and stack traces. Logs of the running service are in
`PADOS_HOME/logs/api-*.err.log` and `api-*.out.log`.

| Code | What it means | What to do |
|---|---|---|
| `RUNTIME_PACKAGE_MISSING` | npm skipped the platform runtime package (often `--omit=optional` or `--no-optional`) | `npm install -g @pados-ai/cli` without omitting optional dependencies; offline: install the runtime tarball together with the CLI tarball (`npm install -g --offline <runtime>.tgz <cli>.tgz`) |
| `RUNTIME_VERSION_MISMATCH` | CLI and runtime package versions differ | reinstall both at the same version |
| `RUNTIME_PACKAGE_INVALID` | the runtime package does not match its manifest (corrupted or incomplete download) | reinstall; compare the tarball with its `.sha256` file |
| `RUNTIME_NOT_INSTALLED` | `init` has not run for this PADOS home | `pados init` |
| `RUNTIME_VERIFY_FAILED` | files inside `PADOS_HOME/runtime/<version>` were changed | `pados init` reinstalls the runtime; data is kept |
| `PLATFORM_UNSUPPORTED` | no runtime package for this OS/CPU | Windows x64 and Linux x64 are supported in this release |
| `NOT_INITIALIZED` / `CONFIG_MISSING` | no `config/pados.env` | `pados init` |
| `CONFIG_INVALID` | Core rejected a value (doctor lists the fields) | fix `PADOS_HOME/config/pados.env`, or `pados init --reconfigure` |
| `SECRET_REFUSED` | a password or credential-shaped value was passed | use `--db-password-ref secretref://env/<NAME>` and export the variable |
| `SECRET_IN_CONFIG` (doctor warning) | a credential is stored in the configuration file | remove it; use `PADOS_DB_PASSWORD_REF` |
| `DATABASE_UNREACHABLE` | PostgreSQL does not answer at the configured host/port | start PostgreSQL; check `PADOS_DB_HOST` / `PADOS_DB_PORT`; firewall |
| `DATABASE_AUTHENTICATION_FAILED` | wrong or missing password | export the variable named by `PADOS_DB_PASSWORD_REF` (default `PADOS_DB_PASSWORD`) in the same shell that runs `pados` |
| `DATABASE_MISSING` | the PADOS databases do not exist | `pados init --create-databases`, or create `<name>` and `<name>_corpus` yourself |
| `DATABASE_DRIVER_UNAVAILABLE` | Linux: libpq is not installed | install `libpq5` (Debian/Ubuntu) or `libpq` (Fedora/RHEL); PADOS never installs it for you |
| `DATABASE_TARGET_REFUSED` / `PROTECTED_DATABASE_TARGET` | database names collide with protected developer databases or with each other | choose dedicated names: `pados init --reconfigure --db-name <name>` |
| `MIGRATIONS_PENDING` | the schema is older than this version | `pados init` (or `pados update --migrate`) applies the migrations |
| `MIGRATION_CONSENT_REQUIRED` | `pados update` needs to migrate the database forward and was not confirmed (non-interactive run without `--migrate`/`--yes`, or "no" at the prompt); nothing was migrated and the previous runtime stays active | back up both databases (`pg_dump -Fc -d <name> -f <name>.dump`, same for `<name>_corpus`), then `pados update --migrate`; afterwards `--rollback` is refused for that database |
| `MIGRATIONS_INCOMPATIBLE` | the schema is newer than this version or foreign | use the matching version (`pados update --rollback` or reinstall it) or restore a backup |
| `PORT_IN_USE` | another program (or PADOS) uses the API port | `pados status`; or `pados init --reconfigure --api-port <port>` |
| `START_FAILED` | the service exited during startup | `pados doctor`; read `logs/api-*.err.log`; rerun `pados up --debug` |
| `HEALTH_TIMEOUT` | the service did not report healthy in time | `pados status`, `pados doctor`; `pados up --timeout 180` on slow machines |
| `NOT_OWNED` | the recorded pid is not a PADOS process started by this CLI | nothing was stopped; the stale record was cleared |
| `SERVICES_RUNNING` | update/uninstall while PADOS runs | `pados down`, then retry |
| `NO_PREVIOUS_RUNTIME` / `ROLLBACK_INCOMPATIBLE` | nothing to roll back to, or the database was migrated forward | restore the database backup taken before the update |
| `INSUFFICIENT_DISK` | free space is below runtime size plus reserve | free space, or lower the reserve with `--min-free-bytes` |
| `CONSENT_REQUIRED` | large disk use (FULL_LOCAL or more than 1 GiB) needs consent | re-run with `--yes` |
| `PADOS_HOME_RESERVED_PATH` / `PADOS_HOME_NOT_ABSOLUTE` | unusable `PADOS_HOME` | unset it or choose an absolute directory |

## Doctor warnings that are expected on a fresh install

- `runtime: NO_LOCAL_RUNTIME`, `models: NO_LOCAL_MODELS` - no local model runtime (such as Ollama) is running. Optional.
- `readiness: CORE_DEGRADED` with `PROOF_DB_UNCONFIGURED` - Core and the API run; the optional proof lane for the
  full product loop is set up separately (`docs/product/QUICKSTART.md`).
- `disk_pressure: STORAGE_PRESSURED` - less than 15 % of the volume is free after the reserve. PADOS keeps running and
  avoids growth; nothing is deleted.

## Windows notes

- `npm install` and `pados init` copy about 6,000 files; antivirus scanning can make them take one to two minutes.
- `pados down` stops the service with `taskkill` (Windows has no graceful signal for detached processes); PADOS state
  is transactional in PostgreSQL, so this is safe.
- The whole PADOS home (`config`, `data`, `logs`, `state`, `runtime`, `cache`) gets an ACL that grants only your
  account access, also when `PADOS_HOME` points outside `%LOCALAPPDATA%`. Administrators can still take ownership;
  share data deliberately (copy it out) rather than by widening this ACL.

## Reporting a problem

Attach `pados doctor --json` and `pados version --json`. Both are free of credentials. Do not attach
`config/control.token`.

## Development-only environment variables

`PADOS_CLI_DEV_PYTHON` / `PADOS_CLI_DEV_APP` (run another interpreter and app directory) and
`PADOS_RUNTIME_PACKAGE_DIR` (use an extracted runtime package) are for PADOS development only. While one is set,
every command prints `WARNING: <name> is set (development-only override ...)` on stderr. If you see this warning
and did not set the variable on purpose, unset it and re-run the command.
