'use strict';
// Background service management for `pados up/down/status`. The service is Core's API (served by
// `python -m pados.install serve`, i.e. pados.api.app). State lives in PADOS_HOME/state/services.json; a
// random instance id on the command line lets `down` verify that a pid is the process this CLI started.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { PadosError } = require('./errors');
const { layout } = require('./home');
const { childEnv, pythonArgs } = require('./runtime');

function readState(home) {
  try {
    return JSON.parse(fs.readFileSync(layout(home).services, 'utf8'));
  } catch {
    return null;
  }
}

function writeState(home, state) {
  const file = layout(home).services;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(`${file}.tmp`, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(`${file}.tmp`, file);
}

function clearState(home) {
  fs.rmSync(layout(home).services, { force: true });
}

// Command line of a live pid, or null when the process does not exist.
function commandLineOf(pid, platform = process.platform) {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  if (platform === 'linux') {
    try {
      return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').join(' ').trim();
    } catch {
      return null;
    }
  }
  if (platform === 'win32') {
    const script = `$p = Get-CimInstance Win32_Process -Filter "ProcessId=${pid}"; if ($p) { $p.CommandLine }`;
    const result = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script],
      { encoding: 'utf8', windowsHide: true, timeout: 20000 });
    const text = (result.stdout || '').trim();
    return result.status === 0 && text ? text : null;
  }
  const result = spawnSync('ps', ['-o', 'command=', '-p', String(pid)], { encoding: 'utf8' });
  const text = (result.stdout || '').trim();
  return result.status === 0 && text ? text : null;
}

function isOwned(state, lookup = commandLineOf) {
  if (!state || !state.pid || !state.instance_id) return false;
  const line = lookup(state.pid);
  return Boolean(line && line.includes(state.instance_id) && line.includes('pados.install'));
}

function portIsFree(host, port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.listen({ host: host === 'localhost' ? '127.0.0.1' : host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

function getJson(url, timeoutMs = 3000) {
  return new Promise((resolve) => {
    const request = http.get(url, { timeout: timeoutMs }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => {
        let json = null;
        try { json = JSON.parse(body); } catch { json = null; }
        resolve({ ok: true, status: response.statusCode, json });
      });
    });
    request.on('timeout', () => request.destroy(new Error('timeout')));
    request.on('error', (error) => resolve({ ok: false, error: error.code || error.message }));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === 'EPERM';
  }
}

function startApi(home, runtime, endpoint, env = process.env) {
  const paths = layout(home);
  const instanceId = crypto.randomUUID();
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const stdoutLog = path.join(paths.logs, `api-${stamp}.out.log`);
  const stderrLog = path.join(paths.logs, `api-${stamp}.err.log`);
  const out = fs.openSync(stdoutLog, 'a', 0o600);
  const err = fs.openSync(stderrLog, 'a', 0o600);
  const runEnv = childEnv(home, env);
  if (runtime.dev && env.PADOS_CLI_DEV_PYTHONPATH) runEnv.PYTHONPATH = env.PADOS_CLI_DEV_PYTHONPATH;
  const child = spawn(runtime.python, pythonArgs(runtime, ['serve', '--home', home, '--instance-id', instanceId]), {
    cwd: runtime.appDir, env: runEnv, detached: true, stdio: ['ignore', out, err], windowsHide: true,
  });
  child.unref();
  fs.closeSync(out);
  fs.closeSync(err);
  const state = {
    schema: 'pados.cli.services.v1', service: 'api', pid: child.pid, instance_id: instanceId,
    started_at: new Date().toISOString(), runtime_version: runtime.version, endpoint: endpoint.base,
    logs: { stdout: stdoutLog, stderr: stderrLog },
  };
  writeState(home, state);
  return state;
}

async function waitHealthy(state, { timeoutMs = 90000, intervalMs = 500 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    if (!isAlive(state.pid)) return { healthy: false, exited: true, last };
    last = await getJson(`${state.endpoint}/health`);
    if (last.ok && last.status === 200 && last.json && last.json.status === 'ok') return { healthy: true, last };
    await sleep(intervalMs);
  }
  return { healthy: false, exited: false, last };
}

async function stopProcess(pid, { timeoutMs = 15000, platform = process.platform } = {}) {
  if (platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, encoding: 'utf8' });
  } else {
    try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ }
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) return { stopped: true, forced: platform === 'win32' };
    await sleep(250);
  }
  try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ }
  await sleep(500);
  return { stopped: !isAlive(pid), forced: true };
}

async function stopApi(home, { lookup = commandLineOf } = {}) {
  const state = readState(home);
  if (!state) throw new PadosError('NOT_RUNNING');
  if (!isOwned(state, lookup)) {
    clearState(home);
    const alive = isAlive(state.pid);
    if (!alive) return { stopped: false, stale: true, pid: state.pid };
    throw new PadosError('NOT_OWNED', { details: { pid: state.pid } });
  }
  const result = await stopProcess(state.pid);
  if (!result.stopped) {
    throw new PadosError('INTERNAL_ERROR', { cause: `PADOS process ${state.pid} did not stop.` });
  }
  clearState(home);
  return { stopped: true, pid: state.pid, forced: result.forced };
}

module.exports = {
  readState, writeState, clearState, commandLineOf, isOwned, portIsFree, getJson, startApi, waitHealthy,
  stopApi, isAlive, sleep,
};
