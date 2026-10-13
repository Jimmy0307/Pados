'use strict';
// Platform runtime package selection (esbuild/swc pattern): optionalDependencies + os/cpu fields install only
// the matching package; the CLI resolves it at run time by platform + arch + EXACT version.

const fs = require('node:fs');
const path = require('node:path');
const { PadosError } = require('./errors');

const RUNTIME_PACKAGES = Object.freeze({
  'win32-x64': Object.freeze({ name: '@pados-ai/runtime-win32-x64', status: 'supported' }),
  'linux-x64': Object.freeze({ name: '@pados-ai/runtime-linux-x64', status: 'supported' }),
  'darwin-arm64': Object.freeze({ name: '@pados-ai/runtime-darwin-arm64', status: 'design-ready' }),
});

const CLI_ROOT = path.resolve(__dirname, '..');

function platformKey(platform = process.platform, arch = process.arch) {
  return `${platform}-${arch}`;
}

function runtimePackageFor(platform = process.platform, arch = process.arch) {
  const key = platformKey(platform, arch);
  const entry = RUNTIME_PACKAGES[key];
  if (!entry) {
    throw new PadosError('PLATFORM_UNSUPPORTED', {
      cause: `This platform (${key}) is not supported by this PADOS release.`,
      details: { supported: Object.keys(RUNTIME_PACKAGES) },
    });
  }
  return { key, ...entry };
}

function cliPackage() {
  return JSON.parse(fs.readFileSync(path.join(CLI_ROOT, 'package.json'), 'utf8'));
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

// Returns { name, version, dir, runtimeDir, manifest } or throws a PadosError.
function resolveRuntimePackage({
  cliVersion = cliPackage().version, platform = process.platform, arch = process.arch,
  env = process.env, resolve = require.resolve, searchFrom = [CLI_ROOT],
} = {}) {
  const wanted = runtimePackageFor(platform, arch);
  let dir;
  if (env.PADOS_RUNTIME_PACKAGE_DIR) {
    dir = path.resolve(env.PADOS_RUNTIME_PACKAGE_DIR); // offline bundle / extracted tarball
  } else {
    try {
      dir = path.dirname(resolve(`${wanted.name}/package.json`, { paths: searchFrom }));
    } catch {
      throw new PadosError('RUNTIME_PACKAGE_MISSING', { details: { package: wanted.name, platform: wanted.key } });
    }
  }
  let pkg;
  let manifest;
  try {
    pkg = readJson(path.join(dir, 'package.json'));
    manifest = readJson(path.join(dir, 'runtime', 'app', 'runtime-manifest.json'));
  } catch {
    throw new PadosError('RUNTIME_PACKAGE_INVALID', { details: { dir } });
  }
  if (pkg.name !== wanted.name) {
    throw new PadosError('RUNTIME_PACKAGE_INVALID', { cause: `Expected ${wanted.name}, found ${pkg.name}.` });
  }
  if (pkg.version !== cliVersion) {
    throw new PadosError('RUNTIME_VERSION_MISMATCH', {
      cause: `Runtime package ${pkg.version} does not match CLI ${cliVersion}.`,
      details: { runtime: pkg.version, cli: cliVersion },
    });
  }
  if (manifest.platform !== wanted.key || manifest.package_version !== pkg.version) {
    throw new PadosError('RUNTIME_PACKAGE_INVALID', { cause: 'Runtime manifest does not match its package.' });
  }
  return { name: pkg.name, version: pkg.version, dir, runtimeDir: path.join(dir, 'runtime'), manifest };
}

module.exports = { RUNTIME_PACKAGES, CLI_ROOT, platformKey, runtimePackageFor, resolveRuntimePackage, cliPackage, readJson };
