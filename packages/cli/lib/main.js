'use strict';
// Command dispatch and the failure boundary (no raw stack traces unless --debug / PADOS_DEBUG=1).

const { parseArgs } = require('./args');
const { PadosError, debugEnabled, formatFailure } = require('./errors');
const { resolveHome } = require('./home');
const init = require('./commands/init');
const lifecycle = require('./commands/lifecycle');
const update = require('./commands/update');
const misc = require('./commands/misc');

const HELP = `PADOS command line (npm bootstrap; PADOS Core is the authority)

Usage: pados <command> [options]

  init       install the runtime into PADOS_HOME, write configuration, migrate a reachable database
             --storage LIGHTWEIGHT|STANDARD|FULL_LOCAL  --min-free-bytes N  --non-interactive  --yes
             --db-host H --db-port P --db-user U --db-name N --db-password-ref secretref://env/NAME
             --db-passfile PATH  --create-databases  --skip-database  --api-port P  --reconfigure
  doctor     PASS / WARNING / BLOCKED per area (--json, --no-device)
  up         start PADOS in the background and wait for health (--timeout SECONDS)
  down       stop the PADOS processes this CLI started
  status     show service state and health
  update     activate the runtime shipped with this CLI (--rollback, --no-migrate; a forward database
             migration needs --migrate / --yes or an interactive yes: back up first)
  version    show CLI, runtime and PADOS versions
  uninstall  remove the application only (dry run unless --apply; --all-data prints a plan)
  runtime | model | storage | node | config   reserved (not yet available)

Global options: --json  --debug  --help   Environment: PADOS_HOME, PADOS_DEBUG=1`;

const HEADLINES = {
  up: 'PADOS could not start.', init: 'PADOS could not be initialized.', update: 'PADOS could not be updated.',
  down: 'PADOS could not be stopped.', doctor: 'PADOS doctor could not run.',
};

const COMMANDS = {
  init: { spec: init.SPEC, run: init.run },
  doctor: { spec: { booleans: ['no-device'], values: [] }, run: lifecycle.doctor },
  up: { spec: { booleans: [], values: ['timeout'] }, run: lifecycle.up },
  down: { spec: { booleans: [], values: [] }, run: lifecycle.down },
  status: { spec: { booleans: [], values: [] }, run: lifecycle.status },
  update: { spec: update.SPEC, run: update.run },
  version: { spec: { booleans: [], values: [] }, run: misc.version },
  uninstall: { spec: misc.UNINSTALL_SPEC, run: misc.uninstall },
  ...Object.fromEntries(misc.RESERVED.map((name) => [name, { spec: { booleans: [], values: [] }, run: misc.reserved(name), loose: true }])),
};

// Development-only overrides (review R-8): honoured, but never silently.
const DEV_OVERRIDES = Object.freeze(['PADOS_CLI_DEV_PYTHON', 'PADOS_CLI_DEV_APP', 'PADOS_RUNTIME_PACKAGE_DIR']);

function devOverrideWarnings(env = process.env) {
  return DEV_OVERRIDES.filter((key) => String(env[key] || '').trim())
    .map((key) => `WARNING: ${key} is set (development-only override; see docs/install/TROUBLESHOOTING.md). Unset it for normal use.`);
}

async function main(argv = process.argv.slice(2), env = process.env) {
  for (const warning of devOverrideWarnings(env)) process.stderr.write(`${warning}\n`);
  const [name, ...rest] = argv;
  if (!name || name === 'help' || name === '--help' || name === '-h') {
    process.stdout.write(`${HELP}\n`);
    return 0;
  }
  if (name === '--version' || name === '-v') return main(['version', ...rest], env);
  const command = COMMANDS[name];
  let flags = {};
  try {
    if (!command) throw new PadosError('USAGE', { cause: `Unknown command "${name}".`, exitCode: 2 });
    const parsed = command.loose ? { flags: {}, positionals: rest } : parseArgs(rest, command.spec);
    flags = parsed.flags;
    if (flags.help) {
      process.stdout.write(`${HELP}\n`);
      return 0;
    }
    const ctx = {
      home: resolveHome(process.platform, env), flags, env, json: Boolean(flags.json),
      interactive: !flags.nonInteractive && Boolean(process.stdin.isTTY && process.stdout.isTTY),
    };
    return await command.run(ctx);
  } catch (error) {
    const debug = debugEnabled(flags, env);
    const headline = HEADLINES[name] || 'PADOS could not complete the command.';
    if (flags.json && error instanceof PadosError) {
      process.stdout.write(`${JSON.stringify({ ok: false, code: error.code, cause: error.message, action: error.action, ...(debug ? { details: error.details } : {}) }, null, 2)}\n`);
    } else {
      process.stderr.write(`${formatFailure(error, { headline, debug })}\n`);
    }
    return error instanceof PadosError ? error.exitCode : 1;
  }
}

module.exports = { main, COMMANDS, HELP, DEV_OVERRIDES, devOverrideWarnings };
