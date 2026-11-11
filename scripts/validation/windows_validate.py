# SPDX-License-Identifier: AGPL-3.0-only
"""Sequential Windows validation of one PADOS worktree at its exact HEAD (CI gates, full pytest, E2E, PG gate).

Usage: python scripts/validation/windows_validate.py <worktree> <out dir> <pg port> [--profile full|core]
       [--pg-bin <PostgreSQL bin dir>]

Every step records its argv (secrets never appear in argv), exit code, duration and log hash. ``<out>/gate-summary.json``
is rewritten after each step, so an interrupted run still leaves a faithful record. A private loopback PostgreSQL
cluster (trust auth on 127.0.0.1, a fresh data directory under ``<out>``) backs the steps that need a database, as the
CI service container does on Linux; it is stopped at the end and its data directory is kept. The out directory must
not exist: evidence is never overwritten. A worktree with tracked changes is refused.

``scripts/validation/windows_lane_records.py`` turns the summary into the lane records that
``scripts/release/runtime_lanes.v1.json`` reads (``windows-ci-gates``, ``windows-full-suite``). The ``full`` profile
also runs the launcher's Pester tests and the web Playwright tests, which are not in CI.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PG_BIN = Path(os.environ.get("PADOS_PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
DETACHED_PROCESS = 0x00000008
DATABASES = ("pados_p6_7_validation", "pados_local_runtime", "pados_corpus_runtime")

# The steps whose exit codes make up the windows-ci-gates lane (the five web steps become its one `frontend` gate).
CI_STEPS = ("ci_policy", "ruff", "mypy", "mypy_win32", "compileall", "d1", "d2", "d4", "migration_gate", "win_native_1",
            "win_native_2", "win_local_intel", "public_export", "secret_scan", "operability_gate", "doc_lint", "npm_cli")
WEB_STEPS = ("web_npm_ci", "web_typecheck", "web_test", "web_lint", "web_build")
D2_SELECTION = (
    "--allow-skip",
    "tests/local_intelligence/test_measured_execution_contract.py::"
    "test_the_accepted_m3_profile_still_loads_with_its_id_and_is_refused",
    "tests/local_intelligence/test_rc2_execution_binding.py",
    "tests/local_intelligence/test_measured_execution_contract.py",
    "tests/unit/test_rc2_graph_measured_binding.py",
)
# Environment-dependent on win32 and identical on pristine main (R19-3); the full suite still runs them.
WIN_NATIVE_2_DESELECT = (
    "tests/security/test_p6_6_adversarial.py::test_illegal_task_transition_new_to_released_is_rejected",
    "tests/security/test_p6_6_adversarial.py::test_index_repository_rejects_roots_outside_registered_project_boundary",
    "tests/security/test_p6_6_adversarial.py::test_single_agent_loop_fails_closed_without_provider",
)


@dataclass
class Run:
    worktree: Path
    out: Path
    port: int
    pg_bin: Path
    profile: str
    summary: dict[str, Any] = field(default_factory=dict)
    # The cluster uses trust auth on 127.0.0.1, so this per-run value authorizes nothing; PADOS settings need one.
    db_password: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)

    @property
    def python(self) -> str:
        return str(self.worktree / ".venv" / "Scripts" / "python.exe")

    @property
    def pgdata(self) -> Path:
        return self.out / "disposable-pgdata"

    def pg_env(self, database: str = "pados_p6_7_validation") -> dict[str, str]:
        return {"PADOS_DB_HOST": "127.0.0.1", "PADOS_DB_PORT": str(self.port), "PADOS_DB_USER": "pados",
                "PADOS_DB_PASSWORD": self.db_password, "PADOS_DB_NAME": database,
                "PADOS_OPERATIONAL_DATABASE_NAME": "pados_local_runtime",
                "PADOS_CORPUS_DATABASE_NAME": "pados_corpus_runtime"}

    def save(self) -> None:
        (self.out / "gate-summary.json").write_text(json.dumps(self.summary, indent=2), encoding="utf-8")

    def step(self, name: str, argv: list[str], *, cwd: Path | None = None, pg_database: str | None = None,
             timeout: int = 7200, shell: bool = False, extra_env: dict[str, str] | None = None) -> int:
        cwd = cwd or self.worktree
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if pg_database is not None:
            env.update(self.pg_env(pg_database))
        env.update(extra_env or {})
        log = self.out / f"{name}.log"
        started = time.monotonic()
        with log.open("w", encoding="utf-8") as handle:
            try:
                code = subprocess.run(argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                      timeout=timeout, shell=shell).returncode
            except subprocess.TimeoutExpired:
                code = -9
                handle.write(f"\nTIMEOUT after {timeout}s\n")
        shown = " ".join(argv)
        self.summary["steps"][name] = {
            "command": shown.replace(str(self.worktree), "<repo>").replace(str(self.out), "<out>"),
            "cwd": str(cwd).replace(str(self.worktree), "<repo>"), "uses_pg": pg_database is not None,
            "exit_code": code, "extra_env": extra_env or {}, "seconds": round(time.monotonic() - started, 1),
            "log": log.name, "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
        self.save()
        return code

    def pg_ready(self) -> bool:
        return subprocess.run([str(self.pg_bin / "pg_isready.exe"), "-h", "127.0.0.1", "-p", str(self.port),
                               "-U", "pados"], capture_output=True).returncode == 0


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def pg_up(run: Run) -> None:
    run.step("pg_initdb", [str(run.pg_bin / "initdb.exe"), "-D", str(run.pgdata), "-U", "pados", "--auth=trust",
                           "-E", "UTF8", "--no-locale"])
    # Detached and without pipes: a foreground `pg_ctl start` keeps the caller's console handles open.
    subprocess.Popen([str(run.pg_bin / "pg_ctl.exe"), "-D", str(run.pgdata), "-l",
                      str(run.out / "disposable-postgres.log"), "-o", f"-p {run.port} -c listen_addresses=127.0.0.1",
                      "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=DETACHED_PROCESS)
    for _ in range(60):
        if run.pg_ready():
            break
        time.sleep(1)
    provision = ("import os,psycopg\nfrom psycopg import sql\n"
                 # trust auth on 127.0.0.1: provisioning connects without a password
                 "kw=dict(host='127.0.0.1',port=int(os.environ['PADOS_DB_PORT']),user='pados')\n"
                 f"dbs={DATABASES!r}\n"
                 "with psycopg.connect(dbname='postgres',autocommit=True,**kw) as a:\n"
                 "    [a.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(d))) for d in dbs]\n"
                 "for d in dbs:\n"
                 "    with psycopg.connect(dbname=d,autocommit=True,**kw) as c: "
                 "c.execute('CREATE EXTENSION IF NOT EXISTS vector')\n")
    run.step("pg_provision", [run.python, "-c", provision], pg_database="pados_p6_7_validation")
    run.step("pg_migrate_heads", [run.python, "-m", "alembic", "upgrade", "heads"],
             pg_database="pados_p6_7_validation")
    for database, attr in (("pados_local_runtime", "OPERATIONAL_REVISION_DEFAULT"),
                           ("pados_corpus_runtime", "CORPUS_REVISION_DEFAULT")):
        revision = subprocess.run([run.python, "-c", f"from pados.config import {attr} as r; print(r)"],
                                  cwd=run.worktree, capture_output=True, text=True, check=True).stdout.strip()
        run.step(f"pg_migrate_{database}", [run.python, "-m", "alembic", "upgrade", revision], pg_database=database)


def pg_down(run: Run) -> None:
    run.step("pg_stop", [str(run.pg_bin / "pg_ctl.exe"), "-D", str(run.pgdata), "-m", "fast", "stop"])


def ci_steps(run: Run) -> list[tuple[str, Callable[[], int]]]:
    """The windows-ci-gates steps, in order. Their names are pinned to the lane registry by a test."""
    py, out = run.python, run.out

    def pytest(name: str, *args: str) -> Callable[[], int]:
        return lambda: run.step(name, [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args,
                                       "--junitxml", str(out / f"{name}.xml")])

    def gate(name: str, label: str, minimum: str, *paths: str) -> Callable[[], int]:
        return lambda: run.step(name, [py, "scripts/release/regression_gate.py", label, str(out / f"{name}.json"),
                                       "--no-skips", "--min-tests", minimum, *paths])

    web = run.worktree / "web"
    web_commands = {"web_npm_ci": "npm ci --ignore-scripts --no-audit --no-fund", "web_typecheck": "npm run typecheck",
                    "web_test": "npm test", "web_lint": "npm run lint", "web_build": "npm run build"}
    steps: list[tuple[str, Callable[[], int]]] = [
        ("ci_policy", lambda: run.step("ci_policy", [py, "scripts/release/ci_policy_check.py",
                                                     str(out / "ci-policy.json")])),
        ("ruff", lambda: run.step("ruff", [py, "-m", "ruff", "check", "src", "tests"])),
        ("mypy", lambda: run.step("mypy", [py, "-m", "mypy", "src"])),
        ("mypy_win32", lambda: run.step("mypy_win32", [py, "-m", "mypy", "--platform", "win32", "src/pados/execution",
                                                       "src/pados/product_loop", "src/pados/enterprise",
                                                       "src/pados/compute_node", "src/pados/release"])),
        ("compileall", lambda: run.step("compileall", [py, "-m", "compileall", "-q", "src", "tests"])),
        ("migration_gate", lambda: run.step("migration_gate", [py, "scripts/release/migration_gate.py",
                                                               str(out / "migration-gate.json")])),
        ("d1", gate("d1", "D1-windows", "24", "tests/unit/test_rc2_approval_subject.py",
                    "tests/contract/test_rc2_route_surface_contract.py")),
        ("d2", gate("d2", "D2-windows", "50", *D2_SELECTION)),
        ("d4", gate("d4", "D4-windows", "17", "tests/unit/test_rc2_worker_model_executor.py",
                    "tests/unit/test_p6_10_loop_engine.py")),
        ("win_native_1", pytest("win_native_1", "tests/platform", "tests/consolidation", "tests/enterprise",
                                "tests/compute_node", "tests/productization", "tests/release_supply_chain")),
        ("win_native_2", pytest("win_native_2", "tests/lifecycle", "tests/learning", "tests/security",
                                "tests/capability_loop", *(arg for test in WIN_NATIVE_2_DESELECT
                                                           for arg in ("--deselect", test)))),
        ("win_local_intel", pytest("win_local_intel", "tests/local_intelligence", "tests/resources",
                                   "tests/capability/test_local_capability_routing.py")),
        ("public_export", lambda: run.step("public_export", [py, "scripts/release/public_export_check.py",
                                                             str(out / "public-export.json")])),
        ("secret_scan", lambda: run.step("secret_scan", [py, "scripts/release/secret_scan.py",
                                                         str(out / "secret-scan.json")])),
        ("operability_gate", lambda: run.step("operability_gate", [py, "scripts/release/operability_gate.py",
                                                                   str(out / "operability-gate.json")])),
        ("doc_lint", lambda: run.step("doc_lint", [py, "scripts/release/doc_lint.py", str(out / "doc-lint.json")])),
        ("npm_cli", lambda: run.step("npm_cli", ["node", "--test", *sorted(
            glob.glob(str(run.worktree / "packages" / "cli" / "test" / "*.test.js")))])),
        ("wheel_build", lambda: run.step("wheel_build", [py, "-m", "pip", "wheel", ".", "--no-deps", "-w",
                                                         str(out / "dist")])),
    ]

    def web_step(name: str) -> Callable[[], int]:
        return lambda: run.step(name, web_commands[name].split(), cwd=web, shell=True)

    steps += [(name, web_step(name)) for name in WEB_STEPS]
    return steps


def validate(run: Run) -> None:
    for _, execute in ci_steps(run):
        execute()
    pg_up(run)
    run.step("full_pytest", [run.python, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "tests",
                             "--ignore=tests/integration", "--junitxml", str(run.out / "full-pytest.xml")],
             pg_database="pados_p6_7_validation", timeout=10800)
    run.step("postgres_gate", [run.python, "scripts/release/postgres_integration_gate.py",
                               str(run.out / "postgres-gate"), "--recreate"], pg_database="pados_p6_7_validation")
    pg_down(run)
    run.step("e2e", [run.python, "scripts/dogfood/local_dogfood_e2e.py", "--pg-bin", str(run.pg_bin), "--pg-port",
                     str(run.port + 1), "--api-port", str(18000 + run.port % 1000), "--out", str(run.out / "e2e")],
             timeout=5400)
    if run.profile == "full":
        web = run.worktree / "web"
        run.step("pester", ["powershell.exe", "-NoProfile", "-Command",
                            f"$r = Invoke-Pester -Path tests/launcher -PassThru -OutputFile '{run.out / 'pester.xml'}' "
                            "-OutputFormat NUnitXml; exit $r.FailedCount"])
        run.step("playwright", "npx playwright test --reporter=list".split(), cwd=web, shell=True)
        run.step("playwright_live_unavailable",
                 "npx playwright test e2e/live-control-center.spec.ts --reporter=list".split(), cwd=web, shell=True,
                 extra_env={"PADOS_E2E_LIVE": "unavailable"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("worktree", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("pg_port", type=int)
    parser.add_argument("--profile", choices=("full", "core"), default="full")
    parser.add_argument("--pg-bin", type=Path, default=DEFAULT_PG_BIN)
    args = parser.parse_args(argv)
    run = Run(worktree=args.worktree.resolve(), out=args.out.resolve(), port=args.pg_port, pg_bin=args.pg_bin,
              profile=args.profile)
    run.out.mkdir(parents=True, exist_ok=False)  # never reuse an evidence directory
    head = _git(run.worktree, "rev-parse", "HEAD")
    dirty = _git(run.worktree, "status", "--porcelain", "--untracked-files=no")
    run.summary = {"schema": "pados.windows-validation.v1", "repo_head": head, "tracked_changes": bool(dirty),
                   "profile": run.profile, "platform": platform.platform(), "python": platform.python_version(),
                   "started": datetime.now(UTC).isoformat(), "pg": {"port": run.port, "bin": str(run.pg_bin)},
                   "steps": {}}
    if dirty:
        run.summary["refused"] = "TRACKED_CHANGES_PRESENT"
        run.save()
        print("refusing: tracked changes present", file=sys.stderr)
        return 2
    try:
        validate(run)
    finally:
        if run.pgdata.exists() and run.pg_ready():
            pg_down(run)
        run.summary["finished"] = datetime.now(UTC).isoformat()
        run.summary["head_after"] = _git(run.worktree, "rev-parse", "HEAD")
        run.save()
        print(json.dumps({name: step["exit_code"] for name, step in run.summary["steps"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
