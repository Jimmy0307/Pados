'use strict';
// pados update: activate the runtime version shipped with this CLI. Data, configuration, receipts and
// artifacts are never touched; the previous runtime stays installed for `pados update --rollback`.
// Migration compatibility (Alembic current vs this runtime's targets) is checked BEFORE switching.

const { PadosError } = require('../errors');
const { layout } = require('../home');
const { resolveRuntimePackage } = require('../platform');
const { installRuntime, readCurrent, runPython, runtimeAt, verifyInstalled, versionDir, writeCurrent } = require('../runtime');
const { ask, freeBytes } = require('./init');
const { laneBlocker, requireInitialized } = require('./lifecycle');
const services = require('../services');
const { humanBytes, write } = require('../output');

const SPEC = { booleans: ['rollback', 'no-migrate', 'migrate'], values: [] };

const BACKUP_ADVICE = 'Back up both PADOS databases first, e.g. pg_dump -Fc -d <name> -f <name>.dump and the same for <name>_corpus. '
  + 'After the migration, pados update --rollback is refused for this database.';

// Forward migration needs explicit consent (review R-3): --yes / --migrate, or an interactive yes. Otherwise nothing
// is migrated and the previous runtime stays active.
async function confirmMigration(ctx, check, { prompt = ask } = {}) {
  if (ctx.flags.yes || ctx.flags.migrate) return;
  const lanes = (check.lanes || []).filter((lane) => ['UNINITIALIZED', 'UPGRADE_AVAILABLE'].includes(lane.state))
    .map((lane) => `${lane.lane} (${lane.database}): ${(lane.revisions || []).join(',') || 'empty'} -> ${lane.target_revision}`);
  if (ctx.interactive) {
    const answer = await prompt(`The database schema must be migrated forward (${lanes.join('; ') || 'pending migrations'}).\n${BACKUP_ADVICE}\nMigrate now? [y/N] `, 'n');
    if (/^y(es)?$/i.test(String(answer).trim())) return;
  }
  throw new PadosError('MIGRATION_CONSENT_REQUIRED', { details: { lanes } });
}

function assertStopped(home) {
  const state = services.readState(home);
  if (state && services.isOwned(state)) throw new PadosError('SERVICES_RUNNING', { details: { pid: state.pid } });
}

function activate(home, current, version, by) {
  return writeCurrent(home, {
    schema: 'pados.cli.runtime-current.v1', version, previous: current.version,
    history: [...(current.history || []), { version, activated_at: new Date().toISOString(), by }],
  });
}

function compatibility(ctx, runtime) {
  const check = runPython(runtime, ctx.home, ['migrate', '--check'], { env: ctx.env }).json;
  if (check.ok === false) throw new PadosError('CONFIG_INVALID', { details: check });
  return check;
}

async function rollback(ctx, current) {
  if (!current.previous) throw new PadosError('NO_PREVIOUS_RUNTIME');
  const runtime = runtimeAt(versionDir(ctx.home, current.previous), current.previous);
  verifyInstalled(runtime.dir);
  const check = compatibility(ctx, runtime);
  if (check.status !== 'CURRENT') {
    const blocker = laneBlocker(check);
    if (blocker && blocker.code === 'MIGRATIONS_INCOMPATIBLE') throw new PadosError('ROLLBACK_INCOMPATIBLE', { details: check });
    throw blocker || new PadosError('ROLLBACK_INCOMPATIBLE', { details: check });
  }
  const record = activate(ctx.home, current, current.previous, 'rollback');
  const result = { status: 'ROLLED_BACK', from: current.version, to: record.version };
  write(ctx.json ? JSON.stringify(result, null, 2) : `PADOS runtime rolled back from ${current.version} to ${record.version}. Start with: pados up`);
  return 0;
}

async function run(ctx) {
  requireInitialized(ctx.home);
  assertStopped(ctx.home);
  const current = readCurrent(ctx.home);
  if (!current || !current.version) throw new PadosError('RUNTIME_NOT_INSTALLED');
  if (ctx.flags.rollback) return rollback(ctx, current);
  const pkg = resolveRuntimePackage({ env: ctx.env });
  if (pkg.version === current.version) {
    const result = { status: 'UP_TO_DATE', version: current.version };
    write(ctx.json ? JSON.stringify(result, null, 2) : `PADOS runtime ${current.version} is current (update the CLI first: npm install -g @pados-ai/cli@latest).`);
    return 0;
  }
  const free = freeBytes(ctx.home);
  if (free < pkg.manifest.files.bytes) {
    throw new PadosError('INSUFFICIENT_DISK', { cause: `Not enough free disk space for runtime ${pkg.version} (${humanBytes(pkg.manifest.files.bytes)} needed).` });
  }
  const installed = installRuntime(ctx.home, pkg);
  const runtime = runtimeAt(installed.dir, pkg.version);
  const before = compatibility(ctx, runtime);
  const blocker = laneBlocker(before);
  let migration = { status: before.status };
  if (blocker && blocker.code === 'MIGRATIONS_PENDING') {
    if (ctx.flags.noMigrate) throw new PadosError('MIGRATIONS_PENDING', { action: 'back up the database, then run pados update --migrate' });
    await confirmMigration(ctx, before);
    migration = runPython(runtime, ctx.home, ['migrate'], { env: ctx.env, timeoutMs: 900000 }).json;
    if (migration.status !== 'CURRENT') throw new PadosError('MIGRATIONS_PENDING', { cause: 'Database migration did not complete; the previous runtime stays active.', details: migration });
  } else if (blocker) {
    throw blocker; // the previous runtime stays active; the new one is installed but not activated
  }
  const record = activate(ctx.home, current, pkg.version, 'update');
  const result = { status: 'UPDATED', from: current.version, to: record.version, migration: migration.status,
    previous_runtime_kept: versionDir(ctx.home, current.version), runtime_dir: layout(ctx.home).runtime };
  if (ctx.json) write(JSON.stringify(result, null, 2));
  else {
    write(`PADOS updated from ${current.version} to ${record.version} (database: ${migration.status}).`);
    write(`The previous runtime is kept for rollback: pados update --rollback`);
    write('Start with: pados up');
  }
  return 0;
}

module.exports = { SPEC, run, confirmMigration };
