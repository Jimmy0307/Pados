'use strict';
// Installed runtimes live in PADOS_HOME/runtime/<version>/ (copied and verified from the platform package so
// that npm upgrades never remove the version in use and the previous version stays available for rollback).
// runtime/current.json names the active version. The CLI only launches the PADOS Python runtime; it never
// interprets PADOS state itself.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { PadosError } = require('./errors');
const { layout } = require('./home');

const MANIFEST_RELATIVE = 'app/runtime-manifest.json';

function listFiles(root) {
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile()) files.push(path.relative(root, full).split(path.sep).join('/'));
      else throw new PadosError('RUNTIME_PACKAGE_INVALID', { cause: `Unexpected non-regular file: ${entry.name}` });
    }
  };
  walk(root);
  return files.filter((name) => name !== MANIFEST_RELATIVE && name !== '.installed').sort();
}

// Digest over (path, size, sha256) of every runtime file; the build records it in runtime-manifest.json.
function treeDigest(root) {
  const index = crypto.createHash('sha256');
  let bytes = 0;
  const files = listFiles(root);
  for (const name of files) {
    const data = fs.readFileSync(path.join(root, ...name.split('/')));
    bytes += data.length;
    index.update(`${name}\0${data.length}\0${crypto.createHash('sha256').update(data).digest('hex')}\n`);
  }
  return { count: files.length, bytes, digest: index.digest('hex') };
}

function readCurrent(home) {
  try {
    return JSON.parse(fs.readFileSync(layout(home).current, 'utf8'));
  } catch {
    return null;
  }
}

function writeCurrent(home, record) {
  const target = layout(home).current;
  const temporary = `${target}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(record, null, 2)}\n`);
  fs.renameSync(temporary, target);
  return record;
}

function versionDir(home, version) {
  if (!/^[0-9A-Za-z.+-]{1,64}$/.test(version)) throw new PadosError('RUNTIME_PACKAGE_INVALID');
  return path.join(layout(home).runtime, version);
}

function readManifest(runtimeDir) {
  return JSON.parse(fs.readFileSync(path.join(runtimeDir, ...MANIFEST_RELATIVE.split('/')), 'utf8'));
}

function isInstalled(home, version, expectedDigest) {
  const dir = versionDir(home, version);
  try {
    const manifest = readManifest(dir);
    return fs.existsSync(path.join(dir, '.installed')) && manifest.files.digest === expectedDigest;
  } catch {
    return false;
  }
}

// Copy runtimePackage.runtimeDir into PADOS_HOME/runtime/<version>, verifying the manifest digest of the copy.
function installRuntime(home, runtimePackage) {
  const { version, manifest } = runtimePackage;
  const finalDir = versionDir(home, version);
  if (isInstalled(home, version, manifest.files.digest)) return { dir: finalDir, installed: false };
  const staging = `${finalDir}.partial-${process.pid}`;
  fs.rmSync(staging, { recursive: true, force: true });
  fs.cpSync(runtimePackage.runtimeDir, staging, { recursive: true, dereference: false, errorOnExist: true });
  const actual = treeDigest(staging);
  if (actual.digest !== manifest.files.digest || actual.count !== manifest.files.count) {
    fs.rmSync(staging, { recursive: true, force: true });
    throw new PadosError('RUNTIME_PACKAGE_INVALID', {
      cause: 'The runtime package contents do not match its manifest.',
      details: { expected: manifest.files, actual },
    });
  }
  if (process.platform !== 'win32') fs.chmodSync(path.join(staging, ...manifest.python_executable.split('/')), 0o755);
  fs.writeFileSync(path.join(staging, '.installed'), `${new Date().toISOString()}\n`);
  if (fs.existsSync(finalDir)) {
    // An incomplete or different copy of the same version: never delete it; move it aside for inspection.
    fs.renameSync(finalDir, `${finalDir}.replaced-${Date.now()}`);
  }
  fs.renameSync(staging, finalDir);
  return { dir: finalDir, installed: true };
}

function verifyInstalled(runtimeDir) {
  const manifest = readManifest(runtimeDir);
  const actual = treeDigest(runtimeDir);
  if (actual.digest !== manifest.files.digest) {
    throw new PadosError('RUNTIME_VERIFY_FAILED', { details: { expected: manifest.files.digest, actual: actual.digest } });
  }
  return manifest;
}

// The active runtime: developer override (PADOS_CLI_DEV_PYTHON + PADOS_CLI_DEV_APP) or runtime/current.json.
function activeRuntime(home, env = process.env) {
  if (env.PADOS_CLI_DEV_PYTHON && env.PADOS_CLI_DEV_APP) {
    return { version: 'dev', python: env.PADOS_CLI_DEV_PYTHON, appDir: env.PADOS_CLI_DEV_APP, dev: true };
  }
  const current = readCurrent(home);
  if (!current || !current.version) throw new PadosError('RUNTIME_NOT_INSTALLED');
  return runtimeAt(versionDir(home, current.version), current.version);
}

function runtimeAt(dir, version) {
  let manifest;
  try {
    manifest = readManifest(dir);
  } catch {
    throw new PadosError('RUNTIME_NOT_INSTALLED', { details: { dir } });
  }
  return {
    version, dir, manifest, dev: false,
    python: path.join(dir, ...manifest.python_executable.split('/')),
    appDir: path.join(dir, 'app'),
  };
}

function childEnv(home, env = process.env) {
  const result = { ...env, PADOS_HOME: home, PYTHONNOUSERSITE: '1', PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' };
  delete result.PYTHONPATH;
  delete result.PYTHONHOME;
  delete result.PYTHONSTARTUP;
  return result;
}

function pythonArgs(runtime, args) {
  // -I (isolated) and -B (never write bytecode: the installed runtime stays byte-identical to its manifest).
  return runtime.dev ? ['-X', 'utf8', '-m', 'pados.install', ...args] : ['-I', '-B', '-X', 'utf8', '-m', 'pados.install', ...args];
}

// Run `python -m pados.install <args> --home <home>` and parse its JSON output.
function runPython(runtime, home, args, { timeoutMs = 300000, env = process.env } = {}) {
  const runEnv = childEnv(home, env);
  if (runtime.dev && env.PADOS_CLI_DEV_PYTHONPATH) runEnv.PYTHONPATH = env.PADOS_CLI_DEV_PYTHONPATH;
  const result = spawnSync(runtime.python, pythonArgs(runtime, [...args, '--home', home]), {
    cwd: runtime.appDir, env: runEnv, encoding: 'utf8', timeout: timeoutMs, windowsHide: true, maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) {
    throw new PadosError('RUNTIME_COMMAND_FAILED', { cause: 'The PADOS runtime could not be started.', details: { error: String(result.error.message) } });
  }
  let json = null;
  try {
    json = JSON.parse(result.stdout);
  } catch {
    throw new PadosError('RUNTIME_COMMAND_FAILED', { details: { exit: result.status, stderr: tail(result.stderr) } });
  }
  return { status: result.status, json, stderr: result.stderr };
}

function tail(text, lines = 20) {
  return String(text || '').split(/\r?\n/).slice(-lines).join('\n');
}

module.exports = {
  MANIFEST_RELATIVE, listFiles, treeDigest, readCurrent, writeCurrent, versionDir, readManifest, installRuntime,
  verifyInstalled, activeRuntime, runtimeAt, childEnv, pythonArgs, runPython, tail,
};
