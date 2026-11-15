# SPDX-License-Identifier: AGPL-3.0-only
"""Live Work Map + Resources E2E: the 工作心智圖 and 資源與儲存 pages against a real ``pados serve`` and PostgreSQL.

Reuses the product work-loop bootstrap unchanged (scripts/dogfood/product_flow_e2e.py: ``pados init``, a private loopback
PostgreSQL, the lane migrations, identity sessions on, browser pairing, and the HTTP demo flow with a paired session:
project → Markdown with a broken link → check → WAITING_HUMAN → cancel → fix → check again → COMPLETED → ResultCandidate
waiting for the WebAuthn step-up). Then, still with the paired session only:

  1. ``GET /control/projects/{id}/work-map`` (PROJECTION ONLY): both TaskLoops, their iterations, jobs and artifact
     receipts (SHA-256), and the candidate still PENDING with no decision;
  2. ``GET /control/resources``: every metric has source, unit and measured/UNKNOWN; the database and RAM are measured;
     model memory is UNKNOWN (no provider configured, so no runtime is called); the cleanup plan never selects evidence,
     decisions, audit records or unique copies and offers no execute action;
  3. with ``--playwright``: web/e2e/work-map-resources-live.spec.ts finds the demo task in the Work Map with its
     Evidence and decision state, switches mind map ↔ list, keeps the view state across pages, and checks the Resources
     page (screenshots to ``--evidence``).

Evidence: ``<out>/WORK_MAP_RESOURCES_E2E_RESULT.json`` (``pados.work-map-resources-e2e.v1``). Exit 0 only when no step
FAILED. Loopback only; never uses a port that is already taken.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity_e2e as identity  # noqa: E402 - shared helpers (unchanged)
import local_dogfood_e2e as dogfood  # noqa: E402 - shared helpers (unchanged)
import product_flow_e2e as product  # noqa: E402 - the demo flow (unchanged)

REPO = dogfood.REPO
CONTRACT = "pados.work-map-resources-e2e.v1"
PROTECTED_CATEGORIES = {"AUTHORITY_METADATA", "ARTIFACTS", "RELEASE_TEST_EVIDENCE", "PROJECT_SOURCES", "REMOTE_UPLOADS"}


def projections(ctx: identity.Context, browser: identity.Browser, project_name: str) -> None:
    run, api = ctx.run, product.Product(identity.SessionApi(browser))
    _, listing = api.call("GET", "/control/projects")
    project = next(p for p in listing["projects"] if p["name"] == project_name)
    status, work = api.call("GET", f"/control/projects/{project['id']}/work-map")
    (run.out / "work_map_projection.json").write_text(json.dumps(work, indent=2, default=str), encoding="utf-8")
    execution = work.get("execution", {}) if isinstance(work, dict) else {}
    loops = {loop["id"]: loop["status"] for loop in work.get("loops", [])}
    iterations = execution.get("iterations", [])
    artifacts = execution.get("artifacts", [])
    candidate = (work.get("candidates") or [{}])[0]
    # Every iteration names a step of a plan in the projection (a governed residual amendment adds steps 2..n).
    step_ids = {step.get("id") for plan in work.get("plans", []) for step in plan.get("steps", [])}
    run.record("work_map_projection_has_execution_evidence_and_decision", status == 200
               and work.get("authority") == "PROJECTION_ONLY" and work.get("projection") == "pados.control.work-map.v1"
               and sorted(loops.values()) == ["CANCELLED", "COMPLETED"]
               and {i["loop_id"] for i in iterations} == set(loops)
               and all(i.get("job_id") and i.get("step_id") in step_ids and isinstance(i.get("step_sequence"), int)
                       for i in iterations)
               and {j["id"] for j in execution.get("jobs", [])} == {i["job_id"] for i in iterations}
               and bool(artifacts) and all(re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) for a in artifacts)
               and not any("path" in a for a in artifacts)
               and candidate.get("approval_status") == "PENDING" and candidate.get("decision") is None
               and all(step.get("id") for plan in work.get("plans", []) for step in plan.get("steps", [])),
               http=status, loops=sorted(loops.values()), iterations=len(iterations), artifacts=len(artifacts),
               step_sequences=sorted(i.get("step_sequence") or 0 for i in iterations),
               artifact_types=sorted({a["artifact_type"] for a in artifacts}), candidate=candidate)

    status, summary = api.call("GET", "/control/resources")
    (run.out / "resources_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    metrics = {m["id"]: m for m in summary.get("metrics", [])} if isinstance(summary, dict) else {}
    plan = summary.get("cleanup_plan", {}) if isinstance(summary, dict) else {}
    well_formed = all({"source", "unit", "measured", "measured_at", "status"} <= set(m)
                      and (m["status"] == "MEASURED") == bool(m["measured"])
                      and (m["measured"] or (m["value"] is None and m["reason"])) for m in metrics.values())
    run.record("resources_summary_measures_or_says_unknown", status == 200 and well_formed
               and metrics["ram_in_use"]["measured"] and metrics["database_size"]["measured"]
               and metrics["execution_records"]["measured"] and metrics["evidence_files"]["measured"]
               and metrics["model_resident_memory"]["reason"] == "MODEL_RUNTIME_NOT_CONFIGURED"
               and summary["trends"]["storage_bytes"]["reason"] == "HISTORY_NOT_RECORDED",
               http=status, statuses={k: m["status"] for k, m in metrics.items()},
               unknown={k: m["reason"] for k, m in metrics.items() if not m["measured"]})
    candidates = plan.get("candidates", [])
    run.record("cleanup_plan_is_a_preview_that_protects_evidence", plan.get("deletes") is False
               and plan.get("plan_only") is True and plan.get("execute", {}).get("available") is False
               and not any(c["category"] in PROTECTED_CATEGORIES for c in candidates)
               and not any(re.search(r"EVIDENCE|RECEIPT|DECISION|AUDIT|LEDGER", k) for c in candidates
                           for k in c["data_kinds"])
               and "job_artifacts" in {p["location_id"] for p in plan.get("protected", [])},
               candidates=[c["location_id"] for c in candidates],
               protected=sorted(p["location_id"] for p in plan.get("protected", [])),
               execute=plan.get("execute"))


def playwright_live(ctx: identity.Context, args: argparse.Namespace, project_name: str) -> None:
    run = ctx.run
    if identity.port_busy(args.web_port):
        run.record("playwright_work_map_resources_live", False, reason=f"web port {args.web_port} is already in use")
        return
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_")}
    env.update({"PADOS_WEB_PORT": str(args.web_port), "PADOS_WEB_BACKEND_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_MAP_LIVE": "1", "PADOS_E2E_PYTHON": sys.executable,
                "PADOS_E2E_API_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_TOKEN_FILE": str(ctx.root / "runtime" / "local" / "control.token"),
                "PADOS_E2E_PROJECT_NAME": project_name, "PADOS_E2E_EVIDENCE_DIR": str(evidence), "PYTHONUTF8": "1"})
    argv = ["npx", "playwright", "test", "e2e/work-map-resources-live.spec.ts", "--reporter=list", "--workers=1",
            "--output", str(run.out / "playwright-results")]
    result = subprocess.run(argv, cwd=REPO / "web", env=env, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800, shell=os.name == "nt")
    (run.out / "playwright_work_map_resources_live.log").write_text(
        f"$ {' '.join(argv)}\nexit={result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}",
        encoding="utf-8")
    summary = [line.strip() for line in result.stdout.splitlines() if re.search(r"\d+ (passed|failed|skipped)", line)]
    shots = sorted(p.name for p in evidence.glob("*.png"))
    run.record("playwright_work_map_resources_live", result.returncode == 0 and any("passed" in s for s in summary)
               and not any("failed" in s or "skipped" in s for s in summary) and bool(shots),
               exit=result.returncode, summary=summary, screenshots=shots, evidence_dir=str(evidence))


def exercise(args: argparse.Namespace, run: dogfood.Run, root: Path, env: dict[str, str]) -> None:
    control_value = (root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    ctx = identity.Context(run, env, root, identity.Http(args.api_port), args.api_port,
                           f"http://127.0.0.1:{args.web_port}", control_value)
    ctx.remember("control", control_value)
    server = dogfood.start_server(run, env, args.api_port, "serve_work_map")
    try:
        browser = ctx.browser("Work map E2E")
        paired = ctx.pair(browser, ctx.mint_code("device_pair_cli_json", "Work map E2E"))
        run.record("browser_paired", paired.status == 200 and bool(browser.csrf), http=paired.status)
        project_name = f"心智圖示範 {int(time.time())}"
        product.flow(ctx, browser, project_name)
        projections(ctx, browser, project_name)
        if args.playwright:
            playwright_live(ctx, args, project_name)
        else:
            identity.note(run, "playwright_work_map_resources_live", "SKIPPED", reason="run without --playwright")
    finally:
        dogfood.stop_server(server)
    product.leak_scan(ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, help="screenshot directory for --playwright (default: <out>/shots)")
    parser.add_argument("--pg-bin", type=Path, help="native PostgreSQL bin dir: start a private loopback cluster")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55481)
    parser.add_argument("--pg-user", default="pados")
    parser.add_argument("--api-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--web-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--playwright", action="store_true", help="also run web/e2e/work-map-resources-live.spec.ts")
    args = parser.parse_args(argv)
    args.api_port = args.api_port or identity.free_port()
    args.web_port = args.web_port or identity.free_port()
    run = dogfood.Run(args.out.resolve())
    args.evidence = args.evidence or run.out / "shots"
    root = Path(tempfile.mkdtemp(prefix="pados-map-e2e-"))
    db_pass = "" if args.pg_bin else os.environ.get("PADOS_DB_PASSWORD", "")
    cluster = dogfood.Cluster(args.pg_bin, root, args.pg_port) if args.pg_bin else None
    started = time.time()
    try:
        for name, port in (("api", args.api_port), ("web", args.web_port), ("postgres", args.pg_port if cluster else 0)):
            if port and identity.port_busy(port):
                raise RuntimeError(f"{name} port {port} is already in use")
        if cluster:
            cluster.start()
        run.record("postgres_ready", True, mode="private-cluster" if cluster else "existing-server", port=args.pg_port)
        env = identity.prepare(args, run, root, db_pass)
        env["PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS"] = "3600"
        exercise(args, run, root, env)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded as evidence, then the run fails
        run.record("unexpected_failure", False, error=f"{type(exc).__name__}: {exc}"[:800])
    finally:
        if cluster:
            cluster.stop()
        with contextlib.suppress(OSError):
            shutil.rmtree(root, ignore_errors=True)
    passed = not any(step["status"] == "FAIL" for step in run.steps)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    counts = {status: sum(1 for s in run.steps if s["status"] == status) for status in ("PASS", "FAIL", "SKIPPED")}
    (run.out / "WORK_MAP_RESOURCES_E2E_RESULT.json").write_text(json.dumps({
        "runner_contract": CONTRACT, "verdict": "PASS" if passed else "FAIL", "commit": head,
        "platform": platform.platform(), "python": sys.version.split()[0], "seconds": int(time.time() - started),
        "finished": datetime.now(UTC).isoformat(), "counts": counts,
        "options": {"playwright": args.playwright, "pg_mode": "private-cluster" if cluster else "existing-server",
                    "api_port": args.api_port, "web_port": args.web_port},
        "steps": run.steps}, indent=2, default=str), encoding="utf-8")
    print(f"WORK MAP + RESOURCES E2E {'PASS' if passed else 'FAIL'} {counts} -> "
          f"{run.out / 'WORK_MAP_RESOURCES_E2E_RESULT.json'}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
