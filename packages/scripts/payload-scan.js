'use strict';
// Pack-time leak check (review R-2): a staged package must not contain the build machine's paths. Every file is
// scanned as bytes (text, .pyc, binaries), case-insensitively, for UTF-8 and UTF-16LE encodings of each needle.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const MIN_NEEDLE_LENGTH = 4;
const GENERIC_ROOT_NAME = /^(pados|pados-ai|pados-core|repo|repository|source|src|work|workspace|build|checkout)$/i;

// The absolute build root (both separator styles), the user profile directory and the build root's directory name
// (for a worktree, its private directory name).
function buildPathNeedles({ root, home = os.homedir(), extra = [] } = {}) {
  const needles = new Set();
  const add = (value) => {
    const text = String(value || '').replace(/[\\/]+$/, '');
    if (text.length >= MIN_NEEDLE_LENGTH) needles.add(text.toLowerCase());
  };
  for (const absolute of [root, home]) {
    if (!absolute) continue;
    add(absolute);
    add(absolute.replace(/\\/g, '/'));
    add(absolute.replace(/\//g, '\\'));
  }
  // The directory name identifies a worktree or private checkout; a generic checkout name (CI clones the
  // repository as "pados") would match the product's own name, so it is covered by the absolute path only.
  const base = root ? String(root).replace(/[\\/]+$/, '').split(/[\\/]/).pop() : '';
  if (base.length >= 6 && !GENERIC_ROOT_NAME.test(base)) add(base);
  for (const value of extra) add(value);
  return [...needles];
}

function encodings(needle) {
  return [Buffer.from(needle, 'utf8').toString('latin1'), Buffer.from(needle, 'utf16le').toString('latin1')];
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.isFile()) yield full;
  }
}

// Returns [{ file (relative, posix), needle }]; file NAMES are checked as well as contents.
function scanForNeedles(dir, needles) {
  const patterns = needles.map((needle) => ({ needle, forms: encodings(needle) }));
  const hits = [];
  for (const file of walk(dir)) {
    const relative = path.relative(dir, file).split(path.sep).join('/');
    const haystack = fs.readFileSync(file).toString('latin1').toLowerCase();
    const name = relative.toLowerCase();
    for (const { needle, forms } of patterns) {
      if (name.includes(needle) || forms.some((form) => haystack.includes(form))) hits.push({ file: relative, needle });
    }
  }
  return hits;
}

function assertNoBuildPaths(dir, needles, label = dir) {
  const hits = scanForNeedles(dir, needles);
  if (hits.length) {
    const files = [...new Set(hits.map((hit) => hit.file))];
    const error = new Error(`PAYLOAD_CONTAINS_BUILD_PATH ${label}: ${files.length} file(s) embed a build-machine path `
      + `(first: ${files.slice(0, 5).join(', ')}); refusing to pack`);
    error.code = 'PAYLOAD_CONTAINS_BUILD_PATH';
    error.hits = hits;
    throw error;
  }
  return { scanned_needles: needles.length, hits: 0 };
}

module.exports = { buildPathNeedles, scanForNeedles, assertNoBuildPaths };
