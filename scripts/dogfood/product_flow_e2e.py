# SPDX-License-Identifier: AGPL-3.0-only
"""Live product work-loop E2E: the Control Center's daily-work flow as real processes over real HTTP (loopback only).

Runs, with a paired browser session only (cookie + X-PADOS-CSRF + Origin; no control token on any product call), the
exact calls the web Control Center makes, against a real ``pados serve`` and a real PostgreSQL, and records each step:

  1. ``pados init``, a private loopback PostgreSQL (``--pg-bin``) or an existing server, the documented lane migrations,
     identity sessions on (scripts/dogfood/identity_e2e.py ``prepare``); ``pados serve``; pairing with a code from
     ``pados device pair``
  2. 建立專案: ``POST /control/projects`` (a name and a description; no path, no id); a replay with the same key returns
     the same project; the folder is inside the workspace root and holds the README
  3. 新增 Markdown: ``POST /control/projects/{id}/documents`` writes ``guide.md`` with one good and one broken link;
     a traversal name is refused; an overwrite without the current SHA-256 is refused
  4. 要求檢查 -> 檢視計畫 -> 核准 -> 執行: a work request naming RUN_STATIC_ANALYSIS (server-captured TaskEnvelope), a
     WORK_ROUTE approval for exactly one ``markdown_reference_check`` step, approve, task goal, the deterministic
     one-step plan bound to the envelope's root goal, TaskLoop create/start/step until it stops
  5. Evidence: the job's verified stderr artifact names the broken link; the loop waits for a human (WAITING_HUMAN) and
     the backend refuses a ResultCandidate (RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP); the loop is cancelled
  6. the document is fixed (replace with the SHA-256 read before), the check runs again and COMPLETES with no broken
     link; 提交候選結果: the candidate is verified and WAITS for approval; the session cannot approve it
     (RESULT_ACCEPTANCE_REQUIRES_STEP_UP, the approval stays PENDING): only the Remote WebAuthn step-up decides
  7. the projection ``GET /control/projects/{id}/work`` shows the loops, the pending candidate (no decision) and the
     audit events; step-up readiness says the Remote console is not configured (the recoverable pending state)
  8. optional ``--playwright``: web/e2e/product-flow-live.spec.ts drives the same demo through the browser UI
     (screenshots to ``--evidence``); the control token never appears in a reply or a captured file

Evidence: ``<out>/PRODUCT_FLOW_E2E_RESULT.json`` (``pados.product-flow-e2e.v1``) plus per-step logs. Exit 0 only when no
step FAILED.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity_e2e as identity  # noqa: E402 - shared helpers of the identity E2E (unchanged)
import local_dogfood_e2e as dogfood  # noqa: E402 - shared helpers of the dogfood E2E (unchanged)

REPO = dogfood.REPO
CONTRACT = "pados.product-flow-e2e.v1"
PROBE = "markdown_reference_check"
OPERATIONS = [{"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": PROBE,
               "max_risk_level": "LOW"}]
BROKEN_GUIDE = "# 使用指南\n\n請先閱讀[專案說明](README.md)。\n\n更多內容見[安裝步驟](missing-install.md)。\n"
FIXED_GUIDE = "# 使用指南\n\n請先閱讀[專案說明](README.md)。\n"
STOPPED = {"COMPLETED", "PAUSED", "BLOCKED", "WAITING_HUMAN", "FAILED_TERMINAL", "FAILED_RECOVERABLE", "CANCELLED",
           "PARTIALLY_COMPLETED", "TIMED_OUT"}


def key(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class Product:
    """The Control Center's calls, through the paired session only (identity_e2e.SessionApi)."""

    def __init__(self, api: identity.SessionApi) -> None:
        self.api = api

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        return self.api.call(method, path, body)

    def check(self, project_id: str, title: str, text: str) -> dict[str, Any]:
        """要求檢查 -> 計畫 -> 核准 -> 執行, exactly as the page does it; returns every id and answer."""
        out: dict[str, Any] = {}
        status, created = self.call("POST", "/control/work-requests", {
            "project_id": project_id, "title": title, "body": text, "idempotency_key": key("web-work"),
            "requested_capability": "RUN_STATIC_ANALYSIS"})
        out["request_http"] = status
        request = created["work_request"]
        envelope = json.loads(request["body"])["task_envelope"]
        root_goal = envelope["normalized_goals"][0]["goal_id"]
        subject = {"kind": "WORK_ROUTE", "schema_version": dogfood.SUBJECT_SCHEMA, "project_id": project_id,
                   "request_id": request["id"], "actor": {"surfaces": ["TASK_LOOP"],
                                                          "worker_types": ["DETERMINISTIC_TOOL"]},
                   "operations": OPERATIONS, "scope_version_ref": None, "task_envelope_hash": None}
        status, approval = self.call("POST", "/control/approvals", {
            "project_id": project_id, "idempotency_key": key("web-plan"), "subject": subject,
            "proposal_summary": f"Deterministic template (no AI): {PROBE} on the project's Markdown"})
        out["approval_http"] = status
        approval = approval["approval"]
        status, decided = self.call("POST", f"/control/approvals/{approval['id']}/approve", {
            "version": approval["version"], "idempotency_key": key("web-approve"), "reason": ""})
        out["approve_http"], out["approval_status"] = status, (decided.get("approval") or {}).get("status")
        status, goal = self.call("POST", "/control/task-goals", {
            "project_id": project_id, "request_id": request["id"], "approval_id": approval["id"],
            "idempotency_key": key("web-goal"), "objective": f"{title}: {text}"[:1000],
            "success_criteria": ["every relative Markdown link in the project resolves",
                                 "an execution receipt with artifact digests exists"]})
        out["goal_http"] = status
        goal_id = goal["task_goal"]["id"]
        status, plan = self.call("POST", "/control/task-plans", {
            "task_goal_id": goal_id, "approval_id": approval["id"], "idempotency_key": key("web-taskplan"),
            "steps": [{"sequence": 1, "product_goal_id": root_goal, "title": "Check every Markdown link",
                       "step_type": "RUN_VALIDATION", "required_capability": "RUN_STATIC_ANALYSIS",
                       "command_id": PROBE,
                       "output_contract": {"pados_capability_outcome": "pados.capability-outcome.v1"}}]})
        out["plan_http"] = status
        plan_id = plan["task_plan"]["id"]
        status, loop = self.call("POST", "/control/loops", {
            "task_goal_id": goal_id, "active_plan_id": plan_id, "approval_id": approval["id"], "max_iterations": 4,
            "idempotency_key": key("web-loop")})
        out["loop_http"] = status
        loop_id = loop["loop"]["id"]
        out["start_http"], _ = self.call("POST", f"/control/loops/{loop_id}/start", {})
        final = None
        for _ in range(6):
            _, step = self.call("POST", f"/control/loops/{loop_id}/step", {"idempotency_key": key("web-step")})
            final = ((step or {}).get("loop") or {}).get("status") if isinstance(step, dict) else None
            if final in STOPPED:
                break
        out.update(request_id=request["id"], approval_id=approval["id"], goal_id=goal_id, plan_id=plan_id,
                   loop_id=loop_id, final_status=final, root_goal=root_goal)
        return out

    def evidence(self, loop_id: str) -> dict[str, Any]:
        """The iteration's job, its artifacts and the verified stderr report the probe wrote."""
        _, iterations = self.call("GET", f"/control/loops/{loop_id}/iterations")
        job_id = next(row["job_id"] for row in iterations["iterations"] if row.get("job_id"))
        _, artifacts = self.call("GET", f"/control/jobs/{job_id}/artifacts")
        status, stderr = self.call("GET", f"/control/jobs/{job_id}/artifacts/stderr/content")
        artifact = (stderr or {}).get("artifact", {}) if isinstance(stderr, dict) else {}
        try:
            report = json.loads(artifact.get("content") or "{}")
        except ValueError:
            report = {}
        _, events = self.call("GET", f"/control/loops/{loop_id}/events")
        return {"job_id": job_id, "artifact_types": sorted(a["artifact_type"] for a in artifacts["artifacts"]),
                "digests_ok": all(re.fullmatch(r"[0-9a-f]{64}", a["sha256"] or "") for a in artifacts["artifacts"]),
                "stderr_http": status, "integrity": artifact.get("integrity"), "report": report,
                "event_types": [e["event_type"] for e in events["events"]]}


def flow(ctx: identity.Context, browser: identity.Browser, project_name: str) -> None:
    run, product = ctx.run, Product(identity.SessionApi(browser))
    body = {"name": project_name, "description": "產品流程示範：Markdown 連結檢查。", "idempotency_key": key("web-project")}
    status, created = product.call("POST", "/control/projects", body)
    replay_status, replay = product.call("POST", "/control/projects", body)
    project = (created or {}).get("project", {}) if isinstance(created, dict) else {}
    project_id = str(project.get("id"))
    folder = Path(ctx.env["PADOS_PRODUCT_WORKSPACE_ROOT"]) / str(project.get("folder"))
    _, listing = product.call("GET", "/control/projects")
    run.record("project_created_with_name_only", status == 200 and created.get("proof_scope") == "READY"
               and replay_status == 200 and replay.get("replayed") is True
               and replay["project"]["id"] == project_id and (folder / "README.md").is_file()
               and any(p["id"] == project_id for p in listing["projects"]), http=status, project_id=project_id,
               folder=project.get("folder"), scope_version=created.get("scope_version"), replay_http=replay_status)

    docs = f"/control/projects/{project_id}/documents"
    status, written = product.call("POST", docs, {"path": "guide.md", "content": BROKEN_GUIDE,
                                                  "idempotency_key": key("web-doc")})
    traversal, refused = product.call("POST", docs, {"path": "../escape.md", "content": "x",
                                                     "idempotency_key": key("web-doc")})
    overwrite, kept = product.call("POST", docs, {"path": "guide.md", "content": FIXED_GUIDE,
                                                  "idempotency_key": key("web-doc")})
    _, listed = product.call("GET", docs)
    run.record("markdown_added_inside_the_project", status == 200 and traversal == 422
               and identity.Reply(traversal, [], json.dumps(refused)).code == "DOCUMENT_PATH_INVALID"
               and overwrite == 409 and identity.Reply(overwrite, [], json.dumps(kept)).code == "DOCUMENT_EXISTS"
               and sorted(d["path"] for d in listed["documents"]) == ["README.md", "guide.md"]
               and not (folder.parent / "escape.md").exists(), http=status, traversal_http=traversal,
               overwrite_http=overwrite, documents=[d["path"] for d in listed["documents"]])
    first_sha = written["document"]["sha256"]

    first = product.check(project_id, "檢查文件連結", "請檢查這個專案所有 Markdown 文件的連結是否有效。")
    evidence = product.evidence(first["loop_id"])
    (run.out / "first_check.json").write_text(json.dumps({**first, "evidence": evidence}, indent=2, default=str),
                                              encoding="utf-8")
    broken = evidence["report"].get("broken", [])
    run.record("plan_approved_and_run", all(first[k] == 200 for k in (
        "request_http", "approval_http", "approve_http", "goal_http", "plan_http", "loop_http", "start_http"))
        and first["approval_status"] == "APPROVED", **{k: v for k, v in first.items() if k.endswith("_http")})
    run.record("evidence_names_the_broken_link", first["final_status"] == "WAITING_HUMAN"
               and evidence["integrity"] == "VERIFIED" and evidence["digests_ok"]
               and broken == [{"file": "guide.md", "target": "missing-install.md"}], final=first["final_status"],
               broken=broken, artifact_types=evidence["artifact_types"])
    refused_http, refused_body = product.call("POST", f"/control/loops/{first['loop_id']}/result-candidate", {})
    cancel_http, cancelled = product.call("POST", f"/control/loops/{first['loop_id']}/cancel", {})
    run.record("unfinished_check_cannot_become_a_candidate", refused_http == 403
               and identity.Reply(refused_http, [], json.dumps(refused_body)).code
               == "RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP" and cancel_http == 200
               and cancelled["loop"]["status"] == "CANCELLED", candidate_http=refused_http, cancel_http=cancel_http)

    status, fixed = product.call("POST", docs, {"path": "guide.md", "content": FIXED_GUIDE, "replace": True,
                                                "expected_sha256": first_sha, "idempotency_key": key("web-doc")})
    second = product.check(project_id, "重新檢查文件連結", "修正後再檢查一次所有 Markdown 連結。")
    clean = product.evidence(second["loop_id"])
    run.record("fixed_document_check_completes", status == 200 and fixed["document"]["replaced"] is True
               and second["final_status"] == "COMPLETED" and clean["report"].get("broken") == []
               and clean["integrity"] == "VERIFIED", final=second["final_status"], report=clean["report"])
    submitted_http, submitted = product.call("POST", f"/control/loops/{second['loop_id']}/result-candidate", {})
    candidate = (submitted or {}).get("candidate", {}) if isinstance(submitted, dict) else {}
    run.record("result_candidate_waits_for_approval", submitted_http == 200
               and candidate.get("verification_status") == "PASSED"
               and candidate.get("decision_status") == "WAITING_APPROVAL", http=submitted_http,
               verification=candidate.get("verification_status"), decision=candidate.get("decision_status"))
    identity.step_up_not_bypassed(ctx, browser, candidate)

    _, work = product.call("GET", f"/control/projects/{project_id}/work")
    rows = {c["candidate_id"]: c for c in work.get("candidates", [])}
    pending = rows.get(candidate.get("candidate_id"), {})
    (run.out / "project_work_projection.json").write_text(json.dumps(work, indent=2, default=str), encoding="utf-8")
    run.record("projection_shows_pending_decision_and_audit", work.get("authority") == "PROJECTION_ONLY"
               and pending.get("approval_status") == "PENDING" and pending.get("decision") is None
               and {loop["status"] for loop in work["loops"]} == {"CANCELLED", "COMPLETED"}
               and work["step_up"]["remote_console"] == "NOT_CONFIGURED"
               and {"PROJECT_CREATED", "PROJECT_DOCUMENT_WRITTEN", "WORK_REQUEST_CAPTURED"}
               <= {e["event_type"] for e in work["events"]}, step_up=work.get("step_up"),
               candidate=pending, loops=[loop["status"] for loop in work["loops"]])


def playwright_live(ctx: identity.Context, args: argparse.Namespace) -> None:
    run = ctx.run
    if identity.port_busy(args.web_port):
        run.record("playwright_product_flow_live", False, reason=f"web port {args.web_port} is already in use")
        return
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_")}
    env.update({"PADOS_WEB_PORT": str(args.web_port), "PADOS_WEB_BACKEND_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_PRODUCT_LIVE": "1", "PADOS_E2E_PYTHON": sys.executable,
                "PADOS_E2E_API_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_TOKEN_FILE": str(ctx.root / "runtime" / "local" / "control.token"),
                "PADOS_E2E_EVIDENCE_DIR": str(evidence), "PYTHONUTF8": "1"})
    argv = ["npx", "playwright", "test", "e2e/product-flow-live.spec.ts", "--reporter=list", "--workers=1",
            "--output", str(run.out / "playwright-results")]
    result = subprocess.run(argv, cwd=REPO / "web", env=env, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=1800, shell=os.name == "nt")
    (run.out / "playwright_product_flow_live.log").write_text(
        f"$ {' '.join(argv)}\nexit={result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}",
        encoding="utf-8")
    summary = [line.strip() for line in result.stdout.splitlines() if re.search(r"\d+ (passed|failed|skipped)", line)]
    shots = sorted(p.name for p in evidence.glob("*.png"))
    run.record("playwright_product_flow_live", result.returncode == 0 and any("passed" in s for s in summary)
               and not any("failed" in s or "skipped" in s for s in summary) and bool(shots),
               exit=result.returncode, summary=summary, screenshots=shots, evidence_dir=str(evidence))


def leak_scan(ctx: identity.Context) -> None:
    tokens = ctx.secrets_seen["control"]
    bodies = [r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers) for r in ctx.http.replies]
    in_replies = sum(1 for text in bodies for value in tokens if value in text)
    files = [p for p in ctx.run.out.rglob("*") if p.is_file() and p.suffix in {".log", ".json", ".txt"}]
    in_files = sorted(p.name for p in files for value in tokens if value in p.read_text("utf-8", "replace"))
    ctx.run.record("no_control_token_leak", bool(tokens) and in_replies == 0 and not in_files,
                   replies_scanned=len(bodies), files_scanned=len(files), in_replies=in_replies, in_files=in_files)


def exercise(args: argparse.Namespace, run: dogfood.Run, root: Path, env: dict[str, str]) -> None:
    control_value = (root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    ctx = identity.Context(run, env, root, identity.Http(args.api_port), args.api_port,
                           f"http://127.0.0.1:{args.web_port}", control_value)
    ctx.remember("control", control_value)
    server = dogfood.start_server(run, env, args.api_port, "serve_product")
    try:
        browser = ctx.browser("Product flow E2E")
        paired = ctx.pair(browser, ctx.mint_code("device_pair_cli_json", "Product flow E2E"))
        run.record("browser_paired", paired.status == 200 and bool(browser.csrf), http=paired.status)
        flow(ctx, browser, f"文件檢查示範 {int(time.time())}")
        if args.playwright:
            playwright_live(ctx, args)
        else:
            identity.note(run, "playwright_product_flow_live", "SKIPPED", reason="run without --playwright")
    finally:
        dogfood.stop_server(server)
    leak_scan(ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, help="screenshot directory for --playwright (default: <out>/shots)")
    parser.add_argument("--pg-bin", type=Path, help="native PostgreSQL bin dir: start a private loopback cluster")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55480)
    parser.add_argument("--pg-user", default="pados")
    parser.add_argument("--api-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--web-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--playwright", action="store_true", help="also run web/e2e/product-flow-live.spec.ts")
    args = parser.parse_args(argv)
    args.api_port = args.api_port or identity.free_port()
    args.web_port = args.web_port or identity.free_port()
    run = dogfood.Run(args.out.resolve())
    args.evidence = args.evidence or run.out / "shots"
    root = Path(tempfile.mkdtemp(prefix="pados-product-e2e-"))
    db_pass = "" if args.pg_bin else os.environ.get("PADOS_DB_PASSWORD", "")
    cluster = dogfood.Cluster(args.pg_bin, root, args.pg_port) if args.pg_bin else None
    started = time.time()
    try:
        if cluster:
            cluster.start()
        run.record("postgres_ready", True, mode="private-cluster" if cluster else "existing-server", port=args.pg_port)
        env = identity.prepare(args, run, root, db_pass)
        env["PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS"] = "3600"  # the browser demo takes minutes; idle expiry is not tested
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
    (run.out / "PRODUCT_FLOW_E2E_RESULT.json").write_text(json.dumps({
        "runner_contract": CONTRACT, "verdict": "PASS" if passed else "FAIL", "commit": head,
        "platform": platform.platform(), "python": sys.version.split()[0], "seconds": int(time.time() - started),
        "finished": datetime.now(UTC).isoformat(), "counts": counts,
        "options": {"playwright": args.playwright, "pg_mode": "private-cluster" if cluster else "existing-server"},
        "steps": run.steps}, indent=2, default=str), encoding="utf-8")
    print(f"PRODUCT FLOW E2E {'PASS' if passed else 'FAIL'} {counts} -> {run.out / 'PRODUCT_FLOW_E2E_RESULT.json'}",
          flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
