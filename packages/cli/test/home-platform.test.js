'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { spawnSync } = require('node:child_process');
const { resolveHome, ensureLayout, LAYOUT, restrictToOwner } = require('../lib/home');
const { platformKey, runtimePackageFor, resolveRuntimePackage, RUNTIME_PACKAGES } = require('../lib/platform');
const { PadosError } = require('../lib/errors');
const { tempDir, fakeRuntimePackage } = require('./helpers');

const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'home-vectors.json'), 'utf8')).vectors;

for (const vector of vectors) {
  test(`PADOS_HOME vector: ${vector.name}`, () => {
    if (vector.error) {
      assert.throws(() => resolveHome(vector.platform, vector.env, vector.user_home), (error) => error.code === vector.error);
    } else {
      assert.equal(resolveHome(vector.platform, vector.env, vector.user_home), vector.expect);
    }
  });
}

test('layout is created with an owner-only config directory', () => {
  const home = path.join(tempDir(), 'home');
  ensureLayout(home);
  for (const name of LAYOUT) assert.ok(fs.statSync(path.join(home, name)).isDirectory(), name);
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(path.join(home, 'config')).mode & 0o777, 0o700);
    assert.equal(fs.statSync(home).mode & 0o777, 0o700);
  } else {
    const acl = spawnSync('icacls', [path.join(home, 'config')], { encoding: 'utf8' }).stdout;
    assert.doesNotMatch(acl, /BUILTIN\\Users|Everyone|Authenticated Users/);
  }
});

// R-6: the whole home is owner-only, including an existing home whose subdirectories predate the ACL.
function aclEntries(target) {
  const out = spawnSync('icacls', [target], { encoding: 'utf8' }).stdout;
  return out.split(/\r?\n/).map((line, index) => (index === 0 ? line.slice(target.length) : line).trim())
    .filter((line) => /^[^:]+:\(/.test(line));
}

test('whole PADOS_HOME is owner-only on Windows (root, data, logs, state, runtime, cache)', { skip: process.platform !== 'win32' }, () => {
  const home = path.join(tempDir(), 'home');
  fs.mkdirSync(path.join(home, 'data', 'objects'), { recursive: true }); // pre-existing content inherits the old ACL
  fs.writeFileSync(path.join(home, 'data', 'objects', 'x.bin'), 'x');
  ensureLayout(home);
  const me = require('node:os').userInfo().username.toLowerCase();
  for (const target of [home, ...['data', 'logs', 'state', 'runtime', 'cache', 'config'].map((n) => path.join(home, n)),
    path.join(home, 'data', 'objects', 'x.bin')]) {
    const entries = aclEntries(target);
    assert.ok(entries.length >= 1, `${target}: no ACL entries parsed`);
    for (const entry of entries) {
      const principal = entry.slice(0, entry.indexOf(':(')).split('\\').pop().toLowerCase();
      assert.equal(principal, me, `${target}: foreign ACL entry ${entry}`);
    }
  }
});

test('whole PADOS_HOME is 0700 on POSIX', { skip: process.platform === 'win32' }, () => {
  const home = path.join(tempDir(), 'home');
  ensureLayout(home);
  assert.equal(fs.statSync(home).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(home, 'config')).mode & 0o777, 0o700);
});

test('restrictToOwner on files is 0600 on POSIX', { skip: process.platform === 'win32' }, () => {
  const file = path.join(tempDir(), 'f');
  fs.writeFileSync(file, 'x', { mode: 0o644 });
  restrictToOwner(file);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('platform keys select exactly one runtime package', () => {
  assert.equal(platformKey('win32', 'x64'), 'win32-x64');
  assert.equal(runtimePackageFor('linux', 'x64').name, '@pados-ai/runtime-linux-x64');
  assert.equal(runtimePackageFor('darwin', 'arm64').status, 'design-ready');
  assert.throws(() => runtimePackageFor('linux', 'arm64'), (e) => e.code === 'PLATFORM_UNSUPPORTED');
  assert.throws(() => runtimePackageFor('freebsd', 'x64'), (e) => e.code === 'PLATFORM_UNSUPPORTED');
  assert.deepEqual(Object.keys(RUNTIME_PACKAGES).sort(), ['darwin-arm64', 'linux-x64', 'win32-x64']);
});

test('runtime package resolves by platform, arch and exact version', () => {
  const root = tempDir();
  const pkg = fakeRuntimePackage(root);
  const resolved = resolveRuntimePackage({ cliVersion: '0.1.0-alpha.1', platform: 'linux', arch: 'x64', env: {}, searchFrom: [root] });
  assert.equal(resolved.dir, pkg.dir);
  assert.equal(resolved.manifest.files.digest, pkg.manifest.files.digest);
  assert.throws(
    () => resolveRuntimePackage({ cliVersion: '0.1.0-alpha.2', platform: 'linux', arch: 'x64', env: {}, searchFrom: [root] }),
    (e) => e instanceof PadosError && e.code === 'RUNTIME_VERSION_MISMATCH');
  assert.throws(
    () => resolveRuntimePackage({ cliVersion: '0.1.0-alpha.1', platform: 'win32', arch: 'x64', env: {}, searchFrom: [root] }),
    (e) => e.code === 'RUNTIME_PACKAGE_MISSING');
  const override = resolveRuntimePackage({ cliVersion: '0.1.0-alpha.1', platform: 'linux', arch: 'x64', env: { PADOS_RUNTIME_PACKAGE_DIR: pkg.dir }, searchFrom: [] });
  assert.equal(override.dir, pkg.dir);
});

test('runtime package whose manifest names another platform is rejected', () => {
  const root = tempDir();
  fakeRuntimePackage(root, { platform: 'win32-x64' });
  assert.throws(
    () => resolveRuntimePackage({ cliVersion: '0.1.0-alpha.1', platform: 'linux', arch: 'x64', env: {}, searchFrom: [root] }),
    (e) => e.code === 'RUNTIME_PACKAGE_INVALID');
});
