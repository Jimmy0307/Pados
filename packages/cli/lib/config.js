'use strict';
// Writes the ONE PADOS configuration file (PADOS_HOME/config/pados.env) in the format Core already reads
// (PADOS_* keys consumed by pados.config.Settings). It is written once at init (or on --reconfigure) and is
// validated by Core afterwards (python -m pados.install validate-config). No credential is ever written:
// the database password is a SecretRef, and secret-shaped values are refused.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { PadosError } = require('./errors');
const { layout, restrictToOwner } = require('./home');

const DATABASE_NAME = /^[a-z_][a-z0-9_]{0,55}$/;
const SECRET_REF = /^secretref:\/\/[a-zA-Z0-9_-]{1,80}\/[a-zA-Z0-9._/-]{1,160}$/;
// Same credential shapes Core refuses (pados.enterprise.secrets._SECRET_LIKE). The key-header alternative is
// assembled at run time so the release secret scan never matches this source file itself.
const KEY_HEADER = ['-----BEGIN [A-Z ]*PRIVATE', 'KEY-----'].join(' ');
const SECRET_LIKE = new RegExp(`(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[abp]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}|${KEY_HEADER}|[Bb][Ee][Aa][Rr][Ee][Rr]\\s+[A-Za-z0-9._-]{12,})`);
const LOOPBACK = new Set(['127.0.0.1', 'localhost']);
const DEFAULTS = Object.freeze({
  apiHost: '127.0.0.1', apiPort: 8088, dbHost: '127.0.0.1', dbPort: 5432, dbUser: 'pados', dbName: 'pados_app',
  dbPasswordRef: 'secretref://env/PADOS_DB_PASSWORD',
});

function integer(value, name, min, max) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new PadosError('CONFIG_INVALID', { cause: `${name} must be an integer between ${min} and ${max}.` });
  }
  return parsed;
}

function plainValue(value, name) {
  const text = String(value);
  if (/[\r\n\0]/.test(text) || text.length > 400) throw new PadosError('CONFIG_INVALID', { cause: `${name} is not a valid value.` });
  if (SECRET_LIKE.test(text)) throw new PadosError('SECRET_REFUSED');
  return text;
}

// Build the ordered key/value entries. `options` comes from CLI flags; unknown flags never reach here.
function buildConfig(home, options = {}) {
  if (options.dbPassword !== undefined) throw new PadosError('SECRET_REFUSED');
  const merged = { ...DEFAULTS, ...Object.fromEntries(Object.entries(options).filter(([, v]) => v !== undefined)) };
  if (!LOOPBACK.has(merged.apiHost)) {
    throw new PadosError('CONFIG_INVALID', { cause: 'The API binds to loopback only (127.0.0.1 or localhost).' });
  }
  if (!DATABASE_NAME.test(merged.dbName)) {
    throw new PadosError('CONFIG_INVALID', { cause: 'Database names use lowercase letters, digits and underscores.' });
  }
  if (!SECRET_REF.test(merged.dbPasswordRef) || merged.dbPasswordRef.split('/').slice(3).some((p) => p === '..' || p === '.')) {
    throw new PadosError('CONFIG_INVALID', { cause: '--db-password-ref must be a SecretRef such as secretref://env/PADOS_DB_PASSWORD.' });
  }
  const paths = layout(home);
  const entries = [
    ['PADOS_ENV', 'local'],
    ['PADOS_RUNTIME_MODE', 'native'],
    ['PADOS_LOG_LEVEL', 'INFO'],
    ['PADOS_API_HOST', merged.apiHost],
    ['PADOS_API_PORT', String(integer(merged.apiPort, '--api-port', 1024, 65535))],
    ['PADOS_DB_HOST', plainValue(merged.dbHost, '--db-host')],
    ['PADOS_DB_PORT', String(integer(merged.dbPort, '--db-port', 1, 65535))],
    ['PADOS_DB_USER', plainValue(merged.dbUser, '--db-user')],
    ['PADOS_DB_PASSWORD', ''],
    ['PADOS_DB_PASSWORD_REF', merged.dbPasswordRef],
    ['PADOS_DB_NAME', merged.dbName],
    ['PADOS_OPERATIONAL_DATABASE_NAME', merged.dbName],
    ['PADOS_CORPUS_DATABASE_NAME', `${merged.dbName}_corpus`],
    ['PADOS_PRODUCT_DATABASE_NAME', ''],
    ['PADOS_PRODUCT_WORKSPACE_ROOT', path.join(paths.data, 'workspace')],
    ['PADOS_PRODUCT_OBJECT_ROOT', path.join(paths.data, 'objects')],
    ['PADOS_PRODUCT_LOOP_PROVIDER', 'AUTO'],
  ];
  if (merged.dbPassfile) entries.push(['PADOS_DB_PASSFILE', plainValue(path.resolve(merged.dbPassfile), '--db-passfile')]);
  return entries;
}

function renderConfig(entries, { controlTokenHash } = {}) {
  const lines = [
    '# PADOS configuration, written by `pados init` (npm bootstrap). Core reads these PADOS_* keys.',
    '# The environment overrides any value here. Credentials are never stored in this file:',
    '# PADOS_DB_PASSWORD stays empty and PADOS_DB_PASSWORD_REF names where the password comes from',
    '# (secretref://env/<NAME> or secretref://libpq/passfile).',
  ];
  for (const [key, value] of entries) lines.push(`${key}=${value}`);
  if (controlTokenHash) {
    lines.push('# SHA-256 of the local control token (the token itself is in control.token, owner-only).');
    lines.push(`PADOS_CONTROL_API_TOKEN_HASH=${controlTokenHash}`);
  }
  return `${lines.join('\n')}\n`;
}

function assertNoSecrets(text) {
  for (const line of text.split(/\r?\n/)) {
    if (/^PADOS_DB_PASSWORD=.+/.test(line) || (!line.startsWith('#') && SECRET_LIKE.test(line))) {
      throw new PadosError('SECRET_REFUSED');
    }
  }
}

function writeOwnerOnly(file, text) {
  const temporary = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, text, { mode: 0o600 });
  restrictToOwner(temporary);
  fs.renameSync(temporary, file);
  restrictToOwner(file);
}

// Generates the local control token (Core's existing `pados init` semantics): the token goes to an owner-only
// file, only its hash goes into the configuration. An existing token is kept unless `rotate` is set.
function ensureControlToken(home, { rotate = false } = {}) {
  const file = layout(home).controlToken;
  if (!rotate && fs.existsSync(file)) {
    const existing = fs.readFileSync(file, 'utf8').trim();
    if (existing) return crypto.createHash('sha256').update(existing).digest('hex').toUpperCase();
  }
  const generated = crypto.randomBytes(32).toString('base64url');
  writeOwnerOnly(file, `${generated}\n`);
  return crypto.createHash('sha256').update(generated).digest('hex').toUpperCase();
}

function writeConfig(home, entries, { force = false, controlTokenHash } = {}) {
  const file = layout(home).configFile;
  if (fs.existsSync(file) && !force) throw new PadosError('CONFIG_EXISTS');
  const text = renderConfig(entries, { controlTokenHash });
  assertNoSecrets(text);
  writeOwnerOnly(file, text);
  return file;
}

// Read-only parse for the launcher (host/port); Core remains the validator and the only consumer of meaning.
function readConfigValues(home) {
  const values = {};
  const text = fs.readFileSync(layout(home).configFile, 'utf8');
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const index = line.indexOf('=');
    values[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return values;
}

function effectiveEndpoint(home, env = process.env) {
  const values = readConfigValues(home);
  const host = env.PADOS_API_HOST || values.PADOS_API_HOST || DEFAULTS.apiHost;
  const port = Number(env.PADOS_API_PORT || values.PADOS_API_PORT || DEFAULTS.apiPort);
  return { host, port, base: `http://${host === 'localhost' ? '127.0.0.1' : host}:${port}` };
}

module.exports = {
  DEFAULTS, SECRET_LIKE, buildConfig, renderConfig, assertNoSecrets, writeConfig, ensureControlToken,
  readConfigValues, effectiveEndpoint, writeOwnerOnly,
};
