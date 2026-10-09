'use strict';
// Failure UX: every expected failure is a PadosError with a code, a plain cause and a suggested action.
// Stack traces are shown only with --debug or PADOS_DEBUG=1.

const CATALOG = Object.freeze({
  PLATFORM_UNSUPPORTED: ['This platform is not supported by this PADOS release.', 'see docs/install/INSTALLATION_ARCHITECTURE.md (supported platforms)'],
  RUNTIME_PACKAGE_MISSING: ['The PADOS runtime package for this platform is not installed.', 'reinstall with optional dependencies enabled: npm install -g @pados-ai/cli (do not use --omit=optional)'],
  RUNTIME_VERSION_MISMATCH: ['The PADOS runtime package version does not match the CLI version.', 'reinstall both together: npm install -g @pados-ai/cli'],
  RUNTIME_PACKAGE_INVALID: ['The PADOS runtime package is incomplete or corrupted.', 'reinstall: npm install -g @pados-ai/cli'],
  RUNTIME_NOT_INSTALLED: ['PADOS has not been initialized on this machine.', 'pados init'],
  RUNTIME_VERIFY_FAILED: ['The installed PADOS runtime failed verification.', 'pados init (reinstalls the runtime; your data is kept)'],
  NOT_INITIALIZED: ['PADOS has not been initialized (no configuration found).', 'pados init'],
  PADOS_HOME_RESERVED_PATH: ['PADOS_HOME points to a reserved location (the drive-root pados directory of developer checkouts).', 'unset PADOS_HOME or choose another directory'],
  PADOS_HOME_NOT_ABSOLUTE: ['PADOS_HOME must be an absolute path.', 'set PADOS_HOME to an absolute directory'],
  INSUFFICIENT_DISK: ['There is not enough free disk space for the requested installation.', 'free disk space or lower the reserve (--min-free-bytes)'],
  CONSENT_REQUIRED: ['This action may use a large amount of disk space and needs explicit consent.', 're-run with --yes'],
  CONFIG_INVALID: ['The PADOS configuration has invalid values.', 'pados doctor (shows the invalid fields), then fix the configuration file'],
  CONFIG_EXISTS: ['A PADOS configuration already exists.', 'keep it, or re-run with --reconfigure (data is never deleted)'],
  SECRET_REFUSED: ['Credentials are never written to PADOS configuration.', 'pass a SecretRef instead: --db-password-ref secretref://env/<NAME>'],
  DATABASE_UNREACHABLE: ['PostgreSQL is not reachable.', 'pados doctor'],
  DATABASE_AUTHENTICATION_FAILED: ['PostgreSQL rejected the credentials.', 'set the variable named by PADOS_DB_PASSWORD_REF, then: pados doctor'],
  DATABASE_MISSING: ['A PADOS database does not exist yet.', 'create it, or run: pados init --create-databases'],
  MIGRATIONS_PENDING: ['Database migrations are pending.', 'pados init (applies them through the PADOS migration graph)'],
  MIGRATION_CONSENT_REQUIRED: ['This update must migrate the database forward; that needs explicit confirmation (afterwards rollback is no longer possible). Nothing was migrated and the previous runtime stays active.', 'back up both PADOS databases (pg_dump), then run: pados update --migrate (or --yes)'],
  MIGRATIONS_INCOMPATIBLE: ['The database schema does not match this PADOS version.', 'pados doctor; use the matching version (pados update --rollback) or restore a backup'],
  DATABASE_DRIVER_UNAVAILABLE: ['The PostgreSQL client library (libpq) was not found.', "install your distribution's libpq package (Debian/Ubuntu: libpq5; Fedora/RHEL: libpq), then: pados doctor"],
  DATABASE_TARGET_REFUSED: ['The configured database names are not allowed.', 'choose dedicated names: pados init --reconfigure --db-name <name>'],
  PORT_IN_USE: ['The PADOS API port is already in use.', 'pados status, or choose another port: pados init --reconfigure --api-port <port>'],
  START_FAILED: ['The PADOS service exited during startup.', 'pados doctor (details: run again with --debug)'],
  HEALTH_TIMEOUT: ['The PADOS service did not become healthy in time.', 'pados status; pados doctor'],
  NOT_RUNNING: ['PADOS is not running.', 'pados up'],
  NOT_OWNED: ['The recorded process is not a PADOS service started by this CLI.', 'nothing was stopped; the stale record was cleared'],
  SERVICES_RUNNING: ['PADOS services are running.', 'pados down, then retry'],
  NO_PREVIOUS_RUNTIME: ['There is no previous runtime version to roll back to.', 'pados version'],
  ROLLBACK_INCOMPATIBLE: ['The database was migrated by the newer version; the previous version cannot use it.', 'restore a database backup taken before the update (see docs/install/TROUBLESHOOTING.md)'],
  RUNTIME_COMMAND_FAILED: ['A PADOS runtime command failed.', 'pados doctor (details: run again with --debug)'],
  USAGE: ['The command line is not valid.', 'pados help'],
  INTERNAL_ERROR: ['An unexpected internal error occurred.', 'run again with --debug and see docs/install/TROUBLESHOOTING.md'],
});

class PadosError extends Error {
  constructor(code, { cause, action, details, exitCode = 1 } = {}) {
    const known = CATALOG[code] || CATALOG.INTERNAL_ERROR;
    super(cause || known[0]);
    this.name = 'PadosError';
    this.code = code;
    this.action = action || known[1];
    this.details = details;
    this.exitCode = exitCode;
  }
}

function debugEnabled(flags = {}, env = process.env) {
  const value = String(env.PADOS_DEBUG || '').toLowerCase();
  return Boolean(flags.debug) || (value !== '' && value !== '0' && value !== 'false');
}

function formatFailure(error, { headline = 'PADOS could not complete the command.', debug = false } = {}) {
  const known = error instanceof PadosError;
  const lines = [headline,
    `Cause: ${known ? error.message : CATALOG.INTERNAL_ERROR[0]}`,
    `Suggested action: ${known ? error.action : CATALOG.INTERNAL_ERROR[1]}`];
  if (known) lines.push(`Code: ${error.code}`);
  if (debug) {
    if (error && error.details !== undefined) lines.push(`Details: ${JSON.stringify(error.details, null, 2)}`);
    lines.push(String((error && error.stack) || error));
  }
  return lines.join('\n');
}

module.exports = { CATALOG, PadosError, debugEnabled, formatFailure };
