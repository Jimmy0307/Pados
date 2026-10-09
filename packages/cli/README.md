# @pados-ai/cli

Install and run PADOS on your own machine.

```sh
npm install -g @pados-ai/cli
pados init      # installs the runtime into PADOS_HOME, writes configuration, migrates a reachable PostgreSQL
pados up        # starts PADOS in the background and prints the UI address
pados doctor    # PASS / WARNING / BLOCKED per area
pados down
```

Or without a global install: `npx @pados-ai/cli init`.

This package is only the installer and launcher. It ships no Python code and downloads nothing at install
time; the matching platform package (`@pados-ai/runtime-win32-x64` or `@pados-ai/runtime-linux-x64`) carries
the runtime. PADOS never installs PostgreSQL, model runtimes or models for you and never contacts a cloud
service on its own.

You need PostgreSQL 16+ with the pgvector extension. Passwords are never written to configuration: export
`PADOS_DB_PASSWORD` (or point `--db-password-ref` at another variable, or use a libpq passfile).

Documentation: `docs/install/USER_INSTALLATION_GUIDE.md` and `docs/install/TROUBLESHOOTING.md` in the PADOS
repository.

License: AGPL-3.0-only (see LICENSE and NOTICE). The PADOS name and logo are not licensed.
