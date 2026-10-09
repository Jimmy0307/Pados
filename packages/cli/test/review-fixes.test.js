'use strict';
// Final-review fixes: R-2 pack-time build-path check, R-3 migration consent, R-8 development override warnings.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { buildPathNeedles, scanForNeedles, assertNoBuildPaths } = require('../../scripts/payload-scan');
const { checkBuildPaths } = require('../../scripts/pack');
const { confirmMigration } = require('../lib/commands/update');
const { devOverrideWarnings } = require('../lib/main');
const { tempDir, runCli } = require('./helpers');

const FAKE_ROOT = 'C:\\Users\\builder\\worktrees\\agent-0123abcd';

test('build-path needles cover the root (both separators), the profile and the worktree name', () => {
  const needles = buildPathNeedles({ root: FAKE_ROOT, home: 'C:\\Users\\builder' });
  assert.ok(needles.includes('c:\\users\\builder\\worktrees\\agent-0123abcd'));
  assert.ok(needles.includes('c:/users/builder/worktrees/agent-0123abcd'));
  assert.ok(needles.includes('c:\\users\\builder'));
  assert.ok(needles.includes('agent-0123abcd'));
  assert.deepEqual(buildPathNeedles({ root: '/tmp/x', home: '/r' }).filter((n) => n.length < 4), []);
  // A generic CI checkout name is not a needle on its own (it is the product name); its absolute path still is.
  assert.deepEqual(buildPathNeedles({ root: '/home/runner/work/pados/pados', home: '/home/runner' }).sort(),
    ['/home/runner', '/home/runner/work/pados/pados', '\\home\\runner', '\\home\\runner\\work\\pados\\pados']);
});

test('payload scan finds build paths in text, in bytecode (UTF-8 and UTF-16LE, any case) and in file names', () => {
  const dir = tempDir();
  fs.mkdirSync(path.join(dir, 'app', '__pycache__'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'clean.txt'), 'nothing to see here: pados-runtime/python/Lib/os.py');
  fs.writeFileSync(path.join(dir, 'app', '__pycache__', 'm.cpython-312.pyc'),
    Buffer.concat([Buffer.from([0xcb, 0x0d, 0x0d, 0x0a, 0, 0]), Buffer.from('c:\\USERS\\builder\\WORKTREES\\agent-0123abcd\\src\\m.py'), Buffer.from([0xe3])]));
  fs.writeFileSync(path.join(dir, 'app', 'native.dll'), Buffer.from('xx C:/Users/builder/tmp yy', 'utf16le'));
  fs.writeFileSync(path.join(dir, 'agent-0123abcd.log'), '');
  const hits = scanForNeedles(dir, buildPathNeedles({ root: FAKE_ROOT, home: 'C:\\Users\\builder' }));
  const files = new Set(hits.map((hit) => hit.file));
  assert.deepEqual([...files].sort(), ['agent-0123abcd.log', 'app/__pycache__/m.cpython-312.pyc', 'app/native.dll']);
  assert.throws(() => assertNoBuildPaths(dir, buildPathNeedles({ root: FAKE_ROOT })), (e) => e.code === 'PAYLOAD_CONTAINS_BUILD_PATH');
});

test('pack refuses a staged payload that embeds the build root and accepts a clean one', () => {
  const root = tempDir('pados-pack-root-');
  const stage = tempDir();
  fs.writeFileSync(path.join(stage, 'ok.txt'), 'neutral');
  assert.equal(checkBuildPaths(stage, 'runtime-test', { root, env: {} }).hits, 0);
  fs.writeFileSync(path.join(stage, 'leak.pyc'), Buffer.from(`\0\0${path.join(root, 'src', 'pados', 'x.py')}\0`));
  assert.throws(() => checkBuildPaths(stage, 'runtime-test', { root, env: {} }), /PAYLOAD_CONTAINS_BUILD_PATH runtime-test/);
  const extra = tempDir();
  fs.writeFileSync(path.join(extra, 'ci.txt'), '/home/runner/work/pados');
  assert.throws(() => checkBuildPaths(extra, 'cli', { root, env: { PADOS_PACK_FORBIDDEN_PATHS: '/home/runner/work' } }), /PAYLOAD_CONTAINS_BUILD_PATH/);
});

test('the user profile is a needle locally; on a hosted CI runner only the build root is', () => {
  const root = tempDir('pados-pack-root-');
  const stage = tempDir();
  fs.writeFileSync(path.join(stage, 'sbom.json'), '{"path": "/home/runner/_work/upstream/wheel"}'); // upstream wheel content
  assert.throws(() => checkBuildPaths(stage, 'runtime', { root, env: {}, home: '/home/runner' }), /PAYLOAD_CONTAINS_BUILD_PATH/);
  assert.equal(checkBuildPaths(stage, 'runtime', { root, env: { GITHUB_ACTIONS: 'true' }, home: '/home/runner' }).hits, 0);
  fs.writeFileSync(path.join(stage, 'leak.pyc'), Buffer.from(root));
  assert.throws(() => checkBuildPaths(stage, 'runtime', { root, env: { GITHUB_ACTIONS: 'true' }, home: '/home/runner' }), /PAYLOAD_CONTAINS_BUILD_PATH/);
});

const PENDING = { status: 'UPGRADE_REQUIRED', lanes: [{ lane: 'operational', database: 'pados_app', state: 'UPGRADE_AVAILABLE', revisions: ['a1'], target_revision: 'b2' }] };

test('update never migrates forward without consent (non-interactive refuses with an explanation)', async () => {
  await assert.rejects(confirmMigration({ flags: {}, interactive: false }, PENDING), (e) => {
    assert.equal(e.code, 'MIGRATION_CONSENT_REQUIRED');
    assert.match(e.message, /Nothing was migrated/);
    assert.match(e.action, /pg_dump/);
    assert.match(e.action, /--migrate/);
    assert.deepEqual(e.details.lanes, ['operational (pados_app): a1 -> b2']);
    return true;
  });
  await confirmMigration({ flags: { migrate: true }, interactive: false }, PENDING);
  await confirmMigration({ flags: { yes: true }, interactive: false }, PENDING);
});

test('interactive update asks, shows the backup advice and defaults to no', async () => {
  let asked = '';
  const answer = (reply) => async (question) => { asked = question; return reply; };
  await confirmMigration({ flags: {}, interactive: true }, PENDING, { prompt: answer('y') });
  assert.match(asked, /pg_dump/);
  assert.match(asked, /\[y\/N\]/);
  await assert.rejects(confirmMigration({ flags: {}, interactive: true }, PENDING, { prompt: answer('n') }), (e) => e.code === 'MIGRATION_CONSENT_REQUIRED');
  await assert.rejects(confirmMigration({ flags: {}, interactive: true }, PENDING, { prompt: answer('') }), (e) => e.code === 'MIGRATION_CONSENT_REQUIRED');
});

test('update accepts --migrate and documents it', () => {
  const result = runCli(['update', '--migrate', '--json'], { PADOS_HOME: path.join(tempDir(), 'home') });
  assert.equal(JSON.parse(result.stdout).code, 'NOT_INITIALIZED'); // parsed (not a USAGE error), then refused safely
  assert.match(runCli(['help']).stdout, /--migrate/);
});

test('development-only overrides print a warning on stderr', () => {
  assert.deepEqual(devOverrideWarnings({}), []);
  const warnings = devOverrideWarnings({ PADOS_CLI_DEV_PYTHON: 'x', PADOS_CLI_DEV_APP: 'y', PADOS_RUNTIME_PACKAGE_DIR: 'z' });
  assert.equal(warnings.length, 3);
  const result = runCli(['version', '--json'], { PADOS_HOME: path.join(tempDir(), 'home'), PADOS_RUNTIME_PACKAGE_DIR: path.join(tempDir(), 'missing') });
  assert.match(result.stderr, /WARNING: PADOS_RUNTIME_PACKAGE_DIR is set \(development-only override/);
  assert.doesNotMatch(result.stdout, /WARNING/);
});
