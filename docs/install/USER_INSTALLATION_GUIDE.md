# Installing PADOS with npm

PADOS runs on your own machine. The npm package installs it, starts it and checks it. It does not install
PostgreSQL, model runtimes or models for you, and it never connects to a cloud service on its own.

> Alpha. The packages are built and tested locally; they are not on the public npm registry yet. Until they are,
> install from the tarballs you were given (see "Installing from tarballs").

## What you need

| Requirement | Notes |
|---|---|
| Windows 10/11 x64 or Linux x64 (glibc) | macOS arm64 is prepared but not available yet |
| Node.js 20 or newer | only for the `pados` command itself |
| PostgreSQL 16+ with the pgvector extension | local or on your network; PADOS uses two databases (operational and corpus) |
| Linux only: your distribution's libpq | Debian/Ubuntu `libpq5`, Fedora/RHEL `libpq` (usually already installed with PostgreSQL clients) |
| About 200 MB for the runtime plus your data | the default storage mode keeps a 2 GiB free-space reserve |

No Python installation is needed. PADOS brings its own.

## Install

```sh
npm install -g @pados-ai/cli
```

or run it without installing: `npx @pados-ai/cli init`.

### Installing from tarballs

```sh
npm install -g --offline pados-ai-runtime-linux-x64-0.1.0-alpha.1.tgz pados-ai-cli-0.1.0-alpha.1.tgz
```

(use `pados-ai-runtime-win32-x64-...` on Windows). Check a tarball first with its `.sha256` file:
`sha256sum -c pados-ai-cli-0.1.0-alpha.1.tgz.sha256` (Linux) or
`Get-FileHash pados-ai-cli-0.1.0-alpha.1.tgz` (PowerShell).

## Set up

1. Give PADOS the database password through the environment, never on the command line or in a file:

   ```sh
   export PADOS_DB_PASSWORD='...'          # Linux/macOS
   $env:PADOS_DB_PASSWORD = '...'          # PowerShell
   ```

   Prefer another variable name? `pados init --db-password-ref secretref://env/MY_PG_PASSWORD`.
   Prefer a libpq passfile? `pados init --db-password-ref secretref://libpq/passfile --db-passfile ~/.pgpass`.

2. Initialize:

   ```sh
   pados init
   ```

   You are asked for a storage mode (press Enter for `LIGHTWEIGHT`). For scripts use flags:

   ```sh
   pados init --non-interactive --storage LIGHTWEIGHT --db-host 127.0.0.1 --db-port 5432 --db-user pados --db-name pados_app
   ```

   - `--create-databases` creates the two PADOS databases if your database user may create databases.
     Otherwise create `pados_app` and `pados_app_corpus` yourself (`CREATE DATABASE ...`).
   - `--api-port 8088` chooses the local port (loopback only).
   - `--storage STANDARD` or `FULL_LOCAL` keeps more data locally; `FULL_LOCAL` asks for `--yes`.

   `init` installs the runtime into your PADOS home, writes the configuration, applies database migrations and
   ends with a doctor report. Running it again keeps your configuration and data.

3. Start:

   ```sh
   pados up
   ```

   It prints the UI address (`http://127.0.0.1:8088/ui/`) and the API documentation (`/docs`).

4. Stop: `pados down`. Status: `pados status`. Health report: `pados doctor`.

## Where things live

| Platform | PADOS home |
|---|---|
| Windows | `%LOCALAPPDATA%\PADOS` |
| Linux | `~/.local/share/pados` (or `$XDG_DATA_HOME/pados`) |
| macOS | `~/Library/Application Support/PADOS` |

Set `PADOS_HOME` to use another directory. Inside: `config/` (your configuration, owner-only), `runtime/` (installed
versions), `data/`, `logs/`, `cache/` (safe to delete), `state/`.

The local control token is in `config/control.token`. Tools that call the control API send it in the
`X-PADOS-Control-Token` header. Keep it private.

## Updating

```sh
pados down
npm install -g @pados-ai/cli@latest
pados update
pados up
```

`update` checks the database schema with the new version before switching. If the new version needs to migrate the
database forward, `update` stops and asks first (it shows the pending changes and how to back up). In scripts, or to
skip the question, back up and pass `--migrate` (or `--yes`):

```sh
pg_dump -Fc -d pados_app -f pados_app.dump
pg_dump -Fc -d pados_app_corpus -f pados_app_corpus.dump
pados update --migrate
```

Without that confirmation nothing is migrated and the previous version stays active (`MIGRATION_CONSENT_REQUIRED`).
The previous version stays installed: `pados update --rollback` goes back as long as the database schema still fits
it; after a forward migration, going back means restoring the backup.

## Removing

```sh
pados down
pados uninstall            # shows what would be removed (application only)
pados uninstall --apply    # removes runtimes, cache and service state; keeps config, data, logs and databases
npm uninstall -g @pados-ai/cli
```

Removing your PADOS data is deliberately manual: `pados uninstall --all-data` prints the steps (backup, purge,
dropping your databases, deleting the PADOS home).

## Offline use

After installation PADOS needs neither npm nor the internet. To install on a machine without internet, copy the CLI
tarball, the runtime tarball for that platform and their `.sha256` files, and use the "Installing from tarballs"
command.

See also: [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [INSTALLATION_ARCHITECTURE.md](INSTALLATION_ARCHITECTURE.md).
