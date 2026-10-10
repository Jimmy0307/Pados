'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { spawnSync } = require('node:child_process');
const { resolveHome, ensureLayout, LAYOUT, restrictToOwner, classifyWindowsAcl } = require('../lib/home');
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

const OWNER = 'HOST\\runneradmin';

// The fully qualified current account (DOMAIN\user) from the system whoami, never a PATH lookup.
function windowsAccount() {
  const whoami = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'whoami.exe');
  return spawnSync(whoami, [], { encoding: 'utf8', windowsHide: true }).stdout.trim();
}

// R-6: the whole home is owner-only, including an existing home whose subdirectories predate the ACL.
function aclEntries(target) {
  const out = spawnSync('icacls', [target], { encoding: 'utf8' }).stdout;
  return out.split(/\r?\n/).map((line, index) => (index === 0 ? line.slice(target.length) : line).trim())
    .filter((line) => /^[^:]+:\(/.test(line));
}

test('whole PADOS_HOME satisfies the Windows ACL contract (root, data, logs, state, runtime, cache)', { skip: process.platform !== 'win32' }, () => {
  const home = path.join(tempDir(), 'home');
  fs.mkdirSync(path.join(home, 'data', 'objects'), { recursive: true }); // pre-existing content inherits the old ACL
  fs.writeFileSync(path.join(home, 'data', 'objects', 'x.bin'), 'x');
  ensureLayout(home);
  const me = windowsAccount();
  for (const target of [home, ...['data', 'logs', 'state', 'runtime', 'cache', 'config'].map((n) => path.join(home, n)),
    path.join(home, 'data', 'objects', 'x.bin')]) {
    const entries = aclEntries(target);
    assert.ok(entries.length >= 1, `${target}: no ACL entries parsed`);
    const verdict = classifyWindowsAcl(entries.map((entry) => entry.slice(0, entry.indexOf(':('))), me);
    assert.deepEqual(verdict, { OWNER_PRESENT: 'YES', BROAD_PRINCIPAL_PRESENT: 'NO', ONLY_ALLOWED_TRUSTED_PRINCIPALS: 'YES' },
      `${target}: ACL violates the owner(+OS-trusted) contract: ${entries.join(' | ')}`);
  }
});

// The Windows ACL contract itself, on synthetic icacls principals (runs on every platform).
test('Windows ACL contract: owner, optionally SYSTEM / Administrators, nothing else', () => {
  const ok = { OWNER_PRESENT: 'YES', BROAD_PRINCIPAL_PRESENT: 'NO', ONLY_ALLOWED_TRUSTED_PRINCIPALS: 'YES' };
  assert.deepEqual(classifyWindowsAcl(['HOST\\runneradmin'], OWNER), ok);
  assert.deepEqual(classifyWindowsAcl(['HOST\\runneradmin', 'NT AUTHORITY\\SYSTEM'], OWNER), ok);
  assert.deepEqual(classifyWindowsAcl(['host\\RunnerAdmin', '*S-1-5-18'], OWNER), ok);
  // The exact ACL observed on the elevated GitHub-hosted Windows runner (standard user-profile ACL).
  assert.deepEqual(classifyWindowsAcl(['NT AUTHORITY\\SYSTEM', 'BUILTIN\\Administrators', 'HOST\\runneradmin'], OWNER), ok);
  assert.deepEqual(classifyWindowsAcl(['*S-1-5-18', '*S-1-5-32-544', 'HOST\\runneradmin'], OWNER), ok);
  // OS-trusted principals never stand in for the owner.
  assert.equal(classifyWindowsAcl(['NT AUTHORITY\\SYSTEM', 'BUILTIN\\Administrators'], OWNER).OWNER_PRESENT, 'NO');
  // The owner is the exact account: a same-named account of another domain/host is not the owner.
  assert.deepEqual(classifyWindowsAcl(['EVIL\\runneradmin'], OWNER),
    { OWNER_PRESENT: 'NO', BROAD_PRINCIPAL_PRESENT: 'NO', ONLY_ALLOWED_TRUSTED_PRINCIPALS: 'NO' });
  assert.equal(classifyWindowsAcl(['HOST\\runneradmin', 'EVIL\\runneradmin'], OWNER).ONLY_ALLOWED_TRUSTED_PRINCIPALS, 'NO');
  assert.equal(classifyWindowsAcl(['runneradmin'], 'runneradmin').OWNER_PRESENT, 'NO'); // unqualified owner fails closed
  // SYSTEM alone (owner missing) is not an owner-only home.
  assert.equal(classifyWindowsAcl(['NT AUTHORITY\\SYSTEM'], OWNER).OWNER_PRESENT, 'NO');
  assert.equal(classifyWindowsAcl([], OWNER).ONLY_ALLOWED_TRUSTED_PRINCIPALS, 'NO');
});

for (const broad of ['Everyone', 'BUILTIN\\Users', 'NT AUTHORITY\\Authenticated Users', 'BUILTIN\\Guests',
  'NT AUTHORITY\\ANONYMOUS LOGON', 'NT AUTHORITY\\INTERACTIVE', '*S-1-1-0', '*S-1-5-32-545', '*S-1-5-11']) {
  test(`Windows ACL contract rejects broad principal ${broad}`, () => {
    const verdict = classifyWindowsAcl(['HOST\\runneradmin', 'NT AUTHORITY\\SYSTEM', broad], OWNER);
    assert.equal(verdict.BROAD_PRINCIPAL_PRESENT, 'YES');
    assert.equal(verdict.ONLY_ALLOWED_TRUSTED_PRINCIPALS, 'NO');
  });
}

test('Windows ACL contract catches a broad grant re-added to a real PADOS_HOME', { skip: process.platform !== 'win32' }, () => {
  const home = path.join(tempDir(), 'home');
  ensureLayout(home);
  const data = path.join(home, 'data');
  for (const sid of ['*S-1-5-32-545', '*S-1-1-0', '*S-1-5-11']) {
    assert.equal(spawnSync('icacls', [data, '/grant', `${sid}:R`], { encoding: 'utf8' }).status, 0);
  }
  const principals = aclEntries(data).map((entry) => entry.slice(0, entry.indexOf(':(')));
  const verdict = classifyWindowsAcl(principals, windowsAccount());
  assert.equal(verdict.BROAD_PRINCIPAL_PRESENT, 'YES', principals.join(' | '));
  assert.equal(verdict.ONLY_ALLOWED_TRUSTED_PRINCIPALS, 'NO');
});

test('Windows ACL contract rejects unknown principals (fail closed), incl. unqualified/foreign trusted names', () => {
  for (const other of ['HOST\\someone-else', 'CREATOR OWNER', '*S-1-5-21-1-2-3-1001', 'SYSTEM', 'HOST\\SYSTEM',
    'Administrators', 'HOST\\Administrators', 'BUILTIN\\Power Users', 'BUILTIN\\Remote Desktop Users']) {
    assert.equal(classifyWindowsAcl(['HOST\\runneradmin', other], OWNER).ONLY_ALLOWED_TRUSTED_PRINCIPALS, 'NO', other);
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
