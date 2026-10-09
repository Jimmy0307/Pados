'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { treeDigest } = require('../lib/runtime');

const BIN = path.resolve(__dirname, '..', 'bin', 'pados.js');

function tempDir(prefix = 'pados-cli-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// A minimal fake runtime package: package.json + runtime/{python stub, app/runtime-manifest.json}.
function fakeRuntimePackage(root, { name = '@pados-ai/runtime-linux-x64', version = '0.1.0-alpha.1', platform = 'linux-x64' } = {}) {
  const dir = path.join(root, 'node_modules', ...name.split('/'));
  const runtimeDir = path.join(dir, 'runtime');
  fs.mkdirSync(path.join(runtimeDir, 'python', 'bin'), { recursive: true });
  fs.mkdirSync(path.join(runtimeDir, 'app', 'src'), { recursive: true });
  fs.writeFileSync(path.join(runtimeDir, 'python', 'bin', 'python3.12'), '#!/bin/sh\n');
  fs.writeFileSync(path.join(runtimeDir, 'app', 'src', 'marker.txt'), crypto.randomBytes(16).toString('hex'));
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name, version }));
  const manifest = {
    schema: 'pados.runtime-manifest.v1', platform, package_version: version, python_executable: 'python/bin/python3.12',
    files: treeDigest(runtimeDir),
  };
  fs.writeFileSync(path.join(runtimeDir, 'app', 'runtime-manifest.json'), JSON.stringify(manifest));
  return { dir, runtimeDir, manifest, name, version };
}

function runCli(args, env = {}) {
  const base = { ...process.env };
  for (const key of Object.keys(base)) if (key.startsWith('PADOS_')) delete base[key];
  return spawnSync(process.execPath, [BIN, ...args], { encoding: 'utf8', env: { ...base, ...env }, timeout: 120000 });
}

module.exports = { BIN, tempDir, fakeRuntimePackage, runCli };
