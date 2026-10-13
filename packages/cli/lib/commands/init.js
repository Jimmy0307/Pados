'use strict';
// pados init: install the platform runtime into PADOS_HOME, write the one configuration file, write the
// storage profile (via Core), migrate a reachable database through the existing Alembic graph, and report
// readiness. Never installs PostgreSQL, model runtimes or models; never contacts a cloud service.

const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline/promises');
const { PadosError } = require('../errors');
const { ensureLayout, layout } = require('../home');
const { resolveRuntimePackage } = require('../platform');
const { activeRuntime, installRuntime, readCurrent, runPython, runtimeAt, writeCurrent } = require('../runtime');
const { buildConfig, ensureControlToken, writeConfig } = require('../config');
const { humanBytes, renderDoctor, write } = require('../output');

const STORAGE_MODES = ['LIGHTWEIGHT', 'STANDARD', 'FULL_LOCAL'];
const LARGE_DISK_BYTES = 1024 ** 3;

const SPEC = {
  booleans: ['reconfigure', 'create-databases', 'skip-database', 'rotate-control-token'],
  values: ['storage', 'min-free-bytes', 'db-host', 'db-port', 'db-user', 'db-name', 'db-password-ref',
    'db-passfile', 'db-password', 'api-port'],
};

function freeBytes(target) {
  let probe = target;
  while (!fs.existsSync(probe)) {
    const parent = path.dirname(probe);
    if (parent === probe) break;
    probe = parent;
  }
  const stats = fs.statfsSync(probe);
  return Number(stats.bavail) * Number(stats.bsize);
}

async function ask(question, fallback) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    const answer = (await rl.question(question)).trim();
    return answer || fallback;
  } finally {
    rl.close();
  }
}

async function chooseStorage(flags, interactive) {
  let mode = flags.storage ? String(flags.storage).toUpperCase() : undefined;
  if (!mode && interactive) {
    mode = (await ask(`Storage mode [${STORAGE_MODES.join('/')}] (LIGHTWEIGHT): `, 'LIGHTWEIGHT')).toUpperCase();
  }
  mode = mode || 'LIGHTWEIGHT';
  if (!STORAGE_MODES.includes(mode)) {
    throw new PadosError('USAGE', { cause: `--storage must be one of ${STORAGE_MODES.join(', ')}.`, exitCode: 2 });
  }
  return mode;
}

async function confirmDisk(ctx, plan) {
  const large = plan.runtimeBytes > LARGE_DISK_BYTES || plan.storage === 'FULL_LOCAL';
  if (!large || ctx.flags.yes) return;
  if (ctx.interactive) {
    const answer = await ask(`This installation may use substantial disk space (${plan.reason}). Continue? [y/N] `, 'n');
    if (/^y(es)?$/i.test(answer)) return;
  }
  throw new PadosError('CONSENT_REQUIRED', { details: plan });
}

function parseMinFree(value) {
  if (value === undefined) return undefined;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 0) {
    throw new PadosError('USAGE', { cause: '--min-free-bytes must be a non-negative integer.', exitCode: 2 });
  }
  return parsed;
}

function installPlatformRuntime(ctx, minFree) {
  if (ctx.env.PADOS_CLI_DEV_PYTHON && ctx.env.PADOS_CLI_DEV_APP) return { runtime: activeRuntime(ctx.home, ctx.env), installed: false };
  const pkg = resolveRuntimePackage({ env: ctx.env });
  const free = freeBytes(ctx.home);
  const needed = pkg.manifest.files.bytes + (minFree || 0);
  if (free < needed) {
    throw new PadosError('INSUFFICIENT_DISK', {
      cause: `Not enough free disk space: ${humanBytes(free)} free, ${humanBytes(needed)} required (runtime ${humanBytes(pkg.manifest.files.bytes)} + reserve ${humanBytes(minFree || 0)}).`,
      details: { free, needed },
    });
  }
  ensureLayout(ctx.home);
  const result = installRuntime(ctx.home, pkg);
  const current = readCurrent(ctx.home);
  if (!current || current.version !== pkg.version) {
    writeCurrent(ctx.home, {
      schema: 'pados.cli.runtime-current.v1', version: pkg.version,
      previous: current && current.version ? current.version : null,
      history: [...((current && current.history) || []), { version: pkg.version, activated_at: new Date().toISOString(), by: 'init' }],
    });
  }
  return { runtime: runtimeAt(result.dir, pkg.version), installed: result.installed, bytes: pkg.manifest.files.bytes };
}

function writeConfiguration(ctx, existing) {
  const paths = layout(ctx.home);
  if (existing && !ctx.flags.reconfigure) return { written: false, file: paths.configFile };
  const entries = buildConfig(ctx.home, {
    dbHost: ctx.flags.dbHost, dbPort: ctx.flags.dbPort, dbUser: ctx.flags.dbUser, dbName: ctx.flags.dbName,
    dbPasswordRef: ctx.flags.dbPasswordRef, dbPassfile: ctx.flags.dbPassfile, apiPort: ctx.flags.apiPort,
    dbPassword: ctx.flags.dbPassword,
  });
  const controlTokenHash = ensureControlToken(ctx.home, { rotate: Boolean(ctx.flags.rotateControlToken) });
  for (const sub of ['workspace', 'objects']) fs.mkdirSync(path.join(paths.data, sub), { recursive: true });
  return { written: true, file: writeConfig(ctx.home, entries, { force: true, controlTokenHash }) };
}

function python(ctx, runtime, args, options) {
  return runPython(runtime, ctx.home, args, { env: ctx.env, ...options });
}

function configureDatabase(ctx, runtime) {
  if (ctx.flags.skipDatabase) return { status: 'SKIPPED' };
  const check = python(ctx, runtime, ['migrate', '--check']).json;
  const states = new Set((check.lanes || []).map((lane) => lane.state));
  if (check.error) return { status: 'BLOCKED', code: check.error === 'DATABASE_DRIVER_UNAVAILABLE' ? check.error : 'DATABASE_TARGET_REFUSED', check };
  if (states.has('UNREACHABLE')) return { status: 'BLOCKED', code: 'DATABASE_UNREACHABLE', check };
  if (states.has('AUTHENTICATION_FAILED')) return { status: 'BLOCKED', code: 'DATABASE_AUTHENTICATION_FAILED', check };
  if (states.has('INCOMPATIBLE')) return { status: 'BLOCKED', code: 'MIGRATIONS_INCOMPATIBLE', check };
  if (states.has('DATABASE_MISSING') && !ctx.flags.createDatabases) return { status: 'BLOCKED', code: 'DATABASE_MISSING', check };
  if (check.status === 'CURRENT') return { status: 'CURRENT', check };
  const args = ['migrate', ...(ctx.flags.createDatabases ? ['--create-databases'] : [])];
  const applied = python(ctx, runtime, args, { timeoutMs: 900000 }).json;
  return { status: applied.status, code: applied.status === 'CURRENT' ? undefined : (applied.code || 'MIGRATIONS_PENDING'), applied };
}

async function run(ctx) {
  if (ctx.flags.dbPassword !== undefined) throw new PadosError('SECRET_REFUSED');
  const paths = layout(ctx.home);
  const existing = fs.existsSync(paths.configFile);
  const previous = readCurrent(ctx.home);
  const storage = await chooseStorage(ctx.flags, ctx.interactive);
  const minFree = parseMinFree(ctx.flags.minFreeBytes);
  const report = { home: ctx.home, existing_installation: existing, previous_runtime: previous && previous.version };
  if (existing && !ctx.json) write(`Existing PADOS installation found at ${ctx.home}; configuration and data are kept.`);

  // Runtime package resolution + disk check happen before anything is written.
  const plan = { storage, reason: storage === 'FULL_LOCAL' ? 'FULL_LOCAL keeps artifacts locally' : 'runtime size', runtimeBytes: 0 };
  if (!(ctx.env.PADOS_CLI_DEV_PYTHON && ctx.env.PADOS_CLI_DEV_APP)) {
    plan.runtimeBytes = resolveRuntimePackage({ env: ctx.env }).manifest.files.bytes;
  }
  await confirmDisk(ctx, plan);
  const runtimeResult = installPlatformRuntime(ctx, minFree);
  ensureLayout(ctx.home);
  report.runtime = { version: runtimeResult.runtime.version, newly_installed: runtimeResult.installed, bytes: runtimeResult.bytes };
  report.config = writeConfiguration(ctx, existing);

  const profileExists = fs.existsSync(paths.storageProfile);
  if (!profileExists || ctx.flags.reconfigure || ctx.flags.storage || minFree !== undefined) {
    const args = ['storage-profile', '--mode', storage, ...(minFree !== undefined ? ['--min-free-bytes', String(minFree)] : [])];
    const profile = python(ctx, runtimeResult.runtime, args);
    if (profile.status !== 0) throw new PadosError('RUNTIME_COMMAND_FAILED', { details: profile.json });
    report.storage_profile = profile.json.profile;
  }
  const validation = python(ctx, runtimeResult.runtime, ['validate-config']);
  if (validation.status !== 0) {
    throw new PadosError('CONFIG_INVALID', { details: validation.json, action: 'pados doctor; fix the configuration file or re-run pados init --reconfigure' });
  }
  report.database = configureDatabase(ctx, runtimeResult.runtime);
  const doctor = python(ctx, runtimeResult.runtime, ['doctor']).json;
  report.doctor = doctor;
  if (ctx.json) {
    write(JSON.stringify(report, null, 2));
  } else {
    write(`PADOS runtime ${report.runtime.version} ${report.runtime.newly_installed ? 'installed' : 'already installed'} in ${ctx.home}`);
    write(`Configuration: ${report.config.file}${report.config.written ? '' : ' (kept)'}`);
    write(`Storage mode: ${storage}`);
    write(`Database: ${report.database.status}${report.database.code ? ` (${report.database.code})` : ''}`);
    write('');
    write(renderDoctor(doctor));
    write('');
    write(doctor.status === 'BLOCKED'
      ? 'PADOS is installed but not ready. Fix the BLOCKED items above, then run: pados doctor'
      : 'Next: pados up');
  }
  return 0;
}

module.exports = { SPEC, run, STORAGE_MODES, freeBytes, ask };
