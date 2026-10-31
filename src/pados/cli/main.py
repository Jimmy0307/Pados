# SPDX-License-Identifier: AGPL-3.0-only
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast
from uuid import UUID

import typer
from click import get_current_context
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from pados.config import PROOF_REVISION_HEAD, Settings, get_settings, lane_revisions_accepted
from pados.db import check_database, get_engine, session_scope
from pados.logging_config import configure_logging, get_logger
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, TaskGoalCreate, TaskPlanCreate
from pados.memory import packet as context_packet
from pados.memory import service as memory_service
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.worker import registry as worker_registry

app = typer.Typer(no_args_is_help=True)
project_app = typer.Typer(no_args_is_help=True)
memory_app = typer.Typer(no_args_is_help=True)
context_app = typer.Typer(no_args_is_help=True)
worker_app = typer.Typer(no_args_is_help=True)
loop_app = typer.Typer(no_args_is_help=True)
node_app = typer.Typer(no_args_is_help=True, help="Local Compute Node (optional execution substrate).")
storage_app = typer.Typer(no_args_is_help=True, help="Storage accounting and retention (recommendation only).")
machine_app = typer.Typer(no_args_is_help=True, help="Machine connections (SSH key / ssh-agent / OS credential refs).")
app.add_typer(memory_app, name="memory")
app.add_typer(project_app, name="project")
app.add_typer(context_app, name="context")
app.add_typer(worker_app, name="worker")
app.add_typer(node_app, name="node")
app.add_typer(loop_app, name="loop")
app.add_typer(storage_app, name="storage")
app.add_typer(machine_app, name="machine")

EVENT_TYPE_HELP = "Allowed values: " + ", ".join(sorted(memory_service.VALID_EVENT_PAYLOAD_KEYS))


def _debug_enabled() -> bool:
    context = get_current_context(silent=True)
    while context is not None:
        if bool(context.params.get("debug")):
            return True
        context = context.parent
    return False


def _operator_error(category: str, message: str) -> NoReturn:
    typer.echo(f"{category}: {message}", err=True)
    raise typer.Exit(code=2)


_INTEGRITY_FAILURE_CATEGORIES = {
    "23502": "not_null_violation",
    "23503": "foreign_key_violation",
    "23505": "unique_violation",
    "23514": "check_violation",
}


def _safe_identifier(value: object) -> str | None:
    candidate = str(value or "")
    if candidate and candidate.isascii() and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
        return candidate
    return None


def _database_failure_details(exc: DBAPIError) -> dict[str, str]:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    sqlstate = str(
        getattr(original, "sqlstate", None)
        or getattr(diagnostic, "sqlstate", None)
        or "unknown"
    )
    table_name = _safe_identifier(getattr(diagnostic, "table_name", None))
    column_name = _safe_identifier(getattr(diagnostic, "column_name", None))
    constraint_name = _safe_identifier(getattr(diagnostic, "constraint_name", None))
    if constraint_name is None and sqlstate == "23502" and table_name and column_name:
        constraint_name = f"{table_name}_{column_name}_not_null"
    if sqlstate in _INTEGRITY_FAILURE_CATEGORIES:
        failure_category = _INTEGRITY_FAILURE_CATEGORIES[sqlstate]
    elif sqlstate.startswith("23"):
        failure_category = "integrity_constraint_violation"
    else:
        failure_category = "database_error"
    return {
        "sqlstate": sqlstate,
        "constraint_name": constraint_name or "not_applicable",
        "column_name": column_name or "not_applicable",
        "failure_category": failure_category,
    }


def _record_database_failure(exc: DBAPIError) -> None:
    get_logger("pados.cli").error("database_operation_failed", **_database_failure_details(exc))


def operator_error_boundary[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
        except (ValueError, TypeError, ValidationError, FileNotFoundError) as exc:
            if _debug_enabled():
                raise
            _operator_error("Validation error", str(exc))
        except PermissionError as exc:
            if _debug_enabled():
                raise
            _operator_error("Domain error", str(exc))
        except IntegrityError as exc:
            _record_database_failure(exc)
            if _debug_enabled():
                raise
            _operator_error("Database constraint error", "the requested operation was rejected")
        except DBAPIError as exc:
            _record_database_failure(exc)
            if _debug_enabled():
                raise
            _operator_error("Database error", "the database operation could not be completed")
        except Exception:
            if _debug_enabled():
                raise
            _operator_error("Unexpected internal error", "the command failed")
        raise AssertionError("operator error boundary did not terminate")

    return wrapped


def _assert_project_import_path() -> None:
    import pados

    actual = Path(pados.__file__).resolve()
    expected = (Path.cwd() / "src" / "pados" / "__init__.py").resolve()
    if actual != expected:
        raise RuntimeError("WORKTREE_IMPORT_PATH_MISMATCH")


@app.callback()
def main(
    context: typer.Context,
    debug: Annotated[bool, typer.Option("--debug", help="Show tracebacks for debugging.")] = False,
) -> None:
    """Personal Autonomous Development OS command line."""

    context.ensure_object(dict)
    context.obj["debug"] = debug


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
        output = (completed.stdout + completed.stderr).strip()
        return completed.returncode, output
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output = (stdout + stderr).strip()
        return 124, output or "command timed out"


def _check_migrations(settings: Settings) -> dict[str, Any]:
    config_path = Path("alembic.ini")
    if not config_path.exists():
        return {"status": "error", "error": "alembic.ini not found"}

    def lane(lane_name: str, database: str, expected_revision: str) -> dict[str, Any]:
        lane_settings = settings.model_copy(update={"db_name": database})
        try:
            engine = get_engine(lane_settings)
            with engine.connect() as conn:
                rows: Any = conn.execute(text("select version_num from alembic_version")).scalars().all()
                proof_rows: Any = (
                    conn.execute(text(
                        "select version_num from p6_19b_alembic_version"
                        if conn.execute(text("select to_regclass('public.p6_19b_alembic_version')")).scalar()
                        else "select null::text as version_num where false"
                    )).scalars().all()
                )
            revisions = sorted(str(row) for row in rows)
            proof_revisions = sorted(str(row) for row in proof_rows)
        except Exception as exc:  # noqa: BLE001 - doctor reports each lane failure as data
            return {
                "database": database,
                "current_revision": None,
                "expected_revision": expected_revision,
                "status": "error",
                "error": str(exc),
            }
        current_revision = revisions[0] if len(revisions) == 1 else None
        accepted = lane_revisions_accepted(settings, lane_name, revisions)
        if lane_name == "proof":
            accepted = accepted and proof_revisions == [PROOF_REVISION_HEAD]
        elif proof_revisions:
            # P6.15 separation: the proof graph never runs in operational or Corpus lanes.
            accepted = False
        result: dict[str, Any] = {
            "database": database,
            "current_revision": current_revision,
            "expected_revision": expected_revision,
            "status": "ok" if accepted else "error",
        }
        if len(revisions) != 1:
            result["current_revisions"] = revisions
        if lane_name == "proof" or proof_revisions:
            result["proof_revisions"] = proof_revisions
        if not accepted:
            result["error"] = f"{lane_name} lane revisions are not the exact expected set"
        return result

    lanes = {
        "operational": lane("operational", settings.operational_database_name, settings.operational_revision),
        "corpus": lane("corpus", settings.corpus_database_name, settings.corpus_revision),
    }
    if settings.product_database_name:
        lanes["proof"] = lane("proof", settings.product_database_name, PROOF_REVISION_HEAD)
    return {**lanes, "status": "ok" if all(item["status"] == "ok" for item in lanes.values()) else "error"}


def _doctor_database_target_error(settings: Settings) -> str | None:
    protected = {"pados_local_runtime", "pados_corpus_runtime", "pados_proof_runtime"}
    targets = {
        settings.db_name,
        settings.operational_database_name,
        settings.corpus_database_name,
        settings.product_database_name,
    }
    if targets & protected:
        return "PROTECTED_DATABASE_TARGET"
    lanes = [settings.operational_database_name, settings.corpus_database_name]
    if settings.product_database_name:
        lanes.append(settings.product_database_name)
    if len(set(lanes)) != len(lanes):
        return "DATABASE_LANES_MUST_DIFFER"
    if settings.db_name != settings.operational_database_name:
        return "ACTIVE_DATABASE_MUST_MATCH_OPERATIONAL_LANE"
    return None


def _system_health(settings: Settings) -> dict[str, Any]:
    from sqlalchemy.exc import SQLAlchemyError

    from pados.operations.service import system_health

    try:
        return system_health(settings)
    except SQLAlchemyError as exc:
        return {"ready": False, "error": type(exc).__name__}


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    _assert_project_import_path()
    settings = get_settings()
    runtime_mode = settings.runtime_mode.lower()
    configure_logging(settings.log_level)
    logger = get_logger("pados.cli")
    checks: dict[str, Any] = {
        "python": sys.version.split()[0],
        "runtime": {
            "mode": runtime_mode,
            "supported_modes": ["native", "wsl", "docker"],
        },
        "settings": {
            "env": settings.env,
            "runtime_mode": runtime_mode,
            "database_url": settings.safe_database_url,
            "chat_provider": settings.chat_provider,
            "embed_provider": settings.embed_provider,
            "local_llm_concurrency": settings.local_llm_concurrency,
        },
        "files": {
            "docker_compose": Path("docker-compose.yml").exists(),
            "alembic_ini": Path("alembic.ini").exists(),
            "migrations": Path("migrations").exists(),
        },
        "postgres_tools": {
            "psql": shutil.which("psql") is not None,
            "pg_dump": shutil.which("pg_dump") is not None,
            "pg_restore": shutil.which("pg_restore") is not None,
        },
    }

    docker = shutil.which("docker")
    checks["docker"] = {"required": runtime_mode == "docker", "available": docker is not None}
    if runtime_mode == "docker" and docker:
        code, output = _run(["docker", "--context", "default", "compose", "version"])
        checks["docker"]["compose_exit_code"] = code
        checks["docker"]["compose"] = output

    target_error = _doctor_database_target_error(settings)
    if target_error:
        checks["database"] = {"status": "error", "error": target_error}
        checks["migrations"] = {"status": "error", "error": target_error}
    else:
        try:
            operational_settings = settings.model_copy(
                update={"db_name": settings.operational_database_name}
            )
            checks["database"] = check_database(operational_settings)
        except Exception as exc:  # noqa: BLE001 - doctor reports database failure as data
            checks["database"] = {"status": "error", "error": str(exc)}
        checks["migrations"] = _check_migrations(settings)
        checks["system_health"] = _system_health(operational_settings)

    supported_runtime = runtime_mode in checks["runtime"]["supported_modes"]
    docker_ok = runtime_mode != "docker" or (
        checks["files"]["docker_compose"] and checks["docker"]["available"]
    )
    pgvector_ok = checks["database"].get("pgvector") == "true"
    ok = (
        supported_runtime
        and checks["files"]["alembic_ini"]
        and checks["files"]["migrations"]
        and docker_ok
        and checks["database"].get("status") == "ok"
        and pgvector_ok
        and checks["migrations"].get("status") == "ok"
    )
    checks["status"] = "ok" if ok else "error"
    if not supported_runtime:
        checks["runtime"]["status"] = "unsupported"
    else:
        checks["runtime"]["status"] = "ok"

    from pados.execution.readiness import readiness

    checks["readiness"] = readiness(checks)
    from pados.enterprise.status import enterprise_status

    # Optional extension section: reported separately, never part of ``ok``.
    checks["enterprise"] = enterprise_status(settings.enterprise_config,
                                             core_ready=checks["readiness"]["ready"] is True)
    from pados.security_posture import security_posture

    checks["security_posture"] = security_posture(settings)
    from pados.compute_node.status import compute_nodes_status

    # Optional compute-node section: reported separately, never part of ``ok``.
    checks["compute_nodes"] = compute_nodes_status(settings.compute_nodes_config,
                                                   core_ready=checks["readiness"]["ready"] is True)
    from pados.connectivity.config import connectivity_status

    # Optional connectivity section: transport only, never part of ``ok``.
    checks["connectivity"] = connectivity_status(settings.connectivity_config,
                                                 core_ready=checks["readiness"]["ready"] is True,
                                                 compute_nodes=checks["compute_nodes"])
    from pados.herdr.visibility import herdr_visibility

    # Optional Herdr telemetry section: read-only, never part of ``ok`` (HERDR WORKING != PADOS ACCEPTED).
    checks["herdr"] = herdr_visibility(settings.herdr_config)
    from pados.machines.visibility import machine_visibility

    # Optional machine inventory section: references and connection states only, never part of ``ok``.
    checks["machines"] = machine_visibility(settings.machines_config)
    if json_output:
        typer.echo(json.dumps(checks, indent=2, sort_keys=True))
    else:
        typer.echo(f"pados doctor status={checks['status']}")
        typer.echo(f"database={checks['database'].get('status')}")
        typer.echo(f"docker={checks['docker']['available']}")
        typer.echo(f"core={checks['enterprise']['core']} enterprise={checks['enterprise']['status']}")
        typer.echo(f"compute_nodes={checks['compute_nodes']['status']}")
        typer.echo(f"connectivity={checks['connectivity']['status']}")
    logger.info("doctor_completed", status=checks["status"])
    if not ok:
        raise typer.Exit(1)


@project_app.command("create")
def project_create(
    name: Annotated[str, typer.Option("--name", help="Project name.")],
    root_path: Annotated[Path, typer.Option("--root-path", help="Existing project directory.")],
    classification: Annotated[str, typer.Option("--classification")] = "internal",
    default_branch: Annotated[str, typer.Option("--default-branch")] = "main",
    json_output: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    with session_scope() as session:
        project_id = memory_service.bootstrap_project(
            session,
            name=name,
            root_path=root_path,
            classification=classification,
            default_branch=default_branch,
        )
    payload = {
        "status": "ok",
        "project_id": project_id,
        "canonical_root": str(root_path.expanduser().resolve()),
        "classification": classification,
        "default_branch": default_branch,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else project_id)


@memory_app.command("project-register")
def memory_project_register(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Existing project UUID.")],
    canonical_root: Annotated[Path, typer.Option("--canonical-root")],
    classification: Annotated[str, typer.Option("--classification")] = "internal",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        memory_service.register_project(
            session,
            project_id=str(project_id),
            canonical_root=canonical_root,
            classification=classification,
        )
    payload = {"status": "ok", "project_id": str(project_id), "canonical_root": str(canonical_root.resolve())}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "project registered")


@memory_app.command("fact-add")
def memory_fact_add(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    title: Annotated[str, typer.Option("--title")],
    summary: Annotated[str, typer.Option("--summary")],
    source_uri: Annotated[str, typer.Option("--source-uri")],
    verification_status: Annotated[str, typer.Option("--verification-status")] = "UNVERIFIED",
    trust_level: Annotated[str, typer.Option("--trust-level")] = "OBSERVED",
    category: Annotated[str | None, typer.Option("--category", help="Structured category, e.g. failure.")] = None,
    origin_type: Annotated[str, typer.Option("--origin-type")] = "HUMAN",
    actor_type: Annotated[str | None, typer.Option("--actor-type")] = None,
    actor_id: Annotated[str | None, typer.Option("--actor-id")] = "cli-operator",
    run_id: Annotated[UUID | None, typer.Option("--run-id")] = None,
    result_ref: Annotated[str | None, typer.Option("--result-ref")] = None,
    evidence_ref: Annotated[str | None, typer.Option("--evidence-ref")] = None,
    evidence_status: Annotated[str, typer.Option("--evidence-status")] = "EVIDENCE_UNAVAILABLE",
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    runtime: Annotated[str | None, typer.Option("--runtime")] = None,
    contract_id: Annotated[str | None, typer.Option("--contract-id")] = None,
    operation_key: Annotated[str | None, typer.Option("--operation-key")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        memory_id = memory_service.add_memory_record(
            session,
            project_id=str(project_id),
            memory_type="semantic",
            title=title,
            content_summary=summary,
            source_type=category or "observation",
            source_uri=source_uri,
            source_hash=None,
            source_trust_level=trust_level,
            verification_status=verification_status,
            freshness_status="CURRENT",
            importance=80,
            retrieval_priority=80,
            origin_type=origin_type,
            actor_type=actor_type or origin_type,
            actor_id=actor_id,
            run_id=str(run_id) if run_id is not None else None,
            result_ref=result_ref,
            evidence_ref=evidence_ref,
            evidence_status=evidence_status,
            provider=provider,
            model=model,
            runtime=runtime,
            contract_id=contract_id,
            operation_key=operation_key,
        )
    payload = {
        "status": "ok",
        "memory_id": memory_id,
        "authority_state": "CANDIDATE",
        "authority_version": 1,
        "context_eligible": False,
        "origin_type": origin_type,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else memory_id)


@memory_app.command("promote")
def memory_promote(
    memory_id: Annotated[UUID, typer.Option("--memory-id")],
    project_id: Annotated[UUID, typer.Option("--project-id")],
    expected_authority_version: Annotated[int, typer.Option("--expected-authority-version")],
    actor_type: Annotated[str, typer.Option("--actor-type")],
    actor_id: Annotated[str, typer.Option("--actor-id")],
    promotion_gate: Annotated[str, typer.Option("--promotion-gate")],
    authority_state: Annotated[str, typer.Option("--authority-state")] = "PROMOTED",
    evidence_ref: Annotated[str | None, typer.Option("--evidence-ref")] = None,
    evidence_status: Annotated[str, typer.Option("--evidence-status")] = "UNKNOWN",
    result_ref: Annotated[str | None, typer.Option("--result-ref")] = None,
    promotion_reason: Annotated[str | None, typer.Option("--promotion-reason")] = None,
    operation_key: Annotated[str | None, typer.Option("--operation-key")] = None,
    expected_superseded_version: Annotated[int | None, typer.Option("--expected-superseded-version")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        result = memory_service.promote_memory(
            session,
            memory_id=str(memory_id),
            project_id=str(project_id),
            expected_authority_version=expected_authority_version,
            authority_state=authority_state,
            actor_type=actor_type,
            actor_id=actor_id,
            promotion_gate=promotion_gate,
            evidence_ref=evidence_ref,
            evidence_status=evidence_status,
            result_ref=result_ref,
            promotion_reason=promotion_reason,
            operation_key=operation_key,
            expected_superseded_version=expected_superseded_version,
        )
    payload = {"status": "ok", "promotion": result}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else str(result["memory_id"]))


@memory_app.command("decision-add")
def memory_decision_add(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    question: Annotated[str, typer.Option("--question")],
    decision: Annotated[str, typer.Option("--decision")],
    rationale: Annotated[str, typer.Option("--rationale")],
    status: Annotated[str, typer.Option("--status")] = "APPROVED",
    run_id: Annotated[UUID | None, typer.Option("--run-id", help="Optional run UUID.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        decision_id = memory_service.record_decision(
            session,
            project_id=str(project_id),
            run_id=str(run_id) if run_id is not None else None,
            decision_type="architecture",
            question=question,
            decision=decision,
            rationale=rationale,
            status=status,
            decided_by="cli",
        )
    payload = {"status": "ok", "decision_id": decision_id}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else decision_id)


@memory_app.command("artifact-register")
def memory_artifact_register(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    path: Annotated[Path, typer.Option("--path")],
    artifact_type: Annotated[str, typer.Option("--artifact-type")] = "report",
    run_id: Annotated[UUID | None, typer.Option("--run-id", help="Optional run UUID.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        artifact_id = memory_service.register_artifact(
            session,
            project_id=str(project_id),
            path=path,
            artifact_type=artifact_type,
            run_id=str(run_id) if run_id is not None else None,
        )
    payload = {"status": "ok", "artifact_id": artifact_id}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else artifact_id)


def _load_json_object(payload_json: str | None, payload_file: Path | None) -> dict[str, Any]:
    if payload_json is not None and payload_file is not None:
        raise ValueError("provide only one of --payload-json and --payload-file")
    if payload_file is not None:
        try:
            raw_payload = payload_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("payload file must be UTF-8 JSON") from exc
    else:
        raw_payload = payload_json if payload_json is not None else "{}"
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")  # noqa: TRY004 - CLI maps ValueError to a usage error
    return cast(dict[str, Any], payload)


@memory_app.command("event-add")
def memory_event_add(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    run_id: Annotated[UUID, typer.Option("--run-id", help="Run UUID.")],
    event_type: Annotated[str, typer.Option("--event-type", help=EVENT_TYPE_HELP)],
    payload_json: Annotated[str | None, typer.Option("--payload-json", help="Inline JSON object.")] = None,
    payload_file: Annotated[Path | None, typer.Option("--payload-file", help="UTF-8 JSON object file.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    parsed_payload = _load_json_object(payload_json, payload_file)
    with session_scope() as session:
        event_id = memory_service.append_run_event(
            session,
            project_id=str(project_id),
            run_id=str(run_id),
            event_type=event_type,
            payload_json=parsed_payload,
        )
    result = {"status": "ok", "event_id": event_id}
    typer.echo(json.dumps(result, indent=2, sort_keys=True) if json_output else event_id)


@memory_app.command("search")
def memory_search(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    query: Annotated[str, typer.Option("--query")],
    max_tokens: Annotated[int, typer.Option("--max-tokens")] = 2000,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        rows = search_memory(
            session,
            RetrievalRequest(project_id=str(project_id), query=query, purpose="cli-search", max_tokens=max_tokens),
        )
    payload = {"status": "ok", "results": [row.__dict__ for row in rows]}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else str(len(rows)))


@memory_app.command("stale")
def memory_stale(
    memory_id: Annotated[UUID, typer.Option("--memory-id", help="Memory UUID.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        memory_service.mark_memory_stale(session, memory_id=str(memory_id))
    payload = {"status": "ok", "memory_id": str(memory_id), "freshness_status": "STALE"}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "stale")


@context_app.command("assemble")
def context_assemble(
    project_id: Annotated[UUID, typer.Option("--project-id", help="Project UUID.")],
    query: Annotated[str, typer.Option("--query")],
    purpose: Annotated[str, typer.Option("--purpose")],
    request_id: Annotated[str, typer.Option("--request-id")] = "cli",
    max_tokens: Annotated[int, typer.Option("--max-tokens")] = 2400,
    output_json: Annotated[Path | None, typer.Option("--output-json")] = None,
    output_markdown: Annotated[Path | None, typer.Option("--output-markdown")] = None,
) -> None:
    with session_scope() as session:
        packet_payload = context_packet.assemble_context_packet(
            session,
            request=RetrievalRequest(
                project_id=str(project_id),
                query=query,
                purpose=purpose,
                max_tokens=max_tokens,
            ),
            request_id=request_id,
        )
    if output_json and output_markdown:
        context_packet.write_packet_artifacts(
            packet_payload,
            json_path=output_json,
            markdown_path=output_markdown,
        )
    typer.echo(json.dumps(packet_payload, indent=2, sort_keys=True))


@context_app.command("inspect")
def context_inspect(
    packet_id: Annotated[UUID, typer.Option("--packet-id", help="Context packet UUID.")],
) -> None:
    with session_scope() as session:
        payload = context_packet.context_packet_json(session, str(packet_id))
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command()
def version(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show product, protocol, schema and migration versions."""
    from pados.versions import version_report

    report = version_report()
    typer.echo(json.dumps(report, indent=2, sort_keys=True) if json_output
               else f"pados {report['product']['version']} ({report['product']['channel']})")


@app.command()
def init(
    directory: Annotated[Path, typer.Option("--dir", help="Where to write .env and runtime/local.")] = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing .env.")] = False,
) -> None:
    """First-run setup: write a local .env with a unique control token (single-user, loopback)."""
    import hashlib
    import os
    import secrets as token_source

    root = directory.resolve()
    env_path = root / ".env"
    if env_path.exists() and not force:
        typer.echo(f"exists: {env_path} (use --force to overwrite)")
        raise typer.Exit(1)
    local = root / "runtime" / "local"
    for sub in ("workspace", "objects"):
        (local / sub).mkdir(parents=True, exist_ok=True)
    control_value = token_source.token_urlsafe(32)
    token_file = local / "control.token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(control_value + "\n")
    lines = [
        "# Generated by `pados init` (LOCAL_SINGLE_USER profile). Edit the database password.",
        "PADOS_ENV=local", "PADOS_RUNTIME_MODE=native", "PADOS_API_HOST=127.0.0.1", "PADOS_API_PORT=8088",
        "PADOS_DB_HOST=127.0.0.1", "PADOS_DB_PORT=5432", "PADOS_DB_USER=pados",
        "PADOS_DB_PASSWORD=" + os.environ.get("PADOS_DB_PASSWORD", "CHANGE_ME"),
        # Dedicated dogfood lanes: the ``pados_*_runtime`` names are reserved for the protected canonical runtime
        # and `pados doctor` refuses them (PROTECTED_DATABASE_TARGET).
        "PADOS_DB_NAME=pados_dogfood_operational", "PADOS_OPERATIONAL_DATABASE_NAME=pados_dogfood_operational",
        "PADOS_PRODUCT_DATABASE_NAME=pados_dogfood_proof", "PADOS_CORPUS_DATABASE_NAME=pados_dogfood_corpus",
        f"PADOS_PRODUCT_WORKSPACE_ROOT={local / 'workspace'}", f"PADOS_PRODUCT_OBJECT_ROOT={local / 'objects'}",
        "PADOS_PRODUCT_LOOP_PROVIDER=AUTO",
        "PADOS_CONTROL_API_TOKEN_HASH=" + hashlib.sha256(control_value.encode("utf-8")).hexdigest().upper(),
        "# Optional extensions (unset = not configured; never affects Core readiness):",
        "# PADOS_ENTERPRISE_CONFIG=examples/config/enterprise-synthetic.json",
        "# PADOS_COMPUTE_NODES_CONFIG=examples/config/compute-nodes-loopback.json",
        "# PADOS_MODEL_CATALOG=<operator model catalog holding accepted measured profiles>",
        "# PADOS_LOCAL_INFERENCE_TIMEOUT_SECONDS=30",
    ]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.echo(json.dumps({"env": str(env_path), "control_token_file": str(token_file),
                           "next": ["pados doctor", "alembic upgrade (see docs/product/QUICKSTART.md)", "pados serve"]},
                          indent=2))


@app.command()
def serve() -> None:
    """Run the PADOS API on the configured loopback address."""
    import uvicorn

    from pados.control.security import assert_localhost_binding, assert_unique_control_token

    settings = get_settings()
    assert_localhost_binding(settings)
    assert_unique_control_token(settings)
    uvicorn.run("pados.api.app:app", host=settings.api_host, port=settings.api_port, log_level="info")


@node_app.command("serve")
def node_serve(
    config: Annotated[Path, typer.Option("--config", help="Node config (pados.compute-node.config.v1).")],
) -> None:
    """Run this host as a Local Compute Node (loopback/private network only)."""
    import uvicorn

    from pados.compute_node.config import NodeConfig, validate_bind
    from pados.compute_node.service import NodeRuntime, create_node_app

    node_config = NodeConfig.model_validate(json.loads(config.read_text(encoding="utf-8")))
    validate_bind(node_config)
    runtime = NodeRuntime(node_config)
    typer.echo(json.dumps({"node_id": runtime.identity.node_id, "endpoint": node_config.endpoint}))
    uvicorn.run(create_node_app(runtime), host=node_config.bind_host, port=node_config.port, log_level="warning")


@node_app.command("probe")
def node_probe(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Probe registered nodes and persist evidence-derived state (PADOS_COMPUTE_NODES_CONFIG)."""
    from pados.compute_node.status import compute_nodes_status

    payload = compute_nodes_status(get_settings().compute_nodes_config, core_ready=False)
    payload.pop("core", None)
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for node in payload["nodes"]:
            typer.echo(f"{node['node_id']} {node['state']} {','.join(node['reasons'][:2])}")
        typer.echo(f"compute_nodes={payload['status']}")


@worker_app.command("list")
def worker_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    workers = worker_registry.discover_workers(get_settings())
    payload = {"status": "ok", "workers": [worker.model_dump(mode="json") for worker in workers.values()]}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "\n".join(workers))


@worker_app.command("health")
def worker_health(
    worker_id: Annotated[str | None, typer.Option("--worker-id")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    workers = worker_registry.discover_workers(get_settings())
    selected = workers if worker_id is None else {worker_id: workers[worker_id]}
    payload = {
        "status": "ok",
        "health": [worker_registry.worker_health_payload(worker) for worker in selected.values()],
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "\n".join(selected))


@worker_app.command("capabilities")
def worker_capabilities(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    capabilities = worker_registry.capability_registry()
    payload = {"status": "ok", "capabilities": [item.model_dump(mode="json") for item in capabilities.values()]}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "\n".join(cap.value for cap in capabilities))


@worker_app.command("diagnose")
def worker_diagnose(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    workers = worker_registry.discover_workers(get_settings())
    capabilities = worker_registry.capability_registry()
    payload = {
        "status": "ok",
        "workers": [worker.model_dump(mode="json") for worker in workers.values()],
        "capability_count": len(capabilities),
        "fail_closed": [
            worker.worker_id
            for worker in workers.values()
            if worker.availability.value in {"UNAVAILABLE", "AUTH_REQUIRED", "DISABLED"}
        ],
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True) if json_output else "worker diagnose completed")


def _echo_json(payload: dict[str, Any] | list[dict[str, Any]], json_output: bool) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str) if json_output else str(payload))


def _payload(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


@loop_app.command("create-goal")
def loop_create_goal(
    payload_json: Annotated[Path, typer.Option("--payload-json")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        row = loop_service.create_task_goal(session, TaskGoalCreate(**_payload(payload_json)))
    _echo_json({"status": "ok", "task_goal": row}, json_output)


@loop_app.command("create-plan")
def loop_create_plan(
    payload_json: Annotated[Path, typer.Option("--payload-json")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        row = loop_service.create_task_plan(
            session, TaskPlanCreate(**_payload(payload_json)), settings=get_settings()
        )
    _echo_json({"status": "ok", "task_plan": row}, json_output)


@loop_app.command("create")
def loop_create(
    payload_json: Annotated[Path, typer.Option("--payload-json")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        row = loop_service.create_loop(session, LoopCreate(**_payload(payload_json)))
    _echo_json({"status": "ok", "loop": row}, json_output)


@loop_app.command("start")
def loop_start(
    loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")],
    owner: Annotated[str, typer.Option("--owner")] = "pados-cli",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        row = loop_service.start_loop(session, str(loop_id), owner_instance=owner)
    _echo_json({"status": "ok", "loop": row}, json_output)


@loop_app.command("step")
def loop_step(
    loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        result = loop_service.run_one_iteration(
            session,
            loop_id=str(loop_id),
            settings=get_settings(),
            artifact_root=Path("runtime/artifacts"),
            idempotency_key=idempotency_key,
        )
    _echo_json({"status": "ok", **result}, json_output)


@loop_app.command("status")
def loop_status(
    loop_id: Annotated[UUID | None, typer.Option("--loop-id", help="Optional loop UUID.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        payload: dict[str, Any] | list[dict[str, Any]]
        payload = loop_service.get_loop(session, str(loop_id)) if loop_id else loop_service.list_loops(session)
    _echo_json({"status": "ok", "loops": payload}, json_output)


@loop_app.command("inspect")
def loop_inspect(
    loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with session_scope() as session:
        payload = {
            "loop": loop_service.get_loop(session, str(loop_id)),
            "iterations": loop_service.loop_iterations(session, str(loop_id)),
            "events": loop_service.loop_events(session, str(loop_id)),
        }
    _echo_json({"status": "ok", **payload}, json_output)


@loop_app.command("pause")
def loop_pause(loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")], json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with session_scope() as session:
        row = loop_service.pause_loop(session, str(loop_id))
    _echo_json({"status": "ok", "loop": row}, json_output)


@loop_app.command("resume")
def loop_resume(loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")], json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with session_scope() as session:
        row = loop_service.resume_loop(session, str(loop_id))
    _echo_json({"status": "ok", "loop": row}, json_output)


@loop_app.command("cancel")
def loop_cancel(loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")], json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with session_scope() as session:
        row = loop_service.cancel_loop(session, str(loop_id))
    _echo_json({"status": "ok", "loop": row}, json_output)


@loop_app.command("events")
def loop_events(loop_id: Annotated[UUID, typer.Option("--loop-id", help="Loop UUID.")], json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with session_scope() as session:
        rows = loop_service.loop_events(session, str(loop_id))
    _echo_json({"status": "ok", "events": rows}, json_output)


@loop_app.command("diagnose")
def loop_diagnose(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    payload = {
        "status": "ok",
        "bounded": True,
        "deterministic": True,
        "approval_aware": True,
        "fail_closed": True,
        "commands": [
            "create-goal",
            "create-plan",
            "create",
            "start",
            "status",
            "inspect",
            "pause",
            "resume",
            "cancel",
            "step",
            "events",
        ],
    }
    _echo_json(payload, json_output)


def _install_operator_error_boundaries() -> None:
    def install(group: typer.Typer) -> None:
        for command in group.registered_commands:
            if command.callback is not None:
                command.callback = operator_error_boundary(command.callback)
        for subgroup in group.registered_groups:
            if subgroup.typer_instance is not None:
                install(subgroup.typer_instance)

    install(app)


_install_operator_error_boundaries()


if __name__ == "__main__":
    app()


@storage_app.command("report")
def storage_report_command(
    runtime_root: Annotated[Path, typer.Option("--runtime-root", help="PADOS runtime directory.")] = Path("runtime"),
    database: Annotated[bool, typer.Option("--database", help="Also account operational DB tables.")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Storage by category with an eligible_for_purge ESTIMATE. Never deletes anything."""
    from pados.config import lane_settings
    from pados.lifecycle.accounting import storage_report
    from pados.state_proof.service import build_proof_engine

    settings = get_settings()
    engine = None
    if database:
        engine = build_proof_engine(lane_settings(settings, settings.operational_database_name).database_url)
    herdr_root = None
    if settings.herdr_config:
        from pados.herdr.config import HerdrConfig, load_config

        loaded = load_config(settings.herdr_config)
        herdr_root = loaded.store_root if isinstance(loaded, HerdrConfig) else None
    report = storage_report(settings, runtime_root=runtime_root.resolve(), engine=engine, include_database=database,
                            herdr_root=herdr_root)
    if json_output:
        typer.echo(json.dumps(report, indent=2, sort_keys=True, default=str))
        return
    for name, category in sorted(report.get("categories", {}).items()):
        eligible = category.get("eligible_for_purge", {})
        typer.echo(f"{name} bytes={category.get('bytes', 0)} files={category.get('file_count', 0)} "
                   f"eligible_bytes={eligible.get('bytes', 0)}")
    typer.echo(f"recommendation_only={report.get('recommendation_only')} "
               f"autonomous_deletion={report.get('autonomous_deletion')}")


def _machine_store() -> Any:
    from pados.machines.inventory import InventoryStore

    path = get_settings().machines_config
    if not path:
        typer.echo("PADOS_MACHINES_CONFIG_REQUIRED", err=True)
        raise typer.Exit(2)
    return InventoryStore(path)


def _machine_probe(store: Any, policy_path: str | None) -> Any:
    """OpenSshProbe with the operator credential policy (a separate owner-only file, never the inventory).

    Without a readable policy the probe still pins host keys but never resolves or uses a credential.
    """
    from pados.machines.errors import MachineError
    from pados.machines.policy import CredentialPolicy, default_policy_path
    from pados.machines.ssh import OpenSshProbe

    path = policy_path or default_policy_path(store.path)
    try:
        policy = CredentialPolicy.load(path, root=store.path.parent if policy_path is None else None)
    except MachineError as exc:  # codes only; the probe then reports CREDENTIAL_POLICY_REQUIRED
        typer.echo(exc.code, err=True)
        policy = None
    return OpenSshProbe(policy=policy)


def _machine_call(action: Callable[[], Any]) -> Any:
    from pados.machines.errors import MachineError

    try:
        return action()
    except MachineError as exc:  # codes only: never echoes hosts, usernames or references
        typer.echo(exc.code, err=True)
        raise typer.Exit(1) from None


@machine_app.command("list")
def machine_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Registered machines: identity, roles, connection state, staleness (no credentials)."""
    from pados.machines.ux import list_machines

    rows = _machine_call(lambda: list_machines(_machine_store()))
    if json_output:
        typer.echo(json.dumps(rows, indent=2, sort_keys=True, default=str))
        return
    for row in rows:
        typer.echo(" ".join(f"{key}={row.get(key)}" for key in ("display_name", "machine_id", "connection_state")))


@machine_app.command("show")
def machine_show(name: Annotated[str, typer.Argument(help="Display name or machine id.")]) -> None:
    """Human summary of one machine (authentication shown as a reference scheme only)."""
    from pados.machines.ux import describe_machine

    # Same freshness projection as list / Mission Control: an old observation shows as STALE.
    typer.echo(_machine_call(lambda: describe_machine(_machine_store(), name)))


@machine_app.command("add")
def machine_add(
    name: Annotated[str, typer.Option("--name", help="Display name, e.g. Mac.")],
    host: Annotated[str, typer.Option("--host", help="Private/LAN/overlay IP or confirmed hostname.")],
    username: Annotated[str, typer.Option("--user", help="Account on the machine.")],
    auth: Annotated[str, typer.Option("--auth", help="ssh-agent | ssh-key | os-credential")] = "ssh-agent",
    auth_ref: Annotated[str | None, typer.Option("--auth-ref", help="secretref://, ssh-agent:// or oscred://")] = None,
    platform: Annotated[str, typer.Option("--platform", help="MACOS | LINUX | WINDOWS")] = "LINUX",
    role: Annotated[list[str] | None, typer.Option("--role", help="HOME_BASE, LOCAL_COMPUTE_NODE, ...")] = None,
    fingerprint: Annotated[str | None, typer.Option("--fingerprint", help="Expected SHA256:... host key.")] = None,
    accept_new_host_key: Annotated[bool, typer.Option("--accept-new-host-key")] = False,
    hostname_confirmed: Annotated[bool, typer.Option("--hostname-confirmed")] = False,
    port: Annotated[int, typer.Option("--port")] = 22,
    credential_policy: Annotated[str | None, typer.Option(
        "--credential-policy", help="Owner-only credential policy file (default: next to the inventory).")] = None,
) -> None:
    """Register a machine. Identity is its SSH host key; PADOS never stores a password."""
    from pados.machines.ux import add_machine, describe

    store = _machine_store()
    profile = _machine_call(lambda: add_machine(
        name, host, username, auth, store=store, probe=_machine_probe(store, credential_policy), port=port,
        platform=platform,
        roles=role or (), auth_ref=auth_ref, expected_fingerprint=fingerprint,
        accept_new_host_key=accept_new_host_key, hostname_confirmed=hostname_confirmed))
    typer.echo(describe(profile))


@machine_app.command("test")
def machine_test(
    name: Annotated[str, typer.Argument(help="Display name or machine id.")],
    credential_policy: Annotated[str | None, typer.Option(
        "--credential-policy", help="Owner-only credential policy file (default: next to the inventory).")] = None,
) -> None:
    """Authentication-only test with the pinned host key (ssh -N: no session, no remote command, no password)."""
    from pados.machines.ux import check_machine

    store = _machine_store()
    result = _machine_call(lambda: check_machine(store, name, _machine_probe(store, credential_policy)))
    typer.echo(f"state={result.connection_state.value} code={result.code}")


@machine_app.command("remove")
def machine_remove(name: Annotated[str, typer.Argument(help="Display name or machine id.")]) -> None:
    """Remove a machine from the inventory (its pinned host key is dropped with it)."""
    from pados.machines.ux import remove_machine

    typer.echo(f"removed={_machine_call(lambda: remove_machine(_machine_store(), name))}")
