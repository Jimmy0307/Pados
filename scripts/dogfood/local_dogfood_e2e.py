# SPDX-License-Identifier: AGPL-3.0-only
"""Live local supervised dogfood E2E: one machine, synthetic data, loopback only, no provider credentials.

Runs the documented operator path end to end as real processes and real HTTP, and records every step:

  1. ``pados init`` (unique control token; the shipped default must be refused)
  2. a PostgreSQL with pgvector: a private loopback cluster from the native binaries (``--pg-bin``, trust auth, no
     no password exists) or an existing server (``--pg-host/--pg-port``; password from ``PADOS_DB_PASSWORD``)
  3. the documented lane migrations (docs/product/QUICKSTART.md) and ``pados doctor --json`` (must be ``ok``)
  4. ``pados serve`` on loopback; authentication checks (no token / default token / init token)
  5. ``pados project create`` (initializes the local proof ScopeVersion), a work request carrying a hashed
     TaskEnvelope, a WORK_ROUTE approval through the existing control approval path, a task goal, a one-step
     deterministic plan (RUN_STATIC_ANALYSIS / markdown_reference_check, declared output contract
     ``pados.capability-outcome.v1``) and the existing TaskLoop driven through ``/control/loops`` to COMPLETED;
     execution receipts and artifact digests are read back
  6. the completed loop is submitted through ``POST /control/loops/{id}/result-candidate``: verification passes and
     the candidate waits for approval; nothing is committed
  7. fail-closed negatives: a project with a broken reference ends WAITING_HUMAN and its candidate is refused; a step
     outside the approval never runs; a finished loop cannot be paused or cancelled
  8. restart with ``PADOS_REMOTE_CONFIG``: state, receipts and the pending candidate read back unchanged; a WebAuthn
     device is enrolled through ``/remote/v1/presence/*`` (descriptor only; the operator adds it to the actor row)
  9. governed acceptance over real HTTP: a standing session cannot decide; a fresh step-up proof approves; the
     existing single CAS commits ScopeVersion 0 -> 1; the consumed proof cannot be replayed; after another restart
     the lineage graph shows the accepted result
 10. the existing ResultCandidate -> step-up -> single CAS suite
     (tests/integration/test_final_convergence_product_loop_postgres.py) on dedicated lanes of the same server,
     covering rejection, stale scope version and candidate substitution

The WebAuthn device in steps 8-9 is the software authenticator in tests/remote/presence_support.py (a real P-256
key and genuine ES256 assertions checked by the production verifier); it stands in for a phone or browser.
``--cluster-only`` starts the private cluster, prints the environment for integration tests and waits.
Evidence: ``<out>/E2E_RESULT.json`` plus per-step logs. Exit 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import EllipsisType
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TOKEN_PLAINTEXT = "pados-local-control-" + "token"  # the publicly known value behind the shipped default hash
LANES = {"operational": "pados_dogfood_operational", "proof": "pados_dogfood_proof", "corpus": "pados_dogfood_corpus"}
ACCEPT_LANES = {"operational": "pados_dogfood_accept_operational", "proof": "pados_dogfood_accept_proof"}
# docs/product/QUICKSTART.md, verbatim explicit revisions (never the plural heads target on the proof lane)
MIGRATIONS = [("operational", "alembic.ini", "20261101_0020"), ("proof", "alembic.ini", "20260730_0011"),
              ("proof", "alembic.ini", "20260808_0013"), ("proof", "alembic_p6_19b.ini", "head"),
              ("corpus", "alembic.ini", "20260730_0011")]
SUBJECT_SCHEMA = "pados.approval-subject.v1"
PROBE = "markdown_reference_check"  # emits pados.capability-outcome.v1 derived from what it checked
OPS = [{"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": PROBE, "max_risk_level": "LOW"}]
PRESENCE_ORIGIN = "http://localhost:5173"  # the software authenticator's origin; rp_id localhost
TERMINAL = {"COMPLETED", "PAUSED", "BLOCKED", "WAITING_HUMAN", "FAILED_TERMINAL", "FAILED_RECOVERABLE", "CANCELLED"}


def dogfood_goal_id(tag: str) -> str:
    return f"goal:dogfood-{tag}"


def dogfood_plan_step(tag: str) -> dict[str, Any]:
    return {
        "sequence": 1,
        "product_goal_id": dogfood_goal_id(tag),
        "title": "Check that every Markdown reference resolves",
        "step_type": "RUN_VALIDATION",
        "required_capability": "RUN_STATIC_ANALYSIS",
        "command_id": PROBE,
        "output_contract": {"pados_capability_outcome": "pados.capability-outcome.v1"},
    }


def task_envelope_body(project_id: str, tag: str) -> str:
    """Build the synthetic task's canonical protocol body for the ResultCandidate path."""
    from pados.enterprise.classification import DataClassification
    from pados.work_protocol.models import Goal, TaskEnvelope, TaskPriority

    intent = "Synthetic dogfood task: check that every Markdown reference in the project resolves."
    goal = Goal(
        protocol_version="1.0",
        schema_version=Goal.SCHEMA,
        goal_id=dogfood_goal_id(tag),
        title="Every Markdown reference resolves",
        objective=intent,
        required_capabilities=("RUN_STATIC_ANALYSIS",),
    )
    envelope = TaskEnvelope(
        protocol_version="1.0",
        schema_version=TaskEnvelope.SCHEMA,
        task_id=f"task:dogfood-{tag}",
        original_intent=intent,
        normalized_goals=(goal,),
        project_ref=project_id,
        classification=DataClassification.INTERNAL,
        priority=TaskPriority.NORMAL,
        deadline=None,
        scope_version_ref="scope:0",
        created_by="human:local-dogfood",
        created_at=datetime.now(UTC),
    )
    return json.dumps(
        {"task_envelope": envelope.model_dump(mode="json"), "task_envelope_hash": envelope.content_hash},
        sort_keys=True,
    )


class Run:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.steps: list[dict[str, Any]] = []
        out.mkdir(parents=True, exist_ok=True)

    def record(self, name: str, passed: bool, **detail: Any) -> bool:
        # detail is nested so no detail key can overwrite the step verdict
        self.steps.append({"step": name, "status": "PASS" if passed else "FAIL", "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}", flush=True)
        return passed

    def command(self, name: str, argv: list[str], env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        log = self.out / f"{name}.log"
        result = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
        log.write_text(f"$ {' '.join(argv)}\nexit={result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n"
                       f"{result.stderr}", encoding="utf-8")
        if check and result.returncode != 0:
            raise RuntimeError(f"{name} failed (exit {result.returncode}); see {log}")
        return result


class Cluster:
    """A private loopback PostgreSQL from native binaries (trust auth: no password exists anywhere)."""

    def __init__(self, bin_dir: Path, root: Path, port: int) -> None:
        self.bin, self.data, self.port = bin_dir, root / "pgdata", port
        self.log = root / "postgres.log"

    def _exe(self, name: str) -> str:
        return str(self.bin / (name + (".exe" if os.name == "nt" else "")))

    def start(self) -> None:
        subprocess.run([self._exe("initdb"), "-D", str(self.data), "-U", "pados", "-A", "trust", "-E", "UTF8",
                        "--no-locale"], check=True, capture_output=True, text=True)
        # No captured pipes: the postmaster pg_ctl spawns would inherit them and the call would never see EOF.
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-l", str(self.log), "-w", "-o",
                        f"-p {self.port} -c listen_addresses=127.0.0.1", "start"], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

    def stop(self) -> None:
        subprocess.run([self._exe("pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "stop"], capture_output=True,
                       text=True)


def _connect(host: str, port: int, user: str, db_pass: str, dbname: str) -> Any:
    import psycopg
    if db_pass:  # libpq reads PGPASSWORD; it stays in this process only
        os.environ["PGPASSWORD"] = db_pass
    return psycopg.connect(host=host, port=port, user=user, dbname=dbname, autocommit=True)


def create_databases(host: str, port: int, user: str, db_pass: str, names: list[str]) -> None:
    with _connect(host, port, user, db_pass, "postgres") as admin:
        for name in names:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{name}"')
    for name in names:
        with _connect(host, port, user, db_pass, name) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")


def base_env(args: argparse.Namespace, root: Path, db_pass: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_")}
    env.update({
        "PYTHONUTF8": "1", "PADOS_ENV": "local", "PADOS_RUNTIME_MODE": "native", "PADOS_API_HOST": "127.0.0.1",
        "PADOS_API_PORT": str(args.api_port), "PADOS_DB_HOST": args.pg_host, "PADOS_DB_PORT": str(args.pg_port),
        "PADOS_DB_USER": args.pg_user, "PADOS_DB_PASSWORD": db_pass or "trust-auth-no-password",
        "PADOS_DB_NAME": LANES["operational"], "PADOS_OPERATIONAL_DATABASE_NAME": LANES["operational"],
        "PADOS_PRODUCT_DATABASE_NAME": LANES["proof"], "PADOS_CORPUS_DATABASE_NAME": LANES["corpus"],
        # the directories `pados init --dir root` creates
        "PADOS_PRODUCT_WORKSPACE_ROOT": str(root / "runtime" / "local" / "workspace"),
        "PADOS_PRODUCT_OBJECT_ROOT": str(root / "runtime" / "local" / "objects"),
        "PADOS_ARTIFACT_ROOT": str(root / "runtime" / "local" / "artifacts"),
    })
    return env


class Api:
    def __init__(self, port: int, control_value: str) -> None:
        self.base, self.control_value = f"http://127.0.0.1:{port}", control_value

    def call(self, method: str, path: str, body: Any = None, *, control: str | None | EllipsisType = ...,
             headers: dict[str, str] | None = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json", **(headers or {})}
        presented = self.control_value if isinstance(control, EllipsisType) else control
        if presented is not None:
            headers["X-PADOS-Control-Token"] = presented
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - loopback only
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except ValueError:
                return exc.code, raw.decode("utf-8", "replace")[:500]


def start_server(run: Run, env: dict[str, str], port: int, name: str, cwd: Path = REPO) -> subprocess.Popen[str]:
    log = open(run.out / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the server
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    server = subprocess.Popen([sys.executable, "-m", "pados.cli.main", "serve"], cwd=cwd, env=env, stdout=log,
                              stderr=subprocess.STDOUT, text=True, creationflags=flags)
    for _ in range(120):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):  # noqa: S310
                return server
        except (urllib.error.URLError, OSError):
            if server.poll() is not None:
                raise RuntimeError(f"pados serve exited early ({server.returncode}); see {name}.log")
            time.sleep(1)
    raise RuntimeError("pados serve did not become healthy")


def stop_server(server: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        server.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        server.terminate()
    try:
        server.wait(timeout=30)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=30)


def route_refusal(api: Api, project: str, tag: str) -> dict[str, Any]:
    """A plan step the approval does not cover: routing is refused and the loop must end BLOCKED, not stay RUNNING."""
    _, work = api.call("POST", "/control/work-requests", {"project_id": project, "title": f"Uncovered {tag}",
                                                         "body": "Synthetic: a step the approval does not cover.",
                                                         "idempotency_key": f"{tag}-wr"})
    request = work["work_request"]["id"]
    uncovered = [{**OPS[0], "step_sequence": 2, "command_id": "python_version"}]  # the approval names step 2; the plan only has step 1
    subject = {"kind": "WORK_ROUTE", "schema_version": SUBJECT_SCHEMA, "project_id": project, "request_id": request,
               "actor": {"surfaces": ["TASK_LOOP"], "worker_types": ["DETERMINISTIC_TOOL"]}, "operations": uncovered,
               "scope_version_ref": None, "task_envelope_hash": None}
    _, created = api.call("POST", "/control/approvals", {"project_id": project, "idempotency_key": f"{tag}-ap",
                                                         "proposal_summary": "Uncovered step", "subject": subject})
    approval = created["approval"]
    api.call("POST", f"/control/approvals/{approval['id']}/approve",
             {"version": approval["version"], "idempotency_key": f"{tag}-ok"})
    _, goal = api.call("POST", "/control/task-goals", {
        "project_id": project, "request_id": request, "approval_id": approval["id"], "idempotency_key": f"{tag}-goal",
        "objective": "A step outside the approval", "success_criteria": ["routing is refused"]})
    goal_id = (goal.get("task_goal") or goal).get("id")
    plan_http, plan = api.call("POST", "/control/task-plans", {
        "task_goal_id": goal_id, "approval_id": approval["id"], "idempotency_key": f"{tag}-plan",
        "steps": [{"sequence": 1, "title": "Python version", "step_type": "RUN_VALIDATION",
                   "required_capability": "RUN_STATIC_ANALYSIS", "command_id": "python_version"}]})
    if plan_http >= 400:  # the plan gate refused a step the approval does not cover
        _, jobs = api.call("GET", "/control/jobs")
        ran = [j for j in (jobs or {}).get("jobs", []) if str(j.get("request_id")) == str(request)]
        reason = json.dumps(plan, default=str)[:400]
        return {"refused_at": "PLAN_CREATE", "plan_http": plan_http, "reason": reason, "jobs_for_request": len(ran),
                "status": "REFUSED" if plan_http in (403, 409) else f"UNEXPECTED_{plan_http}"}
    plan_id = (plan.get("task_plan") or plan).get("id")
    create_http, loop = api.call("POST", "/control/loops", {"task_goal_id": goal_id, "active_plan_id": plan_id,
                                                           "approval_id": approval["id"], "max_iterations": 3,
                                                           "idempotency_key": f"{tag}-loop"})
    _, jobs = api.call("GET", "/control/jobs")
    if not (isinstance(loop, dict) and "loop" in loop):  # refused before any loop exists: the earliest gate
        ran = [j for j in (jobs or {}).get("jobs", []) if str(j.get("request_id")) == str(request)]
        return {"refused_at": "LOOP_CREATE", "create_http": create_http, "detail": loop, "jobs_for_request": len(ran),
                "status": "REFUSED"}
    loop_id = loop["loop"]["id"]
    api.call("POST", f"/control/loops/{loop_id}/start", {})
    steps = [api.call("POST", f"/control/loops/{loop_id}/step", {"idempotency_key": f"{tag}-s{i}"})[0]
             for i in range(2)]
    _, after = api.call("GET", f"/control/loops/{loop_id}")
    _, jobs = api.call("GET", "/control/jobs")
    ran = [j for j in (jobs or {}).get("jobs", []) if str(j.get("request_id")) == str(request)]
    return {"refused_at": "ROUTE", "loop_id": loop_id, "step_http": steps, "jobs_for_request": len(ran),
            "status": (after.get("loop") or after).get("status") if isinstance(after, dict) else None}


def task_flow(api: Api, project: str, tag: str) -> dict[str, Any]:
    _, work = api.call("POST", "/control/work-requests", {"project_id": project, "title": f"Dogfood {tag}",
                                                         "body": task_envelope_body(project, tag),
                                                         "idempotency_key": f"{tag}-wr"})
    request = work["work_request"]["id"]
    subject = {"kind": "WORK_ROUTE", "schema_version": SUBJECT_SCHEMA, "project_id": project, "request_id": request,
               "actor": {"surfaces": ["TASK_LOOP"], "worker_types": ["DETERMINISTIC_TOOL"]}, "operations": OPS,
               "scope_version_ref": None, "task_envelope_hash": None}
    _, created = api.call("POST", "/control/approvals", {"project_id": project, "idempotency_key": f"{tag}-ap",
                                                         "proposal_summary": f"Dogfood: {PROBE}", "subject": subject})
    approval = created["approval"]
    pending_status, _ = api.call("POST", "/control/loops", {"task_goal_id": "00000000-0000-0000-0000-000000000000",
                                                          "approval_id": approval["id"], "idempotency_key": f"{tag}-early"})
    approved_status, approved = api.call("POST", f"/control/approvals/{approval['id']}/approve",
                                         {"version": approval["version"], "idempotency_key": f"{tag}-ok"})
    goal_status, goal = api.call("POST", "/control/task-goals", {
        "project_id": project, "request_id": request, "approval_id": approval["id"], "idempotency_key": f"{tag}-goal",
        "objective": "Check that every Markdown reference in the project resolves",
        "success_criteria": ["the probe reports a declared COMPLETE outcome",
                             "an execution receipt with artifact digests exists"]})
    goal_id = (goal.get("task_goal") or goal).get("id") if isinstance(goal, dict) else None
    plan_status, plan = api.call("POST", "/control/task-plans", {
        "task_goal_id": goal_id, "approval_id": approval["id"], "idempotency_key": f"{tag}-plan",
        "steps": [dogfood_plan_step(tag)]})
    plan_id = (plan.get("task_plan") or plan).get("id") if isinstance(plan, dict) else None
    _, plan_view = api.call("GET", f"/control/task-plans/{plan_id}")
    loop_status, loop = api.call("POST", "/control/loops", {"task_goal_id": goal_id, "active_plan_id": plan_id,
                                                           "approval_id": approval["id"], "max_iterations": 4,
                                                           "idempotency_key": f"{tag}-loop"})
    loop_id = loop["loop"]["id"]
    api.call("POST", f"/control/loops/{loop_id}/start", {})
    decisions: list[Any] = []
    final = None
    for index in range(6):
        _, step = api.call("POST", f"/control/loops/{loop_id}/step", {"idempotency_key": f"{tag}-step{index}"})
        decisions.append({k: step.get(k) for k in ("status", "decision", "decision_reason", "job_status")}
                         if isinstance(step, dict)
                         else step)
        final = (step.get("loop") or {}).get("status") if isinstance(step, dict) else None
        if final in TERMINAL:
            break
    _, events = api.call("GET", f"/control/loops/{loop_id}/events")
    return {"request_id": request, "approval_id": approval["id"], "approval_refused_before_approve": pending_status,
            "approve_http": approved_status, "approval_status": (approved.get("approval") or {}).get("status")
            if isinstance(approved, dict) else None, "goal_http": goal_status, "plan_http": plan_status,
            "plan": plan_view, "loop_http": loop_status, "loop_id": loop_id, "final_status": final,
            "decisions": decisions, "events": events}


def approval_lifecycle(api: Api, project: str, flow: dict[str, Any], tag: str) -> dict[str, Any]:
    """SEC-4 over HTTP: a revoked approval authorizes nothing; a consumed approval cannot back a second loop."""
    _, work = api.call("POST", "/control/work-requests", {"project_id": project, "title": f"Revoked {tag}",
                                                         "body": "Synthetic: an approval withdrawn before use.",
                                                         "idempotency_key": f"{tag}-wr"})
    request = work["work_request"]["id"]
    subject = {"kind": "WORK_ROUTE", "schema_version": SUBJECT_SCHEMA, "project_id": project, "request_id": request,
               "actor": {"surfaces": ["TASK_LOOP"], "worker_types": ["DETERMINISTIC_TOOL"]}, "operations": OPS,
               "scope_version_ref": None, "task_envelope_hash": None}
    _, created = api.call("POST", "/control/approvals", {"project_id": project, "idempotency_key": f"{tag}-ap",
                                                         "proposal_summary": "Revoked before use", "subject": subject})
    approval = created["approval"]
    _, approved = api.call("POST", f"/control/approvals/{approval['id']}/approve",
                           {"version": approval["version"], "idempotency_key": f"{tag}-ok"})
    approved_row = (approved or {}).get("approval", {}) if isinstance(approved, dict) else {}
    revoke_http, revoked = api.call("POST", f"/control/approvals/{approval['id']}/revoke", {
        "version": approved_row.get("version"), "reason": "dogfood: withdrawn", "idempotency_key": f"{tag}-rv"})
    goal_http, goal = api.call("POST", "/control/task-goals", {
        "project_id": project, "request_id": request, "approval_id": approval["id"], "idempotency_key": f"{tag}-g",
        "objective": "Must be refused", "success_criteria": ["refused"]})
    # The completed loop consumed its approval: a fresh goal and plan may still be drafted, but no second loop.
    _, goal2 = api.call("POST", "/control/task-goals", {
        "project_id": project, "request_id": flow["request_id"], "approval_id": flow["approval_id"],
        "idempotency_key": f"{tag}-g2", "objective": "Second loop on a consumed approval",
        "success_criteria": ["refused"]})
    goal2_id = (goal2.get("task_goal") or goal2).get("id") if isinstance(goal2, dict) else None
    plan2_http, plan2 = api.call("POST", "/control/task-plans", {"task_goal_id": goal2_id, "approval_id": flow["approval_id"],
                                                        "idempotency_key": f"{tag}-p2", "steps": [{
        "sequence": 1, "title": "Check references again", "step_type": "RUN_VALIDATION",
        "required_capability": "RUN_STATIC_ANALYSIS", "command_id": PROBE}]})
    plan2_id = (plan2.get("task_plan") or plan2).get("id") if isinstance(plan2, dict) else None
    loop2_http, loop2 = api.call("POST", "/control/loops", {"task_goal_id": goal2_id, "active_plan_id": plan2_id,
                                                           "approval_id": flow["approval_id"], "max_iterations": 2,
                                                           "idempotency_key": f"{tag}-l2"})
    return {"revoke_http": revoke_http, "revoked_status": (revoked or {}).get("approval", {}).get("status")
            if isinstance(revoked, dict) else None, "goal_http": goal_http,
            "goal_detail": goal.get("detail") if isinstance(goal, dict) else goal,
            "second_plan_http": plan2_http, "second_plan_detail": plan2.get("detail") if isinstance(plan2, dict)
            and plan2_http >= 400 else None, "second_loop_http": loop2_http, "second_loop_detail": loop2.get("detail") if isinstance(loop2, dict)
            else loop2, "approval_expires_at": approved_row.get("expires_at")}


def create_project(run: Run, api: Api, env: dict[str, str], root: Path, name: str, *, broken: bool) -> str:
    """A synthetic, public-safe workspace; the broken one has a reference to a file that does not exist."""
    workspace = root / "workspace" / name
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("Synthetic, public-safe dogfood project. See the [guide](docs/guide.md).\n",
                                         encoding="utf-8")
    target = "missing.md" if broken else "../README.md"
    (workspace / "docs" / "guide.md").write_text(f"Back to the [overview]({target}).\n", encoding="utf-8")
    run.command(f"project_create_{name}", [sys.executable, "-m", "pados.cli.main", "project", "create",
                                           "--name", name, "--root-path", str(workspace)], env)
    _, projects = api.call("GET", "/control/projects")
    listing = projects.get("projects", projects) if isinstance(projects, dict) else projects
    return str(next(p["id"] for p in listing if p.get("name") == name))


class RemoteOperator:
    """The operator's Remote Operations configuration: one approving actor on loopback, WebAuthn rp_id localhost."""

    def __init__(self, root: Path, project: str) -> None:
        sys.path.insert(0, str(REPO))
        from tests.remote.presence_support import SoftwareAuthenticator

        self.root, self.project = root / "remote", project
        self.access = secrets.token_hex(32)  # lives only in this process; the config stores its SHA-256
        self.device = SoftwareAuthenticator(origin=PRESENCE_ORIGIN)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_config(self, descriptor: dict[str, Any] | None = None) -> Path:
        credentials = [] if descriptor is None else [{k: descriptor[k] for k in (
            "credential_id", "algorithm", "public_key_cose")} | {"label": "dogfood-device"}]
        path = self.root / "remote.json"
        path.write_text(json.dumps({
            "bind_host": "127.0.0.1", "staging_root": str(self.root / "staging"),
            "ledger_path": str(self.root / "ledger" / "intents.jsonl"),
            "actors": [{"actor_id": "owner", "credential_hash": hashlib.sha256(self.access.encode()).hexdigest(),
                        "projects": [self.project], "may_approve": True, "presence_credentials": credentials}],
            "presence": {"rp_id": "localhost", "origins": [PRESENCE_ORIGIN]}}, indent=2), encoding="utf-8")
        return path


class RemoteClient:
    """Real HTTP against /remote/v1 on the loopback server, exactly as the PWA would call it."""

    def __init__(self, port: int, operator: RemoteOperator) -> None:
        self.api, self.operator = Api(port, ""), operator
        self.headers = {"X-PADOS-Remote-Actor": "owner", "X-PADOS-Remote-Proof": operator.access}

    def call(self, method: str, path: str, body: Any = None, extra: dict[str, str] | None = None) -> tuple[int, Any]:
        return self.api.call(method, path, body, control=None, headers={**self.headers, **(extra or {})})

    def waiting(self, candidate_id: str | None) -> str | None:
        _, attention = self.call("GET", "/remote/v1/attention")
        rows = (attention or {}).get("waiting_approval", []) if isinstance(attention, dict) else []
        return next((r["approval_ref"] for r in rows if r.get("run_id") == candidate_id), None)

    def enroll(self) -> dict[str, Any]:
        _, options = self.call("POST", "/remote/v1/presence/enrollment-options", {})
        public_key = (options or {}).get("public_key", {}) if isinstance(options, dict) else {}
        status, body = self.call("POST", "/remote/v1/presence/enrollment-descriptor", {
            "registration": self.operator.device.registration(public_key), "challenge": public_key.get("challenge")})
        return {"http": status, "descriptor": body.get("descriptor") if isinstance(body, dict) else None}

    def graph_status(self, candidate_id: str | None) -> dict[str, Any]:
        _, graph = self.call("GET", f"/remote/v1/jobs/{candidate_id}/graph")
        nodes = {n["node_id"]: n for n in (graph or {}).get("nodes", [])} if isinstance(graph, dict) else {}
        semantic = [n for n in nodes.values() if str(n["node_id"]).startswith(f"semantic:{self.operator.project}:")]
        return {"authority": (graph or {}).get("authority") if isinstance(graph, dict) else None,
                "decision": (nodes.get(f"decision:{candidate_id}") or {}).get("status"),
                "approval": (nodes.get(f"approval:{candidate_id}") or {}).get("status"),
                "semantic": semantic[-1]["status"] if semantic else None,
                "semantic_node": semantic[-1]["node_id"] if semantic else None}

    def _decide(self, session: dict[str, str], ref: str, candidate_id: str, proof: str | None) -> tuple[int, Any]:
        body: dict[str, Any] = {"intent_id": f"dogfood-approve-{secrets.token_hex(6)}", "project_id":
                                self.operator.project, "target_run_id": candidate_id}
        if proof is not None:
            body["step_up_proof"] = proof
        return self.call("POST", f"/remote/v1/approvals/{ref}/approve", body, session)

    def accept(self, candidate_id: str | None) -> dict[str, Any]:
        ref = self.waiting(candidate_id)
        graph_before = self.graph_status(candidate_id)
        _, plain = self.call("POST", "/remote/v1/sessions", {})
        session = {"X-PADOS-Remote-Session": (plain or {}).get("session_handle", "")}
        without_proof, _ = self._decide(session, str(ref), str(candidate_id), None)
        _, challenge = self.call("POST", f"/remote/v1/approvals/{ref}/step-up", {"action": "APPROVE"}, session)
        challenge = challenge if isinstance(challenge, dict) else {}
        _, verified = self.call("POST", f"/remote/v1/approvals/{ref}/step-up/{challenge.get('challenge_id')}/verify",
                                {"assertion": self.operator.device.assertion(challenge.get("public_key", {}))},
                                session)
        proof = (verified or {}).get("proof_ref") if isinstance(verified, dict) else None
        decision_http, decision = self._decide(session, str(ref), str(candidate_id), proof)
        replay_http, _ = self._decide(session, str(ref), str(candidate_id), proof)
        decision = decision if isinstance(decision, dict) else {"raw": decision}
        return {"approval_ref": ref, "without_proof_http": without_proof, "proof_issued": bool(proof),
                "decision_http": decision_http, "outcome": decision.get("outcome"),
                "presence_method": decision.get("presence_method"), "decision": decision,
                "replay_http": replay_http, "graph_before": graph_before,
                "graph_after": self.graph_status(candidate_id)}


def evidence_of(api: Api, loop_id: str) -> list[dict[str, Any]]:
    """Every job the loop ran, with its status and the artifacts (type, SHA-256) the worker recorded."""
    _, iterations = api.call("GET", f"/control/loops/{loop_id}/iterations")
    rows = iterations.get("iterations", iterations) if isinstance(iterations, dict) else iterations
    evidence: list[dict[str, Any]] = []
    for job_id in [row.get("job_id") for row in rows or [] if isinstance(row, dict) and row.get("job_id")]:
        _, job = api.call("GET", f"/control/jobs/{job_id}")
        _, artifacts = api.call("GET", f"/control/jobs/{job_id}/artifacts")
        record = (job or {}).get("job", {}) if isinstance(job, dict) else {}
        evidence.append({"job_id": job_id, "capability": record.get("capability_id"), "status": record.get("status"),
                         "artifacts": sorted((a.get("artifact_type"), a.get("sha256"))
                                             for a in (artifacts or {}).get("artifacts", []))})
    return evidence


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0915 - a linear, recorded script
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, help="native PostgreSQL bin dir: start a private loopback cluster")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55440)
    parser.add_argument("--pg-user", default="pados")
    parser.add_argument("--api-port", type=int, default=18088)
    parser.add_argument("--cluster-only", action="store_true")
    parser.add_argument("--skip-acceptance-suite", action="store_true")
    args = parser.parse_args(argv)
    run = Run(args.out.resolve())
    root = Path(tempfile.mkdtemp(prefix="pados-dogfood-"))
    db_pass = "" if args.pg_bin else os.environ.get("PADOS_DB_PASSWORD", "")
    cluster = Cluster(args.pg_bin, root, args.pg_port) if args.pg_bin else None
    server: subprocess.Popen[str] | None = None
    started = time.time()
    try:
        if cluster:
            cluster.start()
        run.record("postgres_ready", True, mode="private-cluster" if cluster else "existing-server",
                   port=args.pg_port)
        if args.cluster_only:
            print(json.dumps({"PADOS_DB_HOST": args.pg_host, "PADOS_DB_PORT": args.pg_port,
                              "PADOS_DB_USER": args.pg_user}), flush=True)
            while True:
                time.sleep(3600)

        env = base_env(args, root, db_pass)
        init = run.command("pados_init", [sys.executable, "-m", "pados.cli.main", "init", "--dir", str(root)], env)
        control_value = (root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
        token_hash = next(line.split("=", 1)[1] for line in (root / ".env").read_text(encoding="utf-8").splitlines()
                          if line.startswith("PADOS_CONTROL_API_TOKEN_HASH="))
        env["PADOS_CONTROL_API_TOKEN_HASH"] = token_hash
        run.record("pados_init_unique_token", hashlib.sha256(control_value.encode()).hexdigest().upper() == token_hash
                   and control_value != DEFAULT_TOKEN_PLAINTEXT, init_exit=init.returncode)

        create_databases(args.pg_host, args.pg_port, args.pg_user, db_pass, list(LANES.values()))
        for lane, ini, revision in MIGRATIONS:
            lane_env = {**env, "PADOS_DB_NAME": LANES[lane]}
            run.command(f"migrate_{lane}_{revision}", [sys.executable, "-m", "alembic", "-c", ini, "upgrade",
                                                       revision], lane_env)
        run.record("migrations", True, lanes=LANES, revisions=[m[2] for m in MIGRATIONS])

        doctor = run.command("pados_doctor", [sys.executable, "-m", "pados.cli.main", "doctor", "--json"], env,
                             check=False)
        try:  # the JSON report comes first; a structured log line may follow it
            report, _ = json.JSONDecoder().raw_decode(doctor.stdout.lstrip())
        except ValueError:
            report = {}
        run.record("pados_doctor", doctor.returncode == 0 and report.get("status") == "ok",
                   exit=doctor.returncode, status=report.get("status"),
                   readiness=(report.get("readiness") or {}).get("state"))

        default_env = {**env, "PADOS_CONTROL_API_TOKEN_HASH": hashlib.sha256(
            DEFAULT_TOKEN_PLAINTEXT.encode()).hexdigest().upper()}
        refused = run.command("serve_refuses_default_token", [sys.executable, "-m", "pados.cli.main", "serve"],
                              default_env, check=False)
        run.record("serve_refuses_default_token", refused.returncode != 0
                   and "DEFAULT_CONTROL_TOKEN_REFUSED" in refused.stdout + refused.stderr)

        server = start_server(run, env, args.api_port, "serve_first")
        api = Api(args.api_port, control_value)
        no_token, _ = api.call("GET", "/control/projects", control=None)
        wrong, _ = api.call("GET", "/control/projects", control=DEFAULT_TOKEN_PLAINTEXT)
        ok, _ = api.call("GET", "/control/projects")
        run.record("control_auth", no_token in (401, 403) and wrong in (401, 403) and ok == 200,
                   no_token=no_token, default_token=wrong, init_token=ok)
        # OP-003 health contract as a user sees it: liveness claims nothing, readiness reports the required lane.
        live_http, live = api.call("GET", "/health/live", control=None)
        ready_http, ready = api.call("GET", "/health/ready", control=None)
        checks = {c.get("name"): c for c in (ready or {}).get("checks", [])} if isinstance(ready, dict) else {}
        run.record("health_live_and_ready", live_http == 200 and (live or {}).get("status") == "ALIVE"
                   and ready_http == 200 and (ready or {}).get("schema_version") == "pados.health.v1"
                   and (ready or {}).get("status") in ("OK", "DEGRADED")
                   and (checks.get("operational_database") or {}).get("status") == "OK",
                   live=live_http, ready=ready_http, status=(ready or {}).get("status") if isinstance(ready, dict)
                   else None, checks={name: c.get("status") for name, c in checks.items()})

        project = create_project(run, api, env, root, "dogfood-synthetic", broken=False)
        broken_project = create_project(run, api, env, root, "dogfood-broken-reference", broken=True)
        run.record("project_create", bool(project and broken_project), project_id=project,
                   broken_project_id=broken_project)

        flow = task_flow(api, project, f"dogfood-{int(started)}")
        (run.out / "task_flow.json").write_text(json.dumps(flow, indent=2, default=str), encoding="utf-8")
        receipts = evidence_of(api, flow["loop_id"])
        run.record("approval_required_before_route", flow["approval_refused_before_approve"] >= 400,
                   http=flow["approval_refused_before_approve"])
        run.record("human_approval", flow["approve_http"] == 200 and flow["approval_status"] == "APPROVED")
        run.record("plan_visible", flow["plan_http"] in (200, 201) and isinstance(flow["plan"], dict),
                   plan_http=flow["plan_http"])
        run.record("execution_completed", flow["final_status"] == "COMPLETED", final=flow["final_status"],
                   decisions=flow["decisions"])
        run.record("execution_receipt", bool(receipts) and all(r["status"] == "SUCCEEDED" and r["artifacts"]
                                                                and all(len(d or "") == 64 for _, d in r["artifacts"])
                                                                for r in receipts), receipts=receipts)

        submitted_http, submitted = api.call("POST", f"/control/loops/{flow['loop_id']}/result-candidate", {})
        candidate = (submitted or {}).get("candidate", {}) if isinstance(submitted, dict) else {}
        run.record("result_candidate_verified_waiting_approval", submitted_http == 200
                   and candidate.get("verification_status") == "PASSED"
                   and candidate.get("decision_status") == "WAITING_APPROVAL", http=submitted_http,
                   candidate=candidate)

        broken = task_flow(api, broken_project, f"dogfood-broken-{int(started)}")
        broken_http, broken_result = api.call("POST", f"/control/loops/{broken['loop_id']}/result-candidate", {})
        run.record("broken_reference_waits_for_human_and_is_refused", broken["final_status"] == "WAITING_HUMAN"
                   and broken_http >= 400 and isinstance(broken_result, dict)
                   and broken_result.get("detail") == "RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP",
                   final=broken["final_status"], decisions=broken["decisions"], http=broken_http,
                   response_detail=broken_result.get("detail") if isinstance(broken_result, dict) else None)

        refusal = route_refusal(api, project, f"dogfood-uncovered-{int(started)}")
        run.record("uncovered_step_never_runs", refusal["status"] in ("BLOCKED", "REFUSED")
                   and refusal["jobs_for_request"] == 0, **refusal)

        pause, _ = api.call("POST", f"/control/loops/{flow['loop_id']}/pause", {})
        cancel, _ = api.call("POST", f"/control/loops/{flow['loop_id']}/cancel", {})
        run.record("finished_loop_cannot_be_paused_or_cancelled", pause >= 400 and cancel >= 400,
                   pause=pause, cancel=cancel)

        lifecycle = approval_lifecycle(api, project, flow, f"dogfood-lifecycle-{int(started)}")
        run.record("revoked_or_consumed_approval_authorizes_nothing", lifecycle["revoke_http"] == 200
                   and lifecycle["revoked_status"] == "CANCELLED" and lifecycle["goal_http"] == 403
                   and lifecycle["goal_detail"] == "APPROVAL_REVOKED" and lifecycle["second_loop_http"] == 403
                   and lifecycle["second_loop_detail"] == "APPROVAL_CONSUMED"
                   and bool(lifecycle["approval_expires_at"]), **lifecycle)

        stop_server(server)
        operator = RemoteOperator(root, project)
        env["PADOS_REMOTE_CONFIG"] = str(operator.write_config())
        server = start_server(run, env, args.api_port, "serve_after_restart")
        _, loop_again = api.call("GET", f"/control/loops/{flow['loop_id']}")
        status_again = (loop_again.get("loop") or loop_again).get("status") if isinstance(loop_again, dict) else None
        receipts_again = evidence_of(api, flow["loop_id"])
        remote = RemoteClient(args.api_port, operator)
        waiting = remote.waiting(candidate.get("candidate_id"))
        run.record("restart_persistence", status_again == "COMPLETED" and receipts_again == receipts
                   and waiting is not None, status_after_restart=status_again,
                   evidence_unchanged=receipts_again == receipts, candidate_still_waiting=waiting is not None)

        enrolled = remote.enroll()
        run.record("presence_device_enrolled", enrolled["http"] == 200 and enrolled["descriptor"] is not None,
                   http=enrolled["http"], attestation_verified=(enrolled["descriptor"] or {}).get(
                       "attestation_verified"), authenticator="software (tests/remote/presence_support.py)")
        stop_server(server)
        operator.write_config(enrolled["descriptor"])  # the operator edits the actor row; the API never does
        # From another working directory: receipts must still resolve when acceptance re-verifies them.
        server = start_server(run, env, args.api_port, "serve_with_enrolled_device", cwd=root)

        accepted = remote.accept(candidate.get("candidate_id"))
        (run.out / "governed_acceptance_live.json").write_text(json.dumps(accepted, indent=2, default=str),
                                                               encoding="utf-8")
        run.record("standing_session_cannot_decide", accepted["without_proof_http"] in (403, 422),
                   http=accepted["without_proof_http"])
        run.record("governed_acceptance_live_cas", accepted["decision_http"] == 200
                   and accepted["outcome"] == "COMMITTED"
                   and accepted["presence_method"] == "WEBAUTHN_USER_VERIFICATION",
                   http=accepted["decision_http"], outcome=accepted["outcome"],
                   presence_method=accepted["presence_method"], graph_before=accepted["graph_before"],
                   graph_after=accepted["graph_after"])
        run.record("consumed_proof_cannot_be_replayed", accepted["replay_http"] in (403, 409, 422),
                   http=accepted["replay_http"])

        stop_server(server)
        server = start_server(run, env, args.api_port, "serve_after_acceptance")
        after = remote.graph_status(candidate.get("candidate_id"))
        _, loop_final = api.call("GET", f"/control/loops/{flow['loop_id']}")
        run.record("acceptance_persists_after_restart", after.get("decision") == "ACCEPTED"
                   and after.get("semantic") == "ACCEPTED"
                   and (loop_final.get("loop") or loop_final).get("status") == "COMPLETED", graph=after)
        stop_server(server)
        server = None

        if not args.skip_acceptance_suite:
            create_databases(args.pg_host, args.pg_port, args.pg_user, db_pass, list(ACCEPT_LANES.values()))
            accept_env = {**env, "PADOS_TEST_OPERATIONAL_DATABASE": ACCEPT_LANES["operational"],
                          "PADOS_DB_NAME": ACCEPT_LANES["proof"], "PADOS_PRODUCT_DATABASE_NAME": ACCEPT_LANES["proof"]}
            for lane, ini, revision in MIGRATIONS:
                if lane in ACCEPT_LANES:
                    run.command(f"accept_migrate_{lane}_{revision}", [sys.executable, "-m", "alembic", "-c", ini,
                                                                       "upgrade", revision],
                                {**accept_env, "PADOS_DB_NAME": ACCEPT_LANES[lane]})
            suite = run.command("governed_acceptance_suite", [
                sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "-rs",
                "tests/integration/test_final_convergence_product_loop_postgres.py"], accept_env, check=False)
            tail = suite.stdout.strip().splitlines()[-1:] if suite.stdout else []
            run.record("governed_acceptance_suite", suite.returncode == 0 and "skipped" not in " ".join(tail),
                       exit=suite.returncode, summary=tail)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded as evidence, then the run fails
        run.record("unexpected_failure", False, error=f"{type(exc).__name__}: {exc}"[:800])
    finally:
        if server is not None:
            stop_server(server)
        if cluster:
            cluster.stop()
        shutil.rmtree(root, ignore_errors=True)
    passed = all(step["status"] == "PASS" for step in run.steps)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    (run.out / "E2E_RESULT.json").write_text(json.dumps({
        "verdict": "PASS" if passed else "FAIL", "commit": head, "platform": platform.platform(),
        "python": sys.version.split()[0], "seconds": int(time.time() - started),
        "finished": datetime.now(UTC).isoformat(), "steps": run.steps},
        indent=2, default=str), encoding="utf-8")
    print(f"E2E {'PASS' if passed else 'FAIL'} -> {run.out / 'E2E_RESULT.json'}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

