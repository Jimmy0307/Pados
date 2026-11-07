'use strict';
// RC2 INST-1: re-running `pados init` on an existing installation never activates another runtime or migrates its
// data without consent; it points to `pados update`, which checks compatibility and asks before migrating.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { ensureLayout, layout } = require('../lib/home');
const { runtimePackageFor } = require('../lib/platform');
const { readCurrent, versionDir, writeCurrent } = require('../lib/runtime');
const { tempDir, fakeRuntimePackage, runCli } = require('./helpers');

const CLI_VERSION = require('../package.json').version;
const OLD = '0.0.1-installed';

function existingInstallation() {
  const home = path.join(tempDir(), 'home');
  ensureLayout(home);
  fs.mkdirSync(path.dirname(layout(home).configFile), { recursive: true });
  fs.writeFileSync(layout(home).configFile, 'PADOS_DB_NAME=pados_existing\n');
  writeCurrent(home, { schema: 'pados.cli.runtime-current.v1', version: OLD, previous: null, history: [] });
  const wanted = runtimePackageFor();
  const pkg = fakeRuntimePackage(tempDir(), { name: wanted.name, version: CLI_VERSION, platform: wanted.key });
  return { home, pkg };
}

// R19-5: --adopt-runtime is consent for a *missing* runtime record only; it never activates a different version.
for (const extra of [[], ['--yes'], ['--adopt-runtime'], ['--adopt-runtime', '--yes']]) {
  test(`init ${extra.join(' ') || '(no flags)'} on an existing installation refuses to activate a newer runtime`, () => {
    const { home, pkg } = existingInstallation();
    const result = runCli(['init', '--non-interactive', '--json', ...extra],
      { PADOS_HOME: home, PADOS_RUNTIME_PACKAGE_DIR: pkg.dir });
    assert.equal(readCurrent(home).version, OLD, 'the installed runtime stays active');
    assert.notEqual(result.status, 0);
    assert.match(`${result.stdout}${result.stderr}`, /UPDATE_REQUIRED/);
    assert.equal(fs.existsSync(versionDir(home, CLI_VERSION)), false, 'the new runtime is not even installed');
    assert.equal(fs.readFileSync(layout(home).configFile, 'utf8'), 'PADOS_DB_NAME=pados_existing\n');
  });
}

test('init migrates existing data forward only with the dedicated --migrate consent', () => {
  const { unconsentedMigration } = require('../lib/commands/init');
  const upgrade = { status: 'UPGRADE_AVAILABLE', lanes: [
    { lane: 'operational', database: 'pados', state: 'UPGRADE_AVAILABLE', revisions: ['20261010_0018'],
      target_revision: '20261101_0020' },
    { lane: 'corpus', database: 'pados_corpus', state: 'CURRENT' }] };
  assert.deepEqual(unconsentedMigration(upgrade, {}), ['operational (pados): 20261010_0018 -> 20261101_0020']);
  assert.deepEqual(unconsentedMigration(upgrade, { yes: true }), ['operational (pados): 20261010_0018 -> 20261101_0020'],
    '--yes confirms disk use only and never migrates data');
  assert.equal(unconsentedMigration(upgrade, { migrate: true }), null);
  const fresh = { status: 'UNINITIALIZED', lanes: [{ lane: 'operational', state: 'UNINITIALIZED' },
    { lane: 'corpus', state: 'DATABASE_MISSING' }] };
  assert.equal(unconsentedMigration(fresh, {}), null, 'empty databases hold no data to migrate');
});

for (const record of ['missing', 'corrupt']) {
  test(`init refuses to adopt a runtime when the configuration exists but the runtime record is ${record}`, () => {
    const { home, pkg } = existingInstallation();
    if (record === 'missing') fs.rmSync(layout(home).current);
    else fs.writeFileSync(layout(home).current, '{"version":');
    const result = runCli(['init', '--non-interactive', '--json', '--yes'],
      { PADOS_HOME: home, PADOS_RUNTIME_PACKAGE_DIR: pkg.dir });
    assert.notEqual(result.status, 0);
    assert.match(`${result.stdout}${result.stderr}`, /RUNTIME_RECORD_MISSING/);
    assert.equal(fs.existsSync(versionDir(home, CLI_VERSION)), false, 'nothing is installed or activated');
    assert.equal(fs.readFileSync(layout(home).configFile, 'utf8'), 'PADOS_DB_NAME=pados_existing\n');
  });
}

test('--adopt-runtime is the explicit consent to activate a runtime for an existing configuration', () => {
  const { home, pkg } = existingInstallation();
  fs.rmSync(layout(home).current);
  const result = runCli(['init', '--non-interactive', '--json', '--adopt-runtime'],
    { PADOS_HOME: home, PADOS_RUNTIME_PACKAGE_DIR: pkg.dir });
  assert.doesNotMatch(`${result.stdout}${result.stderr}`, /RUNTIME_RECORD_MISSING|UPDATE_REQUIRED/);
  assert.equal(readCurrent(home).version, CLI_VERSION, 'the consented runtime is recorded as active');
});
