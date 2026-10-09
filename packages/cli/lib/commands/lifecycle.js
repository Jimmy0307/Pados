'use strict';
// pados doctor / up / down / status: thin launchers over the PADOS runtime's own checks and Core's API.

const fs = require('node:fs');
const { PadosError } = require('../errors');
const { layout } = require('../home');
const { activeRuntime, runPython, tail } = require('../runtime');
const { effectiveEndpoint } = require('../config');
const { renderDoctor, write } = require('../output');
const services = require('../services');

const LANE_BLOCKERS = [
  ['UNREACHABLE', 'DATABASE_UNREACHABLE'], ['AUTHENTICATION_FAILED', 'DATABASE_AUTHENTICATION_FAILED'],
  ['DATABASE_MISSING', 'DATABASE_MISSING'], ['INCOMPATIBLE', 'MIGRATIONS_INCOMPATIBLE'],
  ['UNINITIALIZED', 'MIGRATIONS_PENDING'], ['UPGRADE_AVAILABLE', 'MIGRATIONS_PENDING'],
];

function requireInitialized(home) {
  if (!fs.existsSync(layout(home).configFile)) throw new PadosError('NOT_INITIALIZED');
}

function doctorBlockedWithoutRuntime(home, error) {
  return {
    schema: 'pados.install.doctor.v1', status: 'BLOCKED', home,
    areas: [{ area: 'core', status: 'BLOCKED', code: error.code, summary: error.message, action: error.action }],
  };
}

async function doctor(ctx) {
  let report;
  let runtime = null;
  try {
    runtime = activeRuntime(ctx.home, ctx.env);
  } catch (error) {
    if (!(error instanceof PadosError)) throw error;
    report = doctorBlockedWithoutRuntime(ctx.home, error);
  }
  if (runtime) {
    const args = ['doctor', ...(ctx.flags.noDevice ? ['--no-device'] : [])];
    report = runPython(runtime, ctx.home, args, { env: ctx.env }).json;
    if (report.ok === false) report = doctorBlockedWithoutRuntime(ctx.home, new PadosError(report.code || 'RUNTIME_COMMAND_FAILED'));
    const state = services.readState(ctx.home);
    report.services = state ? { api: { pid: state.pid, owned_and_running: services.isOwned(state), endpoint: state.endpoint } } : { api: null };
  }
  write(ctx.json ? JSON.stringify(report, null, 2) : renderDoctor(report));
  return report.status === 'BLOCKED' ? 1 : 0;
}

function laneBlocker(check) {
  if (check.error) return new PadosError(check.error === 'DATABASE_DRIVER_UNAVAILABLE' ? check.error : 'DATABASE_TARGET_REFUSED', { details: check });
  const states = new Set((check.lanes || []).map((lane) => lane.state));
  for (const [state, code] of LANE_BLOCKERS) if (states.has(state)) return new PadosError(code, { details: check });
  return null;
}

async function up(ctx) {
  requireInitialized(ctx.home);
  const runtime = activeRuntime(ctx.home, ctx.env);
  const endpoint = effectiveEndpoint(ctx.home, ctx.env);
  const existing = services.readState(ctx.home);
  if (existing && services.isOwned(existing)) {
    const health = await services.getJson(`${existing.endpoint}/health`);
    const result = { status: 'ALREADY_RUNNING', pid: existing.pid, endpoint: existing.endpoint, health: health.json };
    write(ctx.json ? JSON.stringify(result, null, 2) : `PADOS is already running (pid ${existing.pid}). UI: ${existing.endpoint}/ui/`);
    return 0;
  }
  if (existing) services.clearState(ctx.home);
  const check = runPython(runtime, ctx.home, ['migrate', '--check'], { env: ctx.env }).json;
  if (check.ok === false) throw new PadosError(check.code === 'CONFIG_MISSING' ? 'NOT_INITIALIZED' : 'CONFIG_INVALID', { details: check });
  const blocker = laneBlocker(check);
  if (blocker) throw blocker;
  if (!(await services.portIsFree(endpoint.host, endpoint.port))) {
    throw new PadosError('PORT_IN_USE', { cause: `Port ${endpoint.port} on ${endpoint.host} is already in use.` });
  }
  const state = services.startApi(ctx.home, runtime, endpoint, ctx.env);
  const timeoutMs = Number(ctx.flags.timeout || 90) * 1000;
  const waited = await services.waitHealthy(state, { timeoutMs });
  if (!waited.healthy) {
    const stderr = fs.existsSync(state.logs.stderr) ? tail(fs.readFileSync(state.logs.stderr, 'utf8')) : '';
    if (!waited.exited) await services.stopApi(ctx.home).catch(() => undefined);
    services.clearState(ctx.home);
    throw new PadosError(waited.exited ? 'START_FAILED' : 'HEALTH_TIMEOUT', { details: { log: state.logs.stderr, stderr_tail: stderr } });
  }
  const result = { status: 'RUNNING', pid: state.pid, endpoint: state.endpoint, ui: `${state.endpoint}/ui/`,
    api_docs: `${state.endpoint}/docs`, runtime_version: state.runtime_version, logs: state.logs };
  if (ctx.json) write(JSON.stringify(result, null, 2));
  else {
    write(`PADOS is running (pid ${state.pid}, runtime ${state.runtime_version}).`);
    write(`UI:  ${result.ui}`);
    write(`API: ${result.api_docs}`);
    write('Stop with: pados down');
  }
  return 0;
}

async function down(ctx) {
  let result;
  try {
    result = await services.stopApi(ctx.home);
  } catch (error) {
    if (error instanceof PadosError && error.code === 'NOT_RUNNING') {
      write(ctx.json ? JSON.stringify({ status: 'NOT_RUNNING' }) : 'PADOS is not running (nothing to stop).');
      return 0;
    }
    throw error;
  }
  const status = result.stale ? 'STALE_RECORD_CLEARED' : 'STOPPED';
  write(ctx.json ? JSON.stringify({ status, ...result }, null, 2)
    : (result.stale ? `No PADOS process was running (stale record for pid ${result.pid} cleared).` : `PADOS stopped (pid ${result.pid}).`));
  return 0;
}

async function status(ctx) {
  const state = services.readState(ctx.home);
  const owned = state ? services.isOwned(state) : false;
  const result = { home: ctx.home, initialized: fs.existsSync(layout(ctx.home).configFile), running: owned };
  if (state) {
    result.pid = state.pid;
    result.endpoint = state.endpoint;
    result.runtime_version = state.runtime_version;
    result.started_at = state.started_at;
  }
  if (owned) {
    const health = await services.getJson(`${state.endpoint}/health`);
    const ready = await services.getJson(`${state.endpoint}/ready`);
    result.health = health.json;
    result.ready = ready.ok ? { http_status: ready.status, body: ready.json } : { error: ready.error };
  }
  if (ctx.json) write(JSON.stringify(result, null, 2));
  else if (!owned) write(state ? `PADOS is not running (stale record for pid ${state.pid}).` : 'PADOS is not running.');
  else {
    const db = result.health && result.health.database ? result.health.database.status : 'unknown';
    const readiness = result.ready.http_status === 200 ? 'ready' : 'not fully ready (see pados doctor)';
    write(`PADOS is running (pid ${state.pid}) at ${state.endpoint}`);
    write(`Health: ${result.health ? result.health.status : 'unreachable'}; database: ${db}; readiness: ${readiness}`);
    write(`UI: ${state.endpoint}/ui/`);
  }
  return owned ? 0 : 3;
}

module.exports = { doctor, up, down, status, laneBlocker, requireInitialized };
