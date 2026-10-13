# PADOS npm installation architecture (NPM_INSTALLATION_V1)

Status: alpha, local packages only. Nothing in this design publishes to the npm registry; CI packs and tests
tarballs and never runs `npm publish`.

## 1. Principle: npm is the entrypoint, not an authority

`npm install -g @pados-ai/cli` gives the user one command, `pados`, that installs, configures, starts, stops,
diagnoses and updates a local PADOS. The Node CLI is a **launcher**. Everything that decides something about PADOS
state is done by the PADOS Python runtime, through code that already existed:

| Concern | Single authority (unchanged) | What the Node CLI does |
|---|---|---|
| Configuration | `pados.config.Settings` reads `PADOS_*` keys | writes the one env file once (`init`), never re-interprets it; Core validates it (`python -m pados.install validate-config`) |
| Database schema | the existing Alembic graph (`alembic.ini`, `migrations/`), targets from `Settings.operational_revision` / `corpus_revision` | asks the runtime (`migrate --check` / `migrate`) |
| Health / readiness | `pados.db.check_database`, `pados.operations.service.system_health`, `pados.execution.readiness.readiness`, `security_posture` | renders the runtime's doctor JSON |
| Storage policy | `pados.lifecycle.storage_flexibility` (`StorageBudget`, `pressure_of`) | passes the chosen mode; the mapping lives in `pados.install.storage_profile.budget_for_mode` |
| Device / runtime discovery | `pados.local_intelligence.device.DeviceProfiler`, `runtimes.describe_runtime` (loopback only) | shows the result |
| Serving | `pados.api.app.create_app` (+ static UI mount) | starts/stops the process |
| Deletion | operator-invoked `pados.lifecycle.purge` | never deletes PADOS data or databases |

The Python glue is `src/pados/install/` (`python -m pados.install doctor|migrate|storage-profile|validate-config|serve|version`).
It is composition only: no scheduler, router, configuration, storage, health or runtime authority is added.

## 2. Packages

| Package | Contents | Size (this build) |
|---|---|---|
| `@pados-ai/cli` | pure Node (>= 20), zero dependencies, no install scripts | 33.9 KB tarball, 106.7 KB unpacked, 18 files |
| `@pados-ai/runtime-win32-x64` | relocatable CPython 3.12.15, locked wheels, PADOS Core source, Alembic graph, prebuilt UI, licenses, LGPL sources | 41.1 MB tarball, 127.8 MB unpacked, 6,138 runtime files |
| `@pados-ai/runtime-linux-x64` | same for glibc x86-64 (pure-Python psycopg over system libpq, see section 9) | 53.5 MB tarball, 160.8 MB unpacked, 5,354 runtime files |
| `@pados-ai/runtime-darwin-arm64` | design-ready: package metadata and pinned CPython archive; not built or accepted | n/a |

Selection follows the esbuild/swc pattern: the CLI lists every runtime package in `optionalDependencies` with the
**exact** same version; each runtime package declares `os`/`cpu` (and `libc: glibc` on Linux), so npm installs only
the matching one. At run time `lib/platform.js` resolves `@pados-ai/runtime-<platform>-<arch>` from the CLI's own
location, then requires `package.version === cli version` and `runtime-manifest.json` to name the same platform
and version. `PADOS_RUNTIME_PACKAGE_DIR` (development-only, section 13) points the CLI at an extracted runtime
package. No `postinstall` script exists anywhere, so `npm install --ignore-scripts` changes nothing.

## 3. Runtime packaging decision (PC-H02)

Options evaluated:

1. **Relocatable standalone CPython + locked wheels + PADOS source (chosen).** python-build-standalone
   (astral-sh, release 20261003; build scripts under MPL-2.0, distributions carry CPython's PSF license and each
   component's license, which we ship) `install_only_stripped` archives, pinned by URL and SHA-256 in
   `packages/scripts/build_runtime.py`. Dependencies are downloaded at build time **only by hash**: the committed
   per-platform files `packages/scripts/runtime-wheels-<platform>.txt` list every bundled wheel as
   `name==version --hash=sha256:<hex>`, and the build runs `pip download --require-hashes --no-deps` and
   `pip install --no-index --require-hashes --no-deps` from them, re-checks every downloaded file against the list
   (`WHEEL_NOT_LISTED`, `WHEEL_HASH_MISMATCH`, `WHEEL_MISSING` fail the build), runs `pip check` (the list must be
   dependency-complete), and checks every version against `requirements.lock` (plus
   `packages/scripts/runtime-extra.lock` for one runtime pin the lock lacks); a stale list fails with
   `HASHED_LOCK_STALE`.
   PADOS Core is shipped as source under `runtime/app/src` and put on `sys.path` by a relative `.pth` file, which
   keeps the existing `WORKTREE_IMPORT_PATH_MISMATCH` guard in `migrations/env.py` true. Bytecode is precompiled with
   `--invalidation-mode unchecked-hash` and the runtime runs with `-I -B`, so an installed runtime stays
   byte-identical to its manifest. Bytecode is compiled with a neutral source path (`python -B -m compileall -f
   -s <runtime root> -p pados-runtime`), so no `.pyc` records the build machine's paths; the import system substitutes the real
   source path when it loads a `.pyc`, so tracebacks still point at the installed files. The user never creates a virtualenv and nothing touches PyPI at install time.
2. **PyInstaller / Nuitka single binaries (rejected).** Alembic loads migration scripts from the filesystem,
   uvicorn and pydantic use dynamic imports, and the migration guard compares real file paths; freezing would need
   per-module hooks and would hide the migration graph inside an archive. Nuitka adds a C toolchain to every
   build. Both make LGPL replaceability (section 9) harder because libraries are linked into one artifact.
3. **pip install from PyPI at `pados init` (rejected).** Needs the network at install time and resolves wheels on
   the user's machine; not offline-capable and not reproducible.

Build: `python packages/scripts/build_runtime.py --platform <linux-x64|win32-x64> --web-dist web/dist` on the target
platform, then `node packages/scripts/pack.js --out DIR --packages cli,runtime-<platform>`. The pack step stages
packages outside the repository (so repository ignore rules cannot drop runtime files), re-verifies the runtime
digest, normalizes CLI line endings and writes `.sha256` sidecars and a pack report. Before packing, `pack.js`
scans every staged file as bytes (UTF-8 and UTF-16LE, case-insensitive; file names too) for the absolute build
root (both separator styles), the user profile directory and the build root's directory name (a worktree name;
generic checkout names such as `pados` are covered by the absolute path only) and **refuses to pack** on any hit
(`PAYLOAD_CONTAINS_BUILD_PATH`); `PADOS_PACK_FORBIDDEN_PATHS` adds further strings. On a hosted CI runner
(`GITHUB_ACTIONS=true`) the profile directory is the public runner account that upstream binaries legitimately embed
(for example `/home/runner` in pydantic-core's SBOM), so there only the build root (the runner workspace) is checked. The result is recorded as
`build_path_scan` in the pack report.

Updating the hashed wheel lock (whenever `requirements.lock`, `runtime-extra.lock`, `pyproject.toml` dependencies or
the pinned CPython change):

1. On each target platform run `python packages/scripts/build_runtime.py --platform <platform> --lock`. It extracts
   the pinned CPython, resolves the runtime requirements under the lock with `pip download --only-binary=:all:`
   into an empty directory and rewrites `packages/scripts/runtime-wheels-<platform>.txt` (sorted, no timestamps).
2. Review the diff: only the intended names, versions and hashes may change; compare new hashes with the files'
   digests published on PyPI.
3. Commit the updated file(s) together with the lock change; the normal build then refuses anything else.

The PostgreSQL notice for the libpq bundled in `psycopg-binary` is vendored as
`packages/scripts/vendor/libpq-PostgreSQL-COPYRIGHT.txt` (tag `REL_17_11`, commit
`083ac033419f690758508e08c1736089384bbee8`) and checked against its SHA-256 at build time; the build fetches no
unpinned text.

`runtime/app/runtime-manifest.json` records the platform, package and product version, the CPython archive and its
SHA-256, the source commit, every bundled distribution with its wheel SHA-256, removed components (Tk/Tcl, IDLE,
ensurepip, pip, headers), and a digest over (path, size, SHA-256) of every runtime file. The same algorithm is
implemented in `build_runtime.py` and `packages/cli/lib/runtime.js`.

## 4. PADOS_HOME (PC-H03)

| Platform | Default | Reason |
|---|---|---|
| Windows | `%LOCALAPPDATA%\PADOS` | per-user, non-roaming (runtimes and local state must not roam) |
| Linux | `$XDG_DATA_HOME/pados`, else `~/.local/share/pados` | XDG base-directory convention; one root keeps backup, uninstall and data ownership simple |
| macOS | `~/Library/Application Support/PADOS` | platform convention |

`PADOS_HOME` overrides the default and must be absolute. The whole home is owner-only on every platform: POSIX
`0700` on the root and `config/`; on Windows `pados init` gives the root an explicit, non-inherited ACL for the
current user (`icacls /inheritance:r /grant:r <user>:(OI)(CI)F`) that `data/`, `logs/`, `state/`, `runtime/` and
`cache/` inherit, wherever `PADOS_HOME` points, and `config/` gets its own explicit ACL. On Windows "owner-only"
is the explicit contract in `packages/cli/lib/home.js` (`classifyWindowsAcl`): the exact current account
(`DOMAIN\user`) must be present; the OS-trusted principals `NT AUTHORITY\SYSTEM` (S-1-5-18) and
`BUILTIN\Administrators` (S-1-5-32-544) may also be present (by qualified name or SID only). Both are on every
Windows user profile, an elevated (UAC-off) administrator such as a GitHub-hosted runner keeps them explicitly,
and both already control the machine, so their entries grant nothing new. Every other principal fails, and broad
identities (`Everyone`, `BUILTIN\Users`, `Authenticated Users`, `Guests`, `ANONYMOUS LOGON`, `INTERACTIVE`,
`NETWORK`) are rejected by name and SID. PADOS itself grants only the current account. The drive-root `pados` directory of the system drive
(the developer checkout location) is always refused. The Node and Python resolvers are tested against the same
vectors (`packages/cli/test/fixtures/home-vectors.json`).

```
PADOS_HOME/
  config/     pados.env (PADOS_* keys), control.token, storage-profile.json   owner-only (0700/0600; Windows ACL: owner [+SYSTEM, Administrators])
  runtime/    <version>/ (verified copy of the runtime package), current.json (active + previous + history)
  data/       workspace/, objects/ (PADOS_PRODUCT_WORKSPACE_ROOT / PADOS_PRODUCT_OBJECT_ROOT)
  logs/       api-<timestamp>.out.log / .err.log
  cache/      EPHEMERAL (rebuildable; removed by application-only uninstall)
  state/      services.json (pid, instance id, endpoint) for up/down/status
```

## 5. Configuration and secrets (PC-H04)

`pados init` writes `config/pados.env` once, in the format Core already reads. The process environment overrides it
(the precedence pydantic-settings already applies to `.env`). Credentials are never written:

- `PADOS_DB_PASSWORD` is always empty in the file. `PADOS_DB_PASSWORD_REF` is a SecretRef in the existing PADOS
  syntax: `secretref://env/<NAME>` (resolved in memory at launch from that environment variable) or
  `secretref://libpq/passfile` (libpq reads `PGPASSFILE`/the default passfile, or the server uses trust/peer
  authentication; `PADOS_DB_PASSFILE` may name the file). An empty value never falls back to Core's development
  default password.
- `--db-password` is refused; credential-shaped values are refused before anything is written.
- The local control token (Core's existing `pados init` behaviour) is generated into `config/control.token`
  (owner-only) and only its SHA-256 goes into the configuration. This is the one locally generated credential; it
  never leaves the machine and is not a user credential.
- Doctor reports `SECRET_IN_CONFIG` if a credential appears in the file and checks owner-only modes on POSIX.

Database names default to `pados_app` (operational) and `pados_app_corpus` (corpus); the developer databases
(`pados_local_runtime`, `pados_corpus_runtime`, `pados_proof_runtime`) are refused by the existing doctor rule. The
optional proof lane is not migrated automatically (it needs the explicit P6.19B sequence in
`docs/product/QUICKSTART.md`), so readiness reports `CORE_DEGRADED` / `PROOF_DB_UNCONFIGURED` until an operator sets
it up. This is reported honestly, not hidden.

## 6. Storage modes (PC-H05)

`--storage LIGHTWEIGHT|STANDARD|FULL_LOCAL` (default `LIGHTWEIGHT`) maps onto `StorageBudget` fields in exactly one
function, `pados.install.storage_profile.budget_for_mode`; the profile file is a plain document of those fields so
configuration loading can read it without the installer.

| Mode | min_free_bytes | cache_budget_bytes | max_local_residency_bytes |
|---|---|---|---|
| LIGHTWEIGHT | 2 GiB | 512 MiB | 64 MiB (larger artifacts by reference) |
| STANDARD | 5 GiB | 4 GiB | 1 GiB |
| FULL_LOCAL | 10 GiB | unlimited | unlimited |

Model weights are always `EXTERNAL_REFERENCE` (runtime-managed, never copied). `FULL_LOCAL` and any installation over
1 GiB need `--yes` (or interactive consent). `--min-free-bytes` overrides the reserve; init refuses when free space
is below runtime size plus reserve, and doctor turns `pressure_of` into PASS / WARNING / BLOCKED.

## 7. Commands

| Command | Behaviour |
|---|---|
| `init` | resolve and verify the runtime package, check disk, copy it into `runtime/<version>`, write config + token + storage profile, validate config with Core, check the database lanes; migrate them through Alembic when reachable (`--create-databases` creates missing lane databases); print doctor. Re-running keeps configuration (`--reconfigure` rewrites it; data is never deleted). Never installs PostgreSQL, model runtimes or models and never contacts a cloud service. |
| `doctor` | PASS / WARNING / BLOCKED for core, configuration, database, migrations, storage, disk_pressure, device, runtime, models, network, providers, readiness, security; `--json`; no secrets |
| `up` | preflight (`migrate --check`, port), start `python -I -B -m pados.install serve --instance-id <uuid>` detached, wait for `/health` = ok, print UI (`/ui/`) and API (`/docs`) |
| `down` | stop only a process whose command line carries the recorded instance id and `pados.install`; otherwise refuse and clear the stale record |
| `status` | service record, identity check, `/health`, `/ready` |
| `update` | activate the runtime shipped with this CLI (section 8) |
| `version` | CLI, runtime package, active runtime, PADOS versions |
| `uninstall` | application-only removal (runtimes, cache, state) as a dry run unless `--apply`; `--all-data` prints a manual plan only |
| `runtime`, `model`, `storage`, `node`, `config` | reserved, print "not yet available" (exit 3) |

Failure UX: expected failures print `PADOS could not start.` / `Cause: ...` / `Suggested action: ...` / `Code: ...`,
exit non-zero and show no stack trace; `--debug` or `PADOS_DEBUG=1` adds details and the stack. The Python side
returns coded JSON (`{"ok": false, "code": ...}`) with the same rule.

## 8. Update and rollback (PC-H06)

`npm install -g @pados-ai/cli@<new>` installs the new CLI and runtime package; `pados update` then:

1. refuses while PADOS is running (`pados down` first);
2. copies and verifies the new runtime into `runtime/<new>` (the previous one stays);
3. runs the **new** runtime's `migrate --check`: `CURRENT` -> switch; `UPGRADE_AVAILABLE` -> apply the Alembic
   upgrade to the new targets, then switch; `INCOMPATIBLE` / unreachable -> stop, keep the old version active.
   A forward migration needs explicit consent: `--migrate` or `--yes`, or an interactive yes to a prompt that
   shows the pending lanes and the `pg_dump` backup advice. Without it (for example in a script) update refuses
   with `MIGRATION_CONSENT_REQUIRED`: nothing is migrated and the previous runtime stays active. `--no-migrate`
   always refuses;
4. records `{version, previous, history}` in `runtime/current.json`.

`pados update --rollback` re-activates the previous runtime only if its own `migrate --check` says the database is
`CURRENT` for it; if the newer version migrated the schema forward, rollback is refused with
`ROLLBACK_INCOMPATIBLE` and the instruction to restore a database backup. Database, semantic state, receipts,
configuration and artifacts are never modified by update except through the Alembic upgrade.

## 9. Licensing of the bundles (PC-H07)

- Packages declare `"license": "AGPL-3.0-only"` (owner decision OD-1) and ship the repository `LICENSE` (the verbatim
  GNU AGPL v3 text) and `NOTICE`. `pack.js` refuses or warns if the `LICENSE` text and the declared license ever
  disagree, and the pack report records the check.
- Each runtime package ships `THIRD_PARTY_NOTICES.md` (component, version, license from package metadata) and
  `runtime/licenses/` with the CPython license, the python-build-standalone component licenses, every bundled
  distribution's license files, the web UI licenses (`THIRD_PARTY_LICENSES/`) and native-library texts.
- **psycopg (LGPL-3.0-only).** Shipped unmodified and replaceable, used through its public API; its sdists
  (`psycopg`, and `psycopg-c` for `psycopg-binary`) are included in `runtime/sources/` as corresponding source.
- **Windows:** `psycopg-binary` is bundled. Its vendored native libraries are libpq (PostgreSQL License) and OpenSSL 3
  (Apache-2.0); both texts are in `runtime/licenses/native/`.
- **Linux:** the manylinux `psycopg-binary` wheel also vendors LGPL libraries (libcrypt, libkeyutils) whose exact
  corresponding source we cannot pin, so the compliant option is chosen: the Linux runtime ships pure-Python psycopg
  over the distribution's own libpq. Doctor reports `DATABASE_DRIVER_UNAVAILABLE` with the package to install if
  libpq is missing; PADOS never installs it.
- certifi (MPL-2.0) is shipped in source form (its covered files are the source).
- Residual: legal review before any public distribution, as the third-party license audit already requires.

## 10. Offline

After `npm install`, nothing needs the npm registry or PyPI: the runtime is inside the platform package and is copied
from there. Offline bundle path (future): ship the CLI tarball, the platform runtime tarball and their `.sha256`
files together; install with `npm install -g --offline <runtime>.tgz <cli>.tgz` (tested in acceptance), or extract the
runtime tarball and set `PADOS_RUNTIME_PACKAGE_DIR` before `pados init` (development-only, section 13).

## 11. Security

- API binds to loopback only (Core's `assert_localhost_binding`); `pados init` refuses non-loopback hosts.
- Config, token and storage profile are owner-only; `down` verifies process identity before signalling.
- The CLI and runtime never contact a cloud service; runtime discovery probes loopback endpoints only, and the device
  profiler's route check sends no packet.
- Child processes run with `-I -B` (no user site, no `PYTHONPATH`/`PYTHONHOME` influence, no bytecode writes).
- No install scripts; CI has no publish step, `permissions: {}` and SHA-pinned actions.

## 12. Platforms

| Platform | Status |
|---|---|
| Linux x64 (glibc) | built, packed, fresh install / init / doctor / up / down / restart persistence / update / rollback accepted |
| Windows x64 | built, packed, fresh install / init / doctor / up / down / restart persistence / update / rollback accepted (database reached through an SSH-forwarded loopback port to a disposable pgvector container) |
| macOS arm64 | design-ready (package, pinned archive, PADOS_HOME rule); not built; CI job present but disabled |

## 13. Development-only overrides

These environment variables exist for developing and diagnosing the CLI. They are honoured by the shipped CLI (the
environment is trusted), but every `pados` command prints a `WARNING: <name> is set (development-only override ...)`
line on stderr while one is set. Do not set them for normal use.

| Variable | Effect |
|---|---|
| `PADOS_CLI_DEV_PYTHON` + `PADOS_CLI_DEV_APP` | run PADOS with any interpreter and app directory instead of the installed, digest-verified runtime |
| `PADOS_RUNTIME_PACKAGE_DIR` | use an extracted runtime package directory instead of the npm-installed one (its manifest is self-attested) |
