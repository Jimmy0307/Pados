'use strict';
// Install acceptance from local `npm pack` tarballs into a temporary npm prefix and temporary PADOS_HOME.
// Runs only when PADOS_ACCEPTANCE_TARBALLS names the pack output directory. With PADOS_ACCEPTANCE_DATABASE=1
// (a disposable PostgreSQL reachable through PADOS_DB_HOST/PADOS_DB_PORT, password in PADOS_DB_PASSWORD)
// it also exercises migrations, up/down, restart persistence, update (including the explicit consent a forward
// migration needs) and rollback. Nothing is published.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const httpModule = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { spawnSync } = require('node:child_process');

const TARBALLS = process.env.PADOS_ACCEPTANCE_TARBALLS ? path.resolve(process.env.PADOS_ACCEPTANCE_TARBALLS) : null;
const WITH_DATABASE = process.env.PADOS_ACCEPTANCE_DATABASE === '1';
const SKIP = TARBALLS ? false : 'PADOS_ACCEPTANCE_TARBALLS not set';
const PLATFORM = `${process.platform}-${process.arch}`;
const V1 = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'package.json'), 'utf8')).version;
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), 'pados-accept-'));
const DB_PASSWORD_VALUE = process.env.PADOS_DB_PASSWORD || '';

function tarball(kind, version) {
  return path.join(TARBALLS, kind === 'cli' ? `pados-ai-cli-${version}.tgz` : `pados-ai-runtime-${PLATFORM}-${version}.tgz`);
}

function otherVersion() {
  const names = fs.readdirSync(TARBALLS).filter((n) => n.startsWith('pados-ai-cli-') && n.endsWith('.tgz'));
  const versions = names.map((n) => n.slice('pados-ai-cli-'.length, -'.tgz'.length)).filter((v) => v !== V1);
  return versions[0] || null;
}

function npmInstall(prefix, files) {
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const result = spawnSync(npm, ['install', '-g', '--prefix', prefix, '--offline', '--no-audit', '--no-fund', ...files],
    { encoding: 'utf8', shell: process.platform === 'win32', timeout: 900000 });
  assert.equal(result.status, 0, result.stderr);
}

function padosBin(prefix) {
  return process.platform === 'win32' ? path.join(prefix, 'node_modules', '@pados-ai', 'cli', 'bin', 'pados.js')
    : path.join(prefix, 'lib', 'node_modules', '@pados-ai', 'cli', 'bin', 'pados.js');
}

function pados(prefix, home, args, extraEnv = {}) {
  const env = { ...process.env, PADOS_HOME: home, ...extraEnv };
  for (const key of Object.keys(env)) if (key.startsWith('PADOS_CLI_DEV_')) delete env[key];
  delete env.PADOS_RUNTIME_PACKAGE_DIR;
  return spawnSync(process.execPath, [padosBin(prefix), ...args], { encoding: 'utf8', env, timeout: 900000 });
}

function json(result) {
  return JSON.parse(result.stdout);
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

// No connection reuse: the server is restarted between requests (a pooled keep-alive socket would be stale).
function http(method, url, body) {
  const payload = body ? JSON.stringify(body) : null;
  return new Promise((resolve, reject) => {
    const request = httpModule.request(url, {
      method, agent: false,
      headers: payload ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } : {},
    }, (response) => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { text += chunk; });
      response.on('end', () => {
        let parsed = null;
        try { parsed = JSON.parse(text); } catch { parsed = null; }
        resolve({ status: response.statusCode, json: parsed });
      });
    });
    request.on('error', reject);
    if (payload) request.write(payload);
    request.end();
  });
}

function noSecretIn(...texts) {
  if (!DB_PASSWORD_VALUE) return;
  for (const text of texts) assert.ok(!String(text).includes(DB_PASSWORD_VALUE), 'database password leaked into output');
}

// Runs a short script with the installed runtime's own interpreter and psycopg (the PADOS lane databases only;
// connects with the PADOS_DB_* environment of this test, maintenance database "postgres").
function runtimeSql(home, version, script, args) {
  const runtimeDir = path.join(home, 'runtime', version);
  const manifest = JSON.parse(fs.readFileSync(path.join(runtimeDir, 'app', 'runtime-manifest.json'), 'utf8'));
  const python = path.join(runtimeDir, ...manifest.python_executable.split('/'));
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1' };
  const result = spawnSync(python, ['-I', '-B', '-c', script, ...args], { encoding: 'utf8', env, timeout: 120000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

const CONNECT = 'import os, sys, psycopg\n'
  + 'def connect(name):\n'
  + '    return psycopg.connect(host=os.environ.get("PADOS_DB_HOST", "127.0.0.1"), port=int(os.environ.get("PADOS_DB_PORT", "5432")),\n'
  + '                           user=os.environ.get("PADOS_DB_USER", "pados"), password=os.environ.get("PADOS_DB_PASSWORD", ""),\n'
  + '                           dbname=name, autocommit=True)\n';
const CREATE_EMPTY = `${CONNECT}with connect("postgres") as c:\n    for name in sys.argv[1:]:\n        c.execute("CREATE DATABASE " + name)\n`;
const ALEMBIC_STATE = `${CONNECT}out = []\nfor name in sys.argv[1:]:\n    with connect(name) as c:\n`
  + '        present = c.execute("select to_regclass(\'public.alembic_version\') is not null").fetchone()[0]\n'
  + '        out.append(name + "=" + ("migrated" if present else "empty"))\nprint(",".join(out))\n';

const prefix = path.join(WORK, 'prefix');

test('runtime package missing: clear message, nothing written', { skip: SKIP }, () => {
  const bare = path.join(WORK, 'prefix-cli-only');
  npmInstall(bare, [tarball('cli', V1)]);
  const home = path.join(WORK, 'home-missing');
  const result = pados(bare, home, ['init', '--non-interactive', '--json']);
  assert.equal(result.status, 1);
  assert.equal(json(result).code, 'RUNTIME_PACKAGE_MISSING');
  assert.equal(fs.existsSync(path.join(home, 'config', 'pados.env')), false);
});

test('fresh install from local tarballs (offline)', { skip: SKIP }, () => {
  npmInstall(prefix, [tarball('runtime', V1), tarball('cli', V1)]);
  const version = json(pados(prefix, path.join(WORK, 'home-version'), ['version', '--json']));
  assert.equal(version.cli, V1);
  assert.equal(version.runtime_package.version, V1);
});

test('insufficient disk (configured reserve) refuses before writing', { skip: SKIP }, () => {
  const home = path.join(WORK, 'home-disk');
  const result = pados(prefix, home, ['init', '--non-interactive', '--min-free-bytes', String(2 ** 52), '--json']);
  assert.equal(result.status, 1);
  assert.equal(json(result).code, 'INSUFFICIENT_DISK');
  assert.equal(fs.existsSync(path.join(home, 'runtime')), false);
});

test('database unavailable: init completes, doctor BLOCKED, up explains', { skip: SKIP }, async () => {
  const home = path.join(WORK, 'home-nodb');
  const dbPort = await freePort();
  const init = pados(prefix, home, ['init', '--non-interactive', '--db-host', '127.0.0.1', '--db-port', String(dbPort), '--api-port', String(await freePort()), '--json'],
    { PADOS_DB_HOST: '127.0.0.1', PADOS_DB_PORT: String(dbPort) });
  assert.equal(init.status, 0, init.stderr);
  const report = json(init);
  assert.equal(report.database.code, 'DATABASE_UNREACHABLE');
  assert.equal(report.doctor.status, 'BLOCKED');
  noSecretIn(init.stdout, fs.readFileSync(path.join(home, 'config', 'pados.env'), 'utf8'));
  if (process.platform !== 'win32') assert.equal(fs.statSync(path.join(home, 'config', 'pados.env')).mode & 0o777, 0o600);
  const up = pados(prefix, home, ['up'], { PADOS_DB_HOST: '127.0.0.1', PADOS_DB_PORT: String(dbPort) });
  assert.equal(up.status, 1);
  assert.match(up.stderr, /^PADOS could not start\.\nCause: PostgreSQL is not reachable\.\nSuggested action: pados doctor/);
  assert.doesNotMatch(up.stderr, /Traceback|\n\s+at /);

  const config = path.join(home, 'config', 'pados.env');
  fs.writeFileSync(config, fs.readFileSync(config, 'utf8').replace(/^PADOS_API_PORT=.*$/m, 'PADOS_API_PORT=not-a-port'));
  const doctor = json(pados(prefix, home, ['doctor', '--json', '--no-device'], { PADOS_DB_PORT: String(dbPort) }));
  const configuration = doctor.areas.find((a) => a.area === 'configuration');
  assert.equal(configuration.code, 'CONFIG_INVALID');
  assert.deepEqual(configuration.details.fields, ['PADOS_API_PORT']);
});

test('database lifecycle: init, up, restart persistence, update, rollback', { skip: SKIP || (!WITH_DATABASE && 'PADOS_ACCEPTANCE_DATABASE not set') }, async () => {
  const home = path.join(WORK, 'home-db');
  const apiPort = await freePort();
  const name = `pados_accept_${process.pid}`;
  const init = pados(prefix, home, ['init', '--non-interactive', '--storage', 'LIGHTWEIGHT', '--db-name', name, '--api-port', String(apiPort), '--create-databases', '--json']);
  assert.equal(init.status, 0, init.stderr);
  assert.equal(json(init).database.status, 'CURRENT');
  noSecretIn(init.stdout);
  const base = `http://127.0.0.1:${apiPort}`;
  assert.equal(pados(prefix, home, ['up', '--json']).status, 0);
  try {
    assert.equal((await http('GET', `${base}/health`)).json.status, 'ok');
    assert.equal((await http('GET', `${base}/ui/`)).status, 200);
    const created = await http('POST', `${base}/api/graph/workspaces`, { name: 'acceptance-persisted', description: 'survives restart' });
    assert.equal(created.status, 201);
  } finally {
    assert.equal(pados(prefix, home, ['down', '--json']).status, 0);
  }
  assert.equal(pados(prefix, home, ['status', '--json']).status, 3);
  assert.equal(pados(prefix, home, ['up', '--json']).status, 0);
  const listed = await http('GET', `${base}/api/graph/workspaces`);
  assert.ok(listed.json.workspaces.some((w) => w.name === 'acceptance-persisted'));
  assert.equal(json(pados(prefix, home, ['update', '--json'])).code, 'SERVICES_RUNNING');
  assert.equal(pados(prefix, home, ['down', '--json']).status, 0);

  const v2 = otherVersion();
  if (!v2) return;
  // A second installation whose databases exist but are not migrated yet (forward migration pending on update).
  const pendingHome = path.join(WORK, 'home-pending');
  const pendingName = `pados_accept_${process.pid}_pending`;
  const pendingDbs = [pendingName, `${pendingName}_corpus`];
  const pendingInit = pados(prefix, pendingHome, ['init', '--non-interactive', '--storage', 'LIGHTWEIGHT', '--db-name', pendingName, '--api-port', String(await freePort()), '--skip-database', '--json']);
  assert.equal(pendingInit.status, 0, pendingInit.stderr);
  runtimeSql(pendingHome, V1, CREATE_EMPTY, pendingDbs);

  npmInstall(prefix, [tarball('runtime', v2), tarball('cli', v2)]);
  const updated = json(pados(prefix, home, ['update', '--json']));
  assert.equal(updated.status, 'UPDATED'); // schema already current: nothing to migrate, no consent needed
  assert.equal(updated.migration, 'CURRENT');
  assert.equal(updated.to, v2);

  // R-3: a forward migration is never applied without --migrate / --yes (non-interactive: refused, data untouched).
  const refused = pados(prefix, pendingHome, ['update', '--json']);
  assert.equal(refused.status, 1);
  assert.equal(json(refused).code, 'MIGRATION_CONSENT_REQUIRED');
  assert.match(json(refused).action, /pg_dump/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(pendingHome, 'runtime', 'current.json'), 'utf8')).version, V1, 'previous runtime stays active');
  assert.equal(runtimeSql(pendingHome, V1, ALEMBIC_STATE, pendingDbs), pendingDbs.map((n) => `${n}=empty`).join(','), 'database untouched');
  const plain = pados(prefix, pendingHome, ['update']);
  assert.match(plain.stderr, /^PADOS could not be updated\.\nCause: This update must migrate the database forward/);
  const consented = json(pados(prefix, pendingHome, ['update', '--migrate', '--json']));
  assert.equal(consented.status, 'UPDATED');
  assert.equal(consented.migration, 'CURRENT');
  assert.equal(runtimeSql(pendingHome, v2, ALEMBIC_STATE, pendingDbs), pendingDbs.map((n) => `${n}=migrated`).join(','));
  assert.ok(fs.existsSync(path.join(home, 'runtime', V1)), 'previous runtime kept');
  assert.equal(pados(prefix, home, ['up', '--json']).status, 0);
  try {
    const after = await http('GET', `${base}/api/graph/workspaces`);
    assert.ok(after.json.workspaces.some((w) => w.name === 'acceptance-persisted'), 'data survives update');
  } finally {
    pados(prefix, home, ['down', '--json']);
  }
  const rolled = json(pados(prefix, home, ['update', '--rollback', '--json']));
  assert.equal(rolled.status, 'ROLLED_BACK');
  assert.equal(rolled.to, V1);
});
