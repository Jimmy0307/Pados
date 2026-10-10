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

// Windows ACL contract for PADOS_HOME (review R-6). PADOS grants only the current account; the effective ACL
// may additionally hold the machine's OS-trusted principals NT AUTHORITY\SYSTEM (S-1-5-18) and
// BUILTIN\Administrators (S-1-5-32-544). Both are on every Windows user profile (%LOCALAPPDATA%, the default
// PADOS_HOME parent), an elevated (UAC-off) administrator such as a hosted runner keeps them explicitly, and
// both already control the machine (local administrators hold take-ownership/backup privileges), so their
// ACEs grant nothing new; removing them would only break OS backup/AV tooling. Nothing else is acceptable:
// broad identities are named explicitly and any other or unrecognised principal fails closed too.
// Only qualified names or well-known SIDs: a bare "SYSTEM" could be an ordinary account of that name.
const WINDOWS_TRUSTED_SYSTEM = Object.freeze([
  'nt authority\\system', '*s-1-5-18',
  'builtin\\administrators', '*s-1-5-32-544',
]);
const WINDOWS_BROAD_PRINCIPALS = Object.freeze([
  'everyone', '*s-1-1-0',
  'builtin\\users', 'users', '*s-1-5-32-545',
  'nt authority\\authenticated users', 'authenticated users', '*s-1-5-11',
  'builtin\\guests', 'guests', '*s-1-5-32-546', 'guest',
  'nt authority\\anonymous logon', 'anonymous logon', '*s-1-5-7',
  'nt authority\\interactive', 'interactive', '*s-1-5-4',
  'nt authority\\network', 'network', '*s-1-5-2',
]);

// Classifies icacls principals (e.g. "NT AUTHORITY\SYSTEM", "HOST\user", "*S-1-5-18") against the contract.
// `owner` is the fully qualified account PADOS grants (windowsPrincipal(), i.e. `whoami`: "DOMAIN\user"); it is
// matched exactly, so a same-named account of another domain or host ("OTHER\user") is not the owner.
function classifyWindowsAcl(principals, owner) {
  const me = String(owner).trim().toLowerCase();
  const names = principals.map((p) => String(p).trim().toLowerCase());
  const isOwner = (n) => me.includes('\\') && n === me;
  const isBroad = (n) => WINDOWS_BROAD_PRINCIPALS.includes(n);
  const isTrusted = (n) => isOwner(n) || WINDOWS_TRUSTED_SYSTEM.includes(n);
  return {
    OWNER_PRESENT: names.some(isOwner) ? 'YES' : 'NO',
    BROAD_PRINCIPAL_PRESENT: names.some(isBroad) ? 'YES' : 'NO',
    ONLY_ALLOWED_TRUSTED_PRINCIPALS: names.length > 0 && names.every(isTrusted) ? 'YES' : 'NO',
  };
}

// Owner-only: POSIX modes (0700 directories, 0600 files); on Windows an explicit ACL granting the current
// user (inheritance removed; see the Windows ACL contract above). Returns a description of what was applied.
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
// wherever PADOS_HOME is; config/ additionally gets its own explicit ACL. "Owner-only" on Windows means the
// contract enforced by classifyWindowsAcl: the owner, optionally SYSTEM, and no other principal.
function ensureLayout(home, { platform = process.platform } = {}) {
  fs.mkdirSync(home, { recursive: true });
  restrictToOwner(home, { directory: true, platform });
  for (const name of LAYOUT) fs.mkdirSync(path.join(home, name), { recursive: true });
  restrictToOwner(path.join(home, 'config'), { directory: true, platform });
}

module.exports = { LAYOUT, resolveHome, layout, ensureLayout, restrictToOwner, classifyWindowsAcl };
