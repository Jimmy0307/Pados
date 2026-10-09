'use strict';
// PADOS_HOME resolution (mirrors src/pados/install/home.py; test/fixtures/home-vectors.json keeps them equal)
// and owner-only permissions for the directories and files the CLI creates.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { PadosError } = require('./errors');

const LAYOUT = Object.freeze(['config', 'runtime', 'data', 'logs', 'cache', 'state']);

function resolveHome(platform = process.platform, env = process.env, userHome = os.homedir()) {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const override = String(env.PADOS_HOME || '').trim();
  let candidate;
  if (override) {
    if (!paths.isAbsolute(override)) throw new PadosError('PADOS_HOME_NOT_ABSOLUTE');
    candidate = paths.normalize(override);
    if (candidate.length > 1 && (candidate.endsWith('/') || (platform === 'win32' && candidate.endsWith('\\')))) {
      if (!/^[A-Za-z]:\\$/.test(candidate)) candidate = candidate.slice(0, -1);
    }
  } else if (platform === 'win32') {
    const local = String(env.LOCALAPPDATA || '').trim();
    const base = local && path.win32.isAbsolute(local) ? local : path.win32.join(userHome, 'AppData', 'Local');
    candidate = path.win32.normalize(path.win32.join(base, 'PADOS'));
  } else if (platform === 'darwin') {
    candidate = path.posix.join(userHome, 'Library', 'Application Support', 'PADOS');
  } else {
    const xdg = String(env.XDG_DATA_HOME || '').trim();
    const base = xdg && path.posix.isAbsolute(xdg) ? xdg : path.posix.join(userHome, '.local', 'share');
    candidate = path.posix.normalize(path.posix.join(base, 'pados'));
  }
  if (platform === 'win32' && candidate.replace(/\\+$/, '').toLowerCase() === 'c:\\pados') {
    throw new PadosError('PADOS_HOME_RESERVED_PATH');
  }
  return candidate;
}

function layout(home) {
  return {
    home,
    config: path.join(home, 'config'),
    configFile: path.join(home, 'config', 'pados.env'),
    controlToken: path.join(home, 'config', 'control.token'),
    storageProfile: path.join(home, 'config', 'storage-profile.json'),
    runtime: path.join(home, 'runtime'),
    current: path.join(home, 'runtime', 'current.json'),
    data: path.join(home, 'data'),
    logs: path.join(home, 'logs'),
    cache: path.join(home, 'cache'),
    state: path.join(home, 'state'),
    services: path.join(home, 'state', 'services.json'),
  };
}

let cachedWindowsPrincipal;
function windowsPrincipal() {
  if (cachedWindowsPrincipal !== undefined) return cachedWindowsPrincipal;
  const result = spawnSync('whoami', [], { encoding: 'utf8', windowsHide: true });
  const name = result.status === 0 ? result.stdout.trim() : '';
  cachedWindowsPrincipal = name || os.userInfo().username;
  return cachedWindowsPrincipal;
}

// Owner-only: POSIX modes (0700 directories, 0600 files); on Windows an explicit ACL granting only the
// current user (inheritance removed). Returns a description of what was applied.
function restrictToOwner(target, { directory = false, platform = process.platform } = {}) {
  if (platform !== 'win32') {
    fs.chmodSync(target, directory ? 0o700 : 0o600);
    return { method: 'chmod', mode: directory ? '0700' : '0600' };
  }
  const grant = directory ? `${windowsPrincipal()}:(OI)(CI)F` : `${windowsPrincipal()}:F`;
  const result = spawnSync('icacls', [target, '/inheritance:r', '/grant:r', grant], { encoding: 'utf8', windowsHide: true });
  if (result.status !== 0) throw new PadosError('INTERNAL_ERROR', { cause: 'Could not restrict file permissions.', details: result.stderr });
  return { method: 'icacls', grant };
}

// The whole PADOS_HOME is owner-only on every platform (review R-6). On Windows the root gets an explicit,
// non-inherited ACL for the current user that its subdirectories (data, logs, state, runtime, cache) inherit,
// wherever PADOS_HOME is; config/ additionally gets its own explicit ACL.
function ensureLayout(home, { platform = process.platform } = {}) {
  fs.mkdirSync(home, { recursive: true });
  restrictToOwner(home, { directory: true, platform });
  for (const name of LAYOUT) fs.mkdirSync(path.join(home, name), { recursive: true });
  restrictToOwner(path.join(home, 'config'), { directory: true, platform });
}

module.exports = { LAYOUT, resolveHome, layout, ensureLayout, restrictToOwner };
