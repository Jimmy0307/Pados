#!/usr/bin/env node
'use strict';
// Stage and `npm pack` PADOS npm packages into --out, with .sha256 sidecars and pack-report.json.
// Local only: never runs `npm publish`. Staging happens outside the repository so repository ignore files
// cannot silently drop runtime files; the runtime digest is re-verified on the staged copy.
//
// Usage: node packages/scripts/pack.js --out DIR [--packages cli,runtime-linux-x64] [--set-version 0.1.0-alpha.2]

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { treeDigest } = require('../cli/lib/runtime');
const { assertNoBuildPaths, buildPathNeedles } = require('./payload-scan');

const PACKAGES_DIR = path.resolve(__dirname, '..');
const ROOT = path.resolve(PACKAGES_DIR, '..');

function option(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function rootLicense() {
  const text = fs.readFileSync(path.join(ROOT, 'LICENSE'), 'utf8');
  return { matchesAgpl: /GNU AFFERO GENERAL PUBLIC LICENSE/.test(text.slice(0, 2000)), firstLine: text.split(/\r?\n/).find((l) => l.trim()) };
}

function copyCommon(stage) {
  for (const name of ['LICENSE', 'NOTICE']) {
    fs.writeFileSync(path.join(stage, name), fs.readFileSync(path.join(ROOT, name), 'utf8').replace(/\r\n/g, '\n'));
  }
}

function normalizeLineEndings(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) normalizeLineEndings(full);
    else if (/\.(js|json|md)$/.test(entry.name)) fs.writeFileSync(full, fs.readFileSync(full, 'utf8').replace(/\r\n/g, '\n'));
  }
}

function stageCli(stage, version) {
  const source = path.join(PACKAGES_DIR, 'cli');
  const pkg = JSON.parse(fs.readFileSync(path.join(source, 'package.json'), 'utf8'));
  if (version) {
    pkg.version = version;
    for (const dep of Object.keys(pkg.optionalDependencies)) pkg.optionalDependencies[dep] = version;
  }
  for (const dir of ['bin', 'lib']) fs.cpSync(path.join(source, dir), path.join(stage, dir), { recursive: true });
  fs.copyFileSync(path.join(source, 'README.md'), path.join(stage, 'README.md'));
  normalizeLineEndings(stage); // a Windows checkout may have CRLF; the shebang and tarball must be identical everywhere
  fs.writeFileSync(path.join(stage, 'package.json'), `${JSON.stringify(pkg, null, 2)}\n`);
  copyCommon(stage);
  return pkg;
}

function stageRuntime(name, stage, version) {
  const source = path.join(PACKAGES_DIR, name);
  const runtime = path.join(source, 'runtime');
  if (!fs.existsSync(path.join(runtime, 'app', 'runtime-manifest.json'))) {
    throw new Error(`${name}: runtime payload missing; run packages/scripts/build_runtime.py on that platform first`);
  }
  const pkg = JSON.parse(fs.readFileSync(path.join(source, 'package.json'), 'utf8'));
  if (version) pkg.version = version;
  fs.cpSync(runtime, path.join(stage, 'runtime'), { recursive: true });
  const manifestFile = path.join(stage, 'runtime', 'app', 'runtime-manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
  manifest.package_version = pkg.version; // the manifest is outside the file digest
  fs.writeFileSync(manifestFile, `${JSON.stringify(manifest, null, 2)}\n`);
  const digest = treeDigest(path.join(stage, 'runtime'));
  if (digest.digest !== manifest.files.digest) throw new Error(`${name}: staged runtime does not match its manifest digest`);
  fs.copyFileSync(path.join(runtime, 'licenses', 'THIRD_PARTY_NOTICES.md'), path.join(stage, 'THIRD_PARTY_NOTICES.md'));
  fs.writeFileSync(path.join(stage, 'README.md'), [
    `# ${pkg.name}`, '',
    'Platform runtime for PADOS, installed automatically by `@pados-ai/cli` (do not depend on it directly).',
    `It carries CPython ${manifest.python.version}, the locked Python dependencies and PADOS Core; \`pados init\``,
    'copies it into PADOS_HOME/runtime/<version>/ after verifying its manifest digest.', '',
    'License: AGPL-3.0-only for PADOS (LICENSE, NOTICE); bundled components: THIRD_PARTY_NOTICES.md and runtime/licenses/.', '',
  ].join('\n'));
  fs.writeFileSync(path.join(stage, 'package.json'), `${JSON.stringify(pkg, null, 2)}\n`);
  copyCommon(stage);
  return { pkg, files: digest };
}

// Refuses to pack a payload that embeds the build root, the user profile or the worktree directory name (R-2).
// PADOS_PACK_FORBIDDEN_PATHS (path-delimiter separated) adds further strings. On a hosted CI runner the profile
// directory is a public, shared account path that upstream wheels and CPython binaries legitimately embed (they
// are built on the same runner images, e.g. /home/runner in pydantic-core's SBOM), so only the build root is
// checked there; the runner's workspace path is part of that root.
function checkBuildPaths(stage, name, { root = ROOT, env = process.env, home = os.homedir() } = {}) {
  const extra = String(env.PADOS_PACK_FORBIDDEN_PATHS || '').split(path.delimiter).filter(Boolean);
  const hostedCi = env.GITHUB_ACTIONS === 'true';
  return assertNoBuildPaths(stage, buildPathNeedles({ root, home: hostedCi ? null : home, extra }), name);
}

function npmPack(stage, out) {
  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const result = spawnSync(npm, ['pack', '--json', '--pack-destination', out], {
    cwd: stage, encoding: 'utf8', shell: process.platform === 'win32', maxBuffer: 256 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(`npm pack failed in ${stage}: ${result.stderr}`);
  const [info] = JSON.parse(result.stdout);
  const tarball = path.join(out, info.filename);
  const sha256 = crypto.createHash('sha256').update(fs.readFileSync(tarball)).digest('hex');
  fs.writeFileSync(`${tarball}.sha256`, `${sha256}  ${info.filename}\n`);
  return { filename: info.filename, size: info.size, unpackedSize: info.unpackedSize, entryCount: info.entryCount, integrity: info.integrity, sha256 };
}

function main() {
  const out = path.resolve(option('out', path.join(ROOT, 'dist-npm')));
  const version = option('set-version', undefined);
  const names = option('packages', 'cli').split(',').map((s) => s.trim()).filter(Boolean);
  fs.mkdirSync(out, { recursive: true });
  const stageRoot = fs.mkdtempSync(path.join(out, '.stage-'));
  const license = rootLicense();
  const report = { created_at: new Date().toISOString(), version_override: version || null, root_license: license, packages: [] };
  try {
    for (const name of names) {
      const stage = path.join(stageRoot, name);
      fs.mkdirSync(stage, { recursive: true });
      const staged = name === 'cli' ? { pkg: stageCli(stage, version) } : stageRuntime(name, stage, version);
      const buildPathScan = checkBuildPaths(stage, name);
      report.packages.push({ name: staged.pkg.name, version: staged.pkg.version, license: staged.pkg.license, runtime_files: staged.files || null, build_path_scan: buildPathScan, tarball: npmPack(stage, out) });
    }
  } finally {
    fs.rmSync(stageRoot, { recursive: true, force: true });
  }
  const reportFile = path.join(out, `pack-report-${names.join('+')}${version ? `-${version}` : ''}.json`);
  fs.writeFileSync(reportFile, `${JSON.stringify(report, null, 2)}\n`);
  if (!license.matchesAgpl) {
    process.stderr.write(`WARNING: root LICENSE is not the AGPL-3.0 text yet (first line: ${license.firstLine}); package metadata says AGPL-3.0-only. Integrate agent A's relicensing before any distribution.\n`);
  }
  process.stdout.write(`${JSON.stringify(report.packages.map((p) => ({ name: p.name, version: p.version, ...p.tarball })), null, 2)}\n`);
}

if (require.main === module) main();

module.exports = { checkBuildPaths };
