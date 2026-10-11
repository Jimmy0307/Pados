# SPDX-License-Identifier: AGPL-3.0-only
"""PostgreSQL integration gate for the RC2 invariants (D1 approval binding, D4 loop handoff, migrations).

Usage: postgres_integration_gate.py OUTDIR [--recreate] [--suite NAME ...]

Creates the disposable databases below on a LOOPBACK PostgreSQL named by ``PADOS_DB_HOST``/``PADOS_DB_PORT``/
``PADOS_DB_USER``/``PADOS_DB_PASSWORD``, migrates them with this checkout's Alembic scripts to the declared
heads, then runs each suite through ``regression_gate.py`` with skips refused (an integration test that skips
for lack of a database is a failure here, never a pass). Every database name is fixed and disposable; an
existing one is only dropped with ``--recreate``. The password is passed to child processes through the
environment and is never printed or written to the reports.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados import config as pados_config  # noqa: E402

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
OPS = "pados_ci_rc2_ops_test"          # D1 suite requires a *_test loopback database
D4 = "pados_rc2_d4_ci"                 # D4 suite requires a pados_rc2_d4_* loopback database
RESIDUAL = "pados_residual_plan_test"  # residual amendment suite requires exactly this name
PROOF = "pados_ci_rc2_proof"
MIGRATION = "pados_fc_migration_ci"   # migration suites require a pados_fc_migration_* database
DATABASES = (OPS, D4, RESIDUAL, PROOF, MIGRATION)
# Minimum passing tests per suite (the RC2 acceptance counts), so a silently shrunken suite fails the gate.
MIN_TESTS = {"d1-binding": 16, "d4-handoff": 6, "residual": 3, "proof-lane": 17, "p6-11": 7, "migration": 5}


def _connection() -> dict[str, str]:
    values = {key: os.environ.get(f"PADOS_DB_{key.upper()}", "") for key in ("host", "port", "user", "password")}
    values["port"] = values["port"] or "5432"
    if values["host"] not in LOOPBACK or not values["user"] or not values["password"]:
        raise SystemExit("POSTGRES_GATE_REQUIRES_LOOPBACK_DATABASE_CREDENTIALS")
    return values


def _url(connection: dict[str, str], database: str) -> str:
    return (f"postgresql+psycopg://{quote(connection['user'])}:{quote(connection['password'])}"
            f"@{connection['host']}:{connection['port']}/{database}")


def _prepare(connection: dict[str, str], recreate: bool) -> None:
    import psycopg
    from psycopg.conninfo import make_conninfo

    def conninfo(database: str) -> str:
        return make_conninfo(host=connection["host"], port=connection["port"], user=connection["user"],
                             password=connection["password"], dbname=database)

    with psycopg.connect(conninfo("postgres"), autocommit=True) as admin:
        for database in DATABASES:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
            if exists and not recreate:
                raise SystemExit(f"POSTGRES_GATE_DATABASE_EXISTS {database} (pass --recreate to replace it)")
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{database}"')
    for database in DATABASES:
        with psycopg.connect(conninfo(database), autocommit=True) as target:
            target.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", PADOS_ENV="test", **extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, check=False).returncode


def _migrate(database: str, *targets: str, config: str = "alembic.ini") -> None:
    for target in targets:
        if _run(["-m", "alembic", "-c", config, "upgrade", target], _env(PADOS_DB_NAME=database)):
            raise SystemExit(f"POSTGRES_GATE_MIGRATION_FAILED {database} {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--suite", action="append", choices=sorted(MIN_TESTS))
    args = parser.parse_args(argv)
    connection = _connection()
    args.outdir.mkdir(parents=True, exist_ok=True)
    _prepare(connection, args.recreate)
    for database in (OPS, D4, RESIDUAL):
        _migrate(database, "heads")
    _migrate(PROOF, *pados_config.PROOF_FORMAL_MAIN_HEADS)
    _migrate(PROOF, "head", config="alembic_p6_19b.ini")
    proof_lane = {"PADOS_TEST_OPERATIONAL_DATABASE": OPS, "PADOS_DB_NAME": PROOF, "PADOS_PRODUCT_DATABASE_NAME": PROOF}
    ops_lane = {"PADOS_TEST_OPERATIONAL_DATABASE": OPS, "PADOS_DB_NAME": OPS, "PADOS_PRODUCT_DATABASE_NAME": PROOF}
    suites: dict[str, tuple[list[str], dict[str, str]]] = {
        "d1-binding": (["tests/integration/test_rc2_approval_binding_postgres.py"],
                       {"PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL": _url(connection, OPS)}),
        "d4-handoff": (["tests/integration/test_rc2_loop_model_handoff_postgres.py"],
                       {"PADOS_RC2_D4_TEST_DATABASE_URL": _url(connection, D4)}),
        "residual": (["tests/integration/test_residual_plan_amendment_postgres.py"],
                     {"PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL": _url(connection, RESIDUAL)}),
        "proof-lane": (["tests/integration/test_final_convergence_product_loop_postgres.py",
                        "tests/integration/test_final_closure_approval_binding.py",
                        "tests/integration/test_remote_live_ports.py"], proof_lane),
        "p6-11": (["tests/integration/test_p6_11_controller_postgres.py", "tests/integration/test_p6_11_gap_postgres.py",
                   "tests/integration/test_p6_11_human_gate_postgres.py",
                   "tests/integration/test_p6_11_outcome_postgres.py"], ops_lane),
        "migration": (["tests/integration/test_rc2_migration_0018.py",
                       "tests/integration/test_final_convergence_migration_0017.py"],
                      {"PADOS_FC_MIGRATION_TEST_DATABASE": MIGRATION, "PADOS_DB_NAME": MIGRATION}),
    }
    selected = args.suite or list(suites)
    results: dict[str, int] = {}
    for name in selected:
        paths, extra = suites[name]
        results[name] = _run([str(ROOT / "scripts/release/regression_gate.py"), f"postgres-{name}",
                              str(args.outdir / f"postgres-{name}.json"), "--no-skips",
                              "--min-tests", str(MIN_TESTS[name]), "--basetemp", str(args.outdir / f"pt-{name}"),
                              *paths], _env(**extra))
    summary = {"status": "PASS" if not any(results.values()) else "FAIL",
               "suites": {name: ("PASS" if code == 0 else "FAIL") for name, code in results.items()}}
    (args.outdir / "postgres-integration.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                                           encoding="utf-8")
    print(f"POSTGRES_INTEGRATION {summary['status']} {summary['suites']}")
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
