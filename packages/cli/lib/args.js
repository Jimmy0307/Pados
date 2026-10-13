'use strict';
// Minimal argv parser (no dependencies): `--flag`, `--key value`, `--key=value`, positionals.

const { PadosError } = require('./errors');

const GLOBAL_BOOLEANS = ['debug', 'json', 'help', 'non-interactive', 'yes'];

function camel(name) {
  return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

// spec: { booleans: [...], values: [...] } (kebab-case names); returns { command, positionals, flags }.
function parseArgs(argv, spec = { booleans: [], values: [] }) {
  const booleans = new Set([...GLOBAL_BOOLEANS, ...(spec.booleans || [])]);
  const values = new Set(spec.values || []);
  const flags = {};
  const positionals = [];
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith('--')) {
      positionals.push(arg);
      continue;
    }
    const [rawName, inline] = arg.slice(2).split(/=(.*)/s, 2);
    if (booleans.has(rawName)) {
      if (inline !== undefined) throw new PadosError('USAGE', { cause: `--${rawName} does not take a value.`, exitCode: 2 });
      flags[camel(rawName)] = true;
    } else if (values.has(rawName)) {
      const value = inline !== undefined ? inline : argv[index + 1];
      if (value === undefined || (inline === undefined && value.startsWith('--'))) {
        throw new PadosError('USAGE', { cause: `--${rawName} needs a value.`, exitCode: 2 });
      }
      if (inline === undefined) index += 1;
      flags[camel(rawName)] = value;
    } else {
      throw new PadosError('USAGE', { cause: `Unknown option --${rawName}.`, exitCode: 2 });
    }
  }
  return { positionals, flags };
}

module.exports = { parseArgs, GLOBAL_BOOLEANS };
