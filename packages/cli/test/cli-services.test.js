'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const test = require('node:test');
const { spawn } = require('node:child_process');
const { parseArgs } = require('../lib/args');
const { PadosError, formatFailure } = require('../lib/errors');
const services = require('../lib/services');
const { tempDir, runCli } = require('./helpers');

test('argument parser handles values, booleans and rejects unknown options', () => {
  const parsed = parseArgs(['--storage', 'STANDARD', '--json', '--db-port=5433', 'extra'], { booleans: [], values: ['storage', 'db-port'] });
  assert.deepEqual(parsed.flags, { storage: 'STANDARD', json: true, dbPort: '5433' });
  assert.deepEqual(parsed.positionals, ['extra']);
  assert.throws(() => parseArgs(['--nope']), (e) => e.code === 'USAGE');
  assert.throws(() => parseArgs(['--storage'], { values: ['storage'] }), (e) => e.code === 'USAGE');
});

test('failure messages are plain by default and detailed only in debug', () => {
  const error = new PadosError('DATABASE_UNREACHABLE', { details: { lane: 'operational' } });
  const plain = formatFailure(error, { headline: 'PADOS could not start.' });
  assert.equal(plain, 'PADOS could not start.\nCause: PostgreSQL is not reachable.\nSuggested action: pados doctor\nCode: DATABASE_UNREACHABLE');
  assert.match(formatFailure(error, { debug: true }), /Details: [\s\S]*operational[\s\S]*at /);
  const unknown = formatFailure(new TypeError('boom'), {});
  assert.doesNotMatch(unknown, /boom|at /);
});

test('up on an uninitialized home fails with guidance and no stack trace', () => {
  const result = runCli(['up'], { PADOS_HOME: path.join(tempDir(), 'home') });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /^PADOS could not start\.\nCause: PADOS has not been initialized/);
  assert.match(result.stderr, /Suggested action: pados init/);
  assert.doesNotMatch(result.stderr, /\n\s+at /);
  const debug = runCli(['up', '--debug'], { PADOS_HOME: path.join(tempDir(), 'home') });
  assert.match(debug.stderr, /\n\s+at /);
});

test('doctor without an installed runtime reports BLOCKED as JSON', () => {
  const result = runCli(['doctor', '--json'], { PADOS_HOME: path.join(tempDir(), 'home') });
  assert.equal(result.status, 1);
  const report = JSON.parse(result.stdout);
  assert.equal(report.status, 'BLOCKED');
  assert.equal(report.areas[0].code, 'RUNTIME_NOT_INSTALLED');
});

test('init without the platform runtime package explains how to fix it', () => {
  const home = path.join(tempDir(), 'home');
  const result = runCli(['init', '--non-interactive', '--json'], { PADOS_HOME: home, PADOS_RUNTIME_PACKAGE_DIR: path.join(tempDir(), 'missing') });
  assert.equal(result.status, 1);
  const body = JSON.parse(result.stdout);
  assert.equal(body.code, 'RUNTIME_PACKAGE_INVALID');
  assert.equal(fs.existsSync(path.join(home, 'config', 'pados.env')), false, 'nothing is written before the runtime is resolved');
});

test('init refuses a password on the command line', () => {
  const result = runCli(['init', '--db-password', 'x', '--non-interactive'], { PADOS_HOME: path.join(tempDir(), 'home') });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Credentials are never written to PADOS configuration/);
});

test('reserved commands and unknown commands', () => {
  for (const name of ['runtime', 'model', 'storage', 'node', 'config']) {
    const result = runCli([name, 'list'], { PADOS_HOME: path.join(tempDir(), 'home') });
    assert.equal(result.status, 3, name);
    assert.match(result.stdout, /not yet available/);
  }
  const unknown = runCli(['frobnicate']);
  assert.equal(unknown.status, 2);
  assert.match(unknown.stderr, /Unknown command "frobnicate"/);
});

test('reserved PADOS_HOME is refused', { skip: process.platform !== 'win32' }, () => {
  const result = runCli(['status'], { PADOS_HOME: 'c:\\pados' });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /reserved location/);
});

test('uninstall is a dry run by default and keeps data', () => {
  const home = path.join(tempDir(), 'home');
  for (const sub of ['runtime/0.1.0', 'data', 'config', 'cache']) fs.mkdirSync(path.join(home, sub), { recursive: true });
  const dry = JSON.parse(runCli(['uninstall', '--json'], { PADOS_HOME: home }).stdout);
  assert.equal(dry.applied, false);
  assert.ok(fs.existsSync(path.join(home, 'runtime')));
  const applied = JSON.parse(runCli(['uninstall', '--apply', '--json'], { PADOS_HOME: home }).stdout);
  assert.equal(applied.applied, true);
  assert.ok(!fs.existsSync(path.join(home, 'runtime')) && !fs.existsSync(path.join(home, 'cache')));
  assert.ok(fs.existsSync(path.join(home, 'data')) && fs.existsSync(path.join(home, 'config')));
  const plan = JSON.parse(runCli(['uninstall', '--all-data', '--apply', '--json'], { PADOS_HOME: home }).stdout);
  assert.equal(plan.mode, 'ALL_DATA_PLAN_ONLY');
  assert.equal(plan.applied, false);
  assert.ok(fs.existsSync(path.join(home, 'data')));
});

test('ownership check requires the instance id and the PADOS module on the command line', () => {
  const state = { pid: 4242, instance_id: 'abc-123' };
  assert.equal(services.isOwned(state, () => 'python -m pados.install serve --instance-id abc-123'), true);
  assert.equal(services.isOwned(state, () => 'python -m pados.install serve --instance-id other'), false);
  assert.equal(services.isOwned(state, () => 'notepad.exe abc-123'), false);
  assert.equal(services.isOwned(state, () => null), false);
});

test('port probe detects a listener', async () => {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  assert.equal(await services.portIsFree('127.0.0.1', port), false);
  await new Promise((resolve) => server.close(resolve));
  assert.equal(await services.portIsFree('127.0.0.1', port), true);
});

function sleeper(marker) {
  return spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)', 'pados.install', marker], { stdio: 'ignore' });
}

test('down stops only a process it can identify, and never a foreign pid', async () => {
  const home = path.join(tempDir(), 'home');
  const foreign = sleeper('someone-else');
  services.writeState(home, { pid: foreign.pid, instance_id: 'not-this-one', endpoint: 'http://127.0.0.1:1' });
  await assert.rejects(services.stopApi(home), (e) => e.code === 'NOT_OWNED');
  assert.equal(services.isAlive(foreign.pid), true, 'foreign process untouched');
  assert.equal(services.readState(home), null, 'stale record cleared');
  foreign.kill();

  const owned = sleeper('instance-xyz');
  await services.sleep(300);
  services.writeState(home, { pid: owned.pid, instance_id: 'instance-xyz', endpoint: 'http://127.0.0.1:1' });
  const result = await services.stopApi(home);
  assert.equal(result.stopped, true);
  assert.equal(services.isAlive(owned.pid), false);
  await assert.rejects(services.stopApi(home), (e) => e.code === 'NOT_RUNNING');
});
