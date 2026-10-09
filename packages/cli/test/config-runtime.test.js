'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { buildConfig, writeConfig, ensureControlToken, readConfigValues, effectiveEndpoint, assertNoSecrets } = require('../lib/config');
const { ensureLayout } = require('../lib/home');
const { installRuntime, readCurrent, writeCurrent, treeDigest, verifyInstalled } = require('../lib/runtime');
const { tempDir, fakeRuntimePackage } = require('./helpers');

function home() {
  const dir = path.join(tempDir(), 'home');
  ensureLayout(dir);
  return dir;
}

test('config uses Core PADOS_* keys, loopback API and a SecretRef instead of a password', () => {
  const dir = home();
  const values = Object.fromEntries(buildConfig(dir, {}));
  assert.equal(values.PADOS_API_HOST, '127.0.0.1');
  assert.equal(values.PADOS_DB_PASSWORD, '');
  assert.equal(values.PADOS_DB_PASSWORD_REF, 'secretref://env/PADOS_DB_PASSWORD');
  assert.equal(values.PADOS_DB_NAME, values.PADOS_OPERATIONAL_DATABASE_NAME);
  assert.notEqual(values.PADOS_CORPUS_DATABASE_NAME, values.PADOS_DB_NAME);
  assert.ok(values.PADOS_PRODUCT_OBJECT_ROOT.startsWith(path.join(dir, 'data')));
});

test('credentials and secret-shaped values are refused', () => {
  const dir = home();
  assert.throws(() => buildConfig(dir, { dbPassword: 'hunter2' }), (e) => e.code === 'SECRET_REFUSED');
  assert.throws(() => buildConfig(dir, { dbUser: 'ghp_abcdefghijklmnop' }), (e) => e.code === 'SECRET_REFUSED');
  assert.throws(() => buildConfig(dir, { dbPasswordRef: 'plain-password' }), (e) => e.code === 'CONFIG_INVALID');
  assert.throws(() => assertNoSecrets('PADOS_DB_PASSWORD=x\n'), (e) => e.code === 'SECRET_REFUSED');
});

test('invalid values are rejected before anything is written', () => {
  const dir = home();
  assert.throws(() => buildConfig(dir, { apiPort: '80' }), (e) => e.code === 'CONFIG_INVALID');
  assert.throws(() => buildConfig(dir, { apiPort: 'abc' }), (e) => e.code === 'CONFIG_INVALID');
  assert.throws(() => buildConfig(dir, { dbName: 'Bad-Name' }), (e) => e.code === 'CONFIG_INVALID');
  assert.throws(() => buildConfig(dir, { apiHost: '0.0.0.0' }), (e) => e.code === 'CONFIG_INVALID');
  assert.throws(() => buildConfig(dir, { dbHost: 'a\nPADOS_X=1' }), (e) => e.code === 'CONFIG_INVALID');
});

test('config and control token are owner-only; only the token hash is in the config', () => {
  const dir = home();
  const hash = ensureControlToken(dir);
  const file = writeConfig(dir, buildConfig(dir, { apiPort: 18088 }), { controlTokenHash: hash });
  const text = fs.readFileSync(file, 'utf8');
  const controlValue = fs.readFileSync(path.join(dir, 'config', 'control.token'), 'utf8').trim();
  assert.ok(!text.includes(controlValue));
  assert.equal(crypto.createHash('sha256').update(controlValue).digest('hex').toUpperCase(), hash);
  assert.match(text, new RegExp(`PADOS_CONTROL_API_TOKEN_HASH=${hash}`));
  assert.match(text, /^PADOS_DB_PASSWORD=$/m);
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.equal(fs.statSync(path.join(dir, 'config', 'control.token')).mode & 0o777, 0o600);
  }
  assert.equal(ensureControlToken(dir), hash, 'existing token is kept');
  assert.throws(() => writeConfig(dir, buildConfig(dir, {})), (e) => e.code === 'CONFIG_EXISTS');
  assert.equal(readConfigValues(dir).PADOS_API_PORT, '18088');
  assert.equal(effectiveEndpoint(dir, {}).base, 'http://127.0.0.1:18088');
  assert.equal(effectiveEndpoint(dir, { PADOS_API_PORT: '19000' }).port, 19000, 'environment wins like Core');
});

test('runtime install copies, verifies and records the active version', () => {
  const dir = home();
  const pkg = fakeRuntimePackage(tempDir());
  const first = installRuntime(dir, { ...pkg, runtimeDir: pkg.runtimeDir });
  assert.equal(first.installed, true);
  assert.equal(verifyInstalled(first.dir).files.digest, pkg.manifest.files.digest);
  assert.equal(installRuntime(dir, pkg).installed, false, 'same verified version is not copied twice');
  writeCurrent(dir, { version: pkg.version, previous: null, history: [] });
  assert.equal(readCurrent(dir).version, pkg.version);
  assert.equal(treeDigest(first.dir).digest, pkg.manifest.files.digest, 'marker files are outside the digest');
});

test('a tampered runtime package is refused and leaves no partial install', () => {
  const dir = home();
  const pkg = fakeRuntimePackage(tempDir());
  fs.writeFileSync(path.join(pkg.runtimeDir, 'app', 'src', 'marker.txt'), 'tampered');
  assert.throws(() => installRuntime(dir, pkg), (e) => e.code === 'RUNTIME_PACKAGE_INVALID');
  assert.deepEqual(fs.readdirSync(path.join(dir, 'runtime')), []);
});
