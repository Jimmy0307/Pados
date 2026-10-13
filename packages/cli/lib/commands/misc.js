'use strict';
// pados version, pados uninstall (application-only, dry-run by default) and reserved stub commands.

const fs = require('node:fs');
const path = require('node:path');
const { PadosError } = require('../errors');
const { layout } = require('../home');
const { cliPackage, platformKey, resolveRuntimePackage } = require('../platform');
const { activeRuntime, readCurrent, runPython } = require('../runtime');
const services = require('../services');
const { write } = require('../output');

const RESERVED = ['runtime', 'model', 'storage', 'node', 'config'];

async function version(ctx) {
  const result = { cli: cliPackage().version, platform: platformKey(), node: process.version };
  try {
    const pkg = resolveRuntimePackage({ env: ctx.env });
    result.runtime_package = { name: pkg.name, version: pkg.version };
  } catch (error) {
    result.runtime_package = { error: error.code || 'UNKNOWN' };
  }
  const current = readCurrent(ctx.home);
  result.installed_runtime = current ? { active: current.version, previous: current.previous } : null;
  try {
    const runtime = activeRuntime(ctx.home, ctx.env);
    const report = runPython(runtime, ctx.home, ['version'], { env: ctx.env, timeoutMs: 60000 }).json;
    result.pados = report.versions;
  } catch {
    result.pados = null;
  }
  if (ctx.json) write(JSON.stringify(result, null, 2));
  else {
    write(`@pados-ai/cli ${result.cli} (${result.platform}, node ${result.node})`);
    write(`runtime package: ${result.runtime_package.version || result.runtime_package.error}`);
    write(`active runtime:  ${current ? current.version : 'not installed (run pados init)'}`);
    if (result.pados) write(`PADOS ${result.pados.product.version} (${result.pados.product.channel})`);
  }
  return 0;
}

function sizeOf(target) {
  if (!fs.existsSync(target)) return 0;
  const stats = fs.lstatSync(target);
  if (!stats.isDirectory()) return stats.size;
  return fs.readdirSync(target).reduce((sum, name) => sum + sizeOf(path.join(target, name)), 0);
}

// Application-only removal deletes runtimes, cache and service state; it keeps config/, data/, logs/ and every
// database. Removing PADOS data is NOT performed by this CLI (deletion authority: pados.lifecycle.purge, and
// databases belong to the operator); --all-data prints that plan only.
function uninstallPlan(home, { allData = false } = {}) {
  const paths = layout(home);
  const remove = [paths.runtime, paths.cache, paths.state].filter((p) => fs.existsSync(p))
    .map((p) => ({ path: p, bytes: sizeOf(p) }));
  const keep = [paths.config, paths.data, paths.logs].filter((p) => fs.existsSync(p));
  const plan = {
    mode: allData ? 'ALL_DATA_PLAN_ONLY' : 'APPLICATION_ONLY', home, remove, keep,
    databases: 'never dropped by the CLI (operator-owned PostgreSQL databases)',
    npm: 'npm uninstall -g @pados-ai/cli',
  };
  if (allData) {
    plan.all_data_steps = [
      'pados down',
      'back up: pg_dump each PADOS database and copy PADOS_HOME/data and PADOS_HOME/config',
      'delete PADOS records through the operator-invoked purge (pados.lifecycle.purge) if required',
      'drop the PADOS databases yourself (DROP DATABASE ...) after verifying the backup',
      `remove ${home} manually`,
      'npm uninstall -g @pados-ai/cli',
    ];
  }
  return plan;
}

async function uninstall(ctx) {
  const plan = uninstallPlan(ctx.home, { allData: Boolean(ctx.flags.allData) });
  const apply = Boolean(ctx.flags.apply) && !ctx.flags.allData;
  if (apply) {
    const state = services.readState(ctx.home);
    if (state && services.isOwned(state)) throw new PadosError('SERVICES_RUNNING');
    for (const item of plan.remove) fs.rmSync(item.path, { recursive: true, force: true });
    plan.applied = true;
  } else {
    plan.applied = false;
  }
  if (ctx.json) write(JSON.stringify(plan, null, 2));
  else {
    write(plan.mode === 'APPLICATION_ONLY'
      ? `${plan.applied ? 'Removed' : 'Would remove'} (application only):`
      : 'Removing all PADOS data is not automated. Plan:');
    for (const item of plan.remove) write(`  - ${item.path} (${item.bytes} bytes)`);
    write('Kept (your data and configuration):');
    for (const item of plan.keep) write(`  + ${item}`);
    write(`Databases: ${plan.databases}`);
    if (plan.all_data_steps) plan.all_data_steps.forEach((step, i) => write(`  ${i + 1}. ${step}`));
    if (!plan.applied && plan.mode === 'APPLICATION_ONLY') write('Dry run. Apply with: pados uninstall --apply');
    write(`Then remove the CLI: ${plan.npm}`);
  }
  return 0;
}

function reserved(name) {
  return async (ctx) => {
    const message = `pados ${name}: not yet available in this version.`;
    write(ctx.json ? JSON.stringify({ status: 'NOT_YET_AVAILABLE', command: name }) : `${message} See: pados doctor`);
    return 3;
  };
}

module.exports = { version, uninstall, uninstallPlan, reserved, RESERVED, UNINSTALL_SPEC: { booleans: ['apply', 'all-data'], values: [] } };
