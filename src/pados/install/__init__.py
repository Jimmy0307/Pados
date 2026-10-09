# SPDX-License-Identifier: AGPL-3.0-only
"""Installation glue for the npm bootstrap (``@pados-ai/cli``).

npm is the installation / bootstrap / UX entrypoint only. This package is the thin Python side the Node CLI
shells out to: PADOS_HOME resolution, loading the one PADOS configuration file into the process environment
(Core's ``Settings`` stays the only reader of PADOS_* keys), the storage-mode -> ``StorageBudget`` mapping,
a machine-readable doctor composed from the existing health functions, migration checks through the existing
Alembic configuration, and ``serve`` (the existing API app). It is not a scheduler, router, configuration,
storage, health or runtime authority.
"""
