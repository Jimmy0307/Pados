# SPDX-License-Identifier: AGPL-3.0-only
"""Layer 3 live acceptance: D1, D2 and D4 against an INSTALLED PADOS on real local hardware.

Manual / release-candidate only. Never run by CI (hosted runners have no accepted local model). Every product
action goes through the installed PADOS (its loopback API, or its own services for the two operator steps the
API does not expose); the harness only sequences calls, reads ground truth through PADOS's own database
settings, and records the result. Run it with the INSTALLED runtime's interpreter, isolated:

  <PADOS_HOME>/runtime/<version>/python/python -I live_acceptance.py prepare --home H --profile-report R \
      --expected-profile-id ID --out DIR
      Creates a disposable acceptance project, imports the ACCEPTED model capability profile into the
      installation's model catalog and writes a compute-node registration that names that profile's model for
      this project only. Run it before `pados up`. Prints the environment `pados up` needs.
  ... live_acceptance.py run --home H --out DIR --rc-manifest RC/rc-manifest.json [--only d1,d2,d4]
      Refuses unless the installed runtime is exactly the RC CI artifact named by the RC manifest (verify the
      downloaded artifact first with scripts/release/rc_manifest.py verify; live acceptance never runs against
      a developer build).
      D1: an approval bound to one subject cannot route other capabilities, and a legacy unbound approval
          cannot route at all; no job is created and every refusal is durably audited.
      D2: one real local inference through graph cognition carries measured-execution evidence whose context
          window equals what the runtime actually loaded.
      D4: a TaskLoop runs a deterministic step and then a model step to COMPLETED with no PAUSE decision.
  ... live_acceptance.py revoke --home H --out DIR
      Rewrites the registration with an empty model allowlist (revocation), so the dogfood grant does not linger.

Results go to DIR/live-acceptance-<gate>.json with the installed runtime identity. Secrets are never printed:
the database password comes from the installation's SecretRef, the control token from its owner-only file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SUBJECT_SCHEMA = "pados.approval-subject.v1"
NODE_CREDENTIAL_REF = "secretref://env/PADOS_LIVE_ACCEPTANCE_NODE_CREDENTIAL"
OPS = [{"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "python_version",
        "max_risk_level": "LOW"},
       {"step_sequence": 2, "capability_id": "SUMMARIZE", "command_id": None, "max_risk_level": "LOW"}]
TERMINAL_LOOP = {"COMPLETED", "PAUSED", "BLOCKED", "WAITING_HUMAN", "FAILED_TERMINAL", "FAILED_RECOVERABLE"}


class Harness:
    def __init__(self, home: Path, out: Path) -> None:
        from pados.install.envfile import bootstrap

        self.home, self.out = home, out
        out.mkdir(parents=True, exist_ok=True)
        bootstrap(home)
        from pados.config import get_settings

        self.settings = get_settings()
        self.base = f"http://{self.settings.api_host}:{self.settings.api_port}"
        self.trace: list[dict[str, Any]] = []
        self.state_file = out / "live-acceptance-state.json"

    # --- recording -------------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}

    def save_state(self, values: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps({**self.state(), **values}, indent=1), encoding="utf-8")

    def runtime_identity(self) -> dict[str, Any]:
        import pados

        app = Path(pados.__file__).resolve().parents[2]
        manifest = app / "runtime-manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        return {"runtime_app": str(app), "runtime_manifest_sha256": _sha(manifest),
                "source": runtime_source_identity(data)}

    def dump(self, gate: str, result: dict[str, Any]) -> bool:
        record = {"gate": gate, **result, "runtime": self.runtime_identity(), "trace": self.trace}
        (self.out / f"live-acceptance-{gate}.json").write_text(
            json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        verdict = result.get("status")
        print(f"LIVE_ACCEPTANCE {gate} {verdict}", flush=True)
        self.trace = []
        return verdict == "PASS"

    # --- product calls ---------------------------------------------------------------------------------------
    def token(self) -> str:
        from pados.install.home import config_file

        token_file: Path = config_file(self.home).parent / "control.token"
        return token_file.read_text(encoding="utf-8").strip()

    def api(self, method: str, path: str, body: dict[str, Any] | None = None,
            timeout: float = 900) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Content-Type": "application/json", "X-PADOS-Control-Token": self.token()})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            payload: Any = json.loads(raw or b"null")
        except ValueError:
            payload = raw.decode("utf-8", "replace")[:2000]
        self.trace.append({"call": f"{method} {path}", "status": status,
                           "elapsed_s": round(time.perf_counter() - started, 3), "response": payload})
        return status, payload

    def sql(self, query: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
        from sqlalchemy import text

        from pados.db import session_scope

        with session_scope(self.settings) as session:
            return [tuple(row) for row in session.execute(text(query), params or {}).all()]

    def request_and_approval(self, project: str, tag: str, surfaces: list[str]) -> tuple[str, str]:
        _, work = self.api("POST", "/control/work-requests", {
            "project_id": project, "title": f"Live acceptance {tag}", "body": f"Live acceptance {tag}",
            "idempotency_key": f"{tag}-wr"})
        request = work["work_request"]["id"]
        _, created = self.api("POST", "/control/approvals", {
            "project_id": project, "idempotency_key": f"{tag}-ap",
            "proposal_summary": f"Live acceptance {tag}: python_version then SUMMARIZE",
            "subject": {"kind": "WORK_ROUTE", "schema_version": SUBJECT_SCHEMA, "project_id": project,
                        "request_id": request,
                        "actor": {"surfaces": surfaces, "worker_types": ["DETERMINISTIC_TOOL", "OLLAMA"]},
                        "operations": OPS, "scope_version_ref": None, "task_envelope_hash": None}})
        approval = created["approval"]
        self.api("POST", f"/control/approvals/{approval['id']}/approve",
                 {"version": approval["version"], "idempotency_key": f"{tag}-ok"})
        return request, approval["id"]


def runtime_source_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """Identity of an installed runtime from its runtime-manifest.json (commit and dirty live under "source")."""

    raw_source = manifest.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    return {"package": manifest.get("package"), "version": manifest.get("package_version"),
            "platform": manifest.get("platform"), "commit": source.get("commit"), "dirty": source.get("dirty")}


def chat_environment(artifact_ref: str, catalog: Path, registry: Path) -> dict[str, str]:
    """Environment `pados up` needs. The measured binding matches PADOS_CHAT_MODEL against the profile's
    artifact.artifact_ref (the runtime tag, e.g. qwen3:8b), never against the catalog model_id."""

    return {"PADOS_CHAT_PROVIDER": "ollama", "PADOS_CHAT_MODEL": artifact_ref,
            "PADOS_MODEL_CATALOG": str(catalog), "PADOS_COMPUTE_NODES_CONFIG": str(registry)}


def find_measured_execution(body: Any) -> dict[str, Any] | None:
    """The measured-execution evidence of a graph cognitive response.

    The API returns it under work_item.candidate_outputs[].measured_execution and under
    candidate_nodes[].metadata_json.measured_execution; both are read, the first found wins.
    """

    if not isinstance(body, dict):
        return None
    raw_item = body.get("work_item")
    work_item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else {}
    candidates: list[Any] = list(work_item.get("candidate_outputs") or [])
    candidates += [(node or {}).get("metadata_json") for node in body.get("candidate_nodes") or []
                   if isinstance(node, dict)]
    for candidate in candidates:
        evidence = candidate.get("measured_execution") if isinstance(candidate, dict) else None
        if isinstance(evidence, dict):
            return evidence
    return None


def rc_identity_check(runtime: dict[str, Any], rc_manifest: dict[str, Any]) -> list[str]:
    """Problems that make an installed runtime unacceptable as the RC artifact named by ``rc_manifest``."""

    problems: list[str] = []
    if rc_manifest.get("schema") != "pados.rc-manifest.v1":
        problems.append("RC_MANIFEST_INVALID")
    if runtime.get("commit") != rc_manifest.get("RC_COMMIT"):
        problems.append("RUNTIME_NOT_RC_COMMIT")
    if runtime.get("dirty") is not False:
        problems.append("RUNTIME_DIRTY_OR_UNKNOWN")
    platform = (rc_manifest.get("platforms") or {}).get(runtime.get("platform") or "")
    names = [item.get("file", "") for item in (platform or {}).get("packages", [])]
    if not any(f"-{runtime.get('version')}.tgz" in name and "runtime" in name for name in names):
        problems.append("RUNTIME_PACKAGE_NOT_IN_RC")
    return problems


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _catalog_path(harness: Harness) -> Path:
    return Path(harness.settings.model_catalog_path or harness.home / "state" / "model-catalog.json")


def _registry_path(harness: Harness) -> Path:
    return Path(harness.settings.compute_nodes_config or harness.home / "config" / "compute-nodes.json")


def _write_registry(harness: Harness, project: str, models: tuple[str, ...]) -> dict[str, Any]:
    from pados.compute_node.manifest import NodeCapability
    from pados.compute_node.registry import NodeRegistration, NodeRegistry
    from pados.enterprise.classification import DataClassification

    node_id = "lcn-" + hashlib.sha256(f"pados-live-acceptance:{harness.home}".encode()).hexdigest()[:20]
    registration = NodeRegistration(
        node_id=node_id, node_name="live-acceptance-local-runtime", endpoint=harness.settings.ollama_base_url,
        auth_ref=NODE_CREDENTIAL_REF, authorized=True, allowed_capabilities=(NodeCapability.LOCAL_INFERENCE,),
        max_classification=DataClassification.INTERNAL, project_ids=(project,), allowed_models=models,
        security_profile="LOCAL_SINGLE_USER")
    path = _registry_path(harness)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "pados.compute-nodes.registry.v1",
                                "state_dir": str(harness.home / "state" / "nodes"),
                                "nodes": [registration.model_dump(mode="json")]}, indent=2), encoding="utf-8")
    loaded = NodeRegistry.load(path)
    return {"registry": str(path), "registry_sha256": _sha(path), "project_ids": [project],
            "allowed_models": list(loaded.nodes[0].allowed_models)}


def prepare(harness: Harness, report: Path, expected_profile_id: str) -> int:
    from pados.db import session_scope
    from pados.local_intelligence.catalog_store import ModelCatalogStore
    from pados.memory import service as memory_service
    from pados.resources.capability_profile import ModelCapabilityProfile
    from pados.resources.models import ModelCatalog, ModelRuntimeProfile

    document = json.loads(report.read_text(encoding="utf-8"))
    profile = ModelCapabilityProfile.model_validate(document["profile"])
    description = ModelRuntimeProfile.model_validate(document["catalog_description"])
    if profile.profile_id != expected_profile_id:
        print("LIVE_ACCEPTANCE prepare FAIL PROFILE_ID_UNEXPECTED")
        return 1
    root = harness.home / "data" / "workspace" / "live-acceptance"
    root.mkdir(parents=True, exist_ok=True)
    project = harness.state().get("project_id")
    if not project:
        with session_scope(harness.settings) as session:
            project = memory_service.bootstrap_project(session, name=f"live-acceptance-{int(time.time())}",
                                                       root_path=root)
    store = ModelCatalogStore(_catalog_path(harness))
    digest = store.save(ModelCatalog([description]).register_capability_profile(profile))
    reloaded = store.load().capability_profile(description.model_id)
    if reloaded is None or reloaded.model_dump(mode="json") != profile.model_dump(mode="json"):
        print("LIVE_ACCEPTANCE prepare FAIL PERSISTENCE_CHANGED_IDENTITY")
        return 1
    registry = _write_registry(harness, str(project), (profile.artifact.model_ref,))
    runtime_tag = profile.artifact.artifact_ref
    harness.save_state({"project_id": str(project), "profile_id": profile.profile_id,
                        "model_ref": profile.artifact.model_ref, "model_id": runtime_tag,
                        "catalog_model_id": description.model_id})
    environment = chat_environment(runtime_tag, store.path, Path(registry["registry"]))
    harness.dump("prepare", {"status": "PASS", "project_id": str(project), "profile_id": profile.profile_id,
                             "report_sha256": _sha(report), "catalog_digest": digest, **registry,
                             "environment_for_pados_up": environment,
                             "node_credential_env": NODE_CREDENTIAL_REF.rsplit("/", 1)[1]})
    print(json.dumps(environment))
    return 0


def gate_d1(harness: Harness, project: str) -> bool:
    tag = f"live-d1-{int(time.time())}"
    request, approval = harness.request_and_approval(project, tag, ["CONTROL_API", "TASK_LOOP"])
    results: dict[str, Any] = {}
    for capability in ("BUILD_RELEASE", "MODIFY_CODE"):
        status, body = harness.api("POST", f"/control/work-requests/{request}/route", {
            "approval_id": approval, "capability_id": capability, "context_query": "exploit replay",
            "idempotency_key": f"{tag}-{capability}"})
        results[capability] = {"status": status, "detail": body.get("detail") if isinstance(body, dict) else body}
    summary = f"legacy unbound approval {tag}"
    _, legacy = harness.api("POST", "/control/approvals", {
        "project_id": project, "proposal_summary": summary,
        "proposal_hash": hashlib.sha256(summary.encode()).hexdigest(), "idempotency_key": f"{tag}-legacy"})
    unbound = legacy["approval"]
    harness.api("POST", f"/control/approvals/{unbound['id']}/approve",
                {"version": unbound["version"], "idempotency_key": f"{tag}-legacy-ok"})
    status, body = harness.api("POST", f"/control/work-requests/{request}/route", {
        "approval_id": unbound["id"], "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "python_version",
        "context_query": "legacy", "idempotency_key": f"{tag}-legacy-route"})
    results["LEGACY_UNBOUND"] = {"status": status, "detail": body.get("detail") if isinstance(body, dict) else body}
    jobs = harness.sql("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:r AS uuid)", {"r": request})[0][0]
    refusals = harness.sql(
        "SELECT payload_json->>'code', payload_json->>'capability_id' FROM control_sync_events "
        "WHERE event_type = 'ROUTE_REFUSED' AND payload_json->>'request_id' = :r ORDER BY sequence_number",
        {"r": request})
    passed = (all(results[c]["status"] == 403 and results[c]["detail"] == "APPROVAL_SCOPE_MISMATCH"
                  for c in ("BUILD_RELEASE", "MODIFY_CODE"))
              and results["LEGACY_UNBOUND"]["detail"] == "APPROVAL_SUBJECT_UNBOUND" and jobs == 0
              and len(refusals) == 3)
    return harness.dump("d1", {"status": "PASS" if passed else "FAIL", "request_id": request,
                               "approval_id": approval, "results": results, "worker_jobs_for_request": jobs,
                               "route_refused_events": refusals})


def gate_d2(harness: Harness, project: str, model_id: str) -> bool:
    _, created = harness.api("POST", "/api/graph/workspaces", {
        "name": f"Live acceptance D2 {int(time.time())}", "description": "live acceptance", "project_id": project})
    workspace = created["workspace"]["id"]
    _, node = harness.api("POST", f"/api/graph/workspaces/{workspace}/nodes", {
        "semantic_type": "IDEA", "title": "What does a measured execution binding guarantee?",
        "content": "In two sentences, explain why a local model run should record the exact configuration it used."})
    status, body = harness.api("POST", f"/api/graph/workspaces/{workspace}/selection/cognitive",
                               {"node_ids": [node["node"]["id"]], "operation": "THINK"})
    base = harness.settings.ollama_base_url.rstrip("/")
    with urllib.request.urlopen(f"{base}/api/ps", timeout=10) as response:  # noqa: S310 - loopback runtime
        loaded = [{key: model.get(key) for key in ("name", "context_length", "size", "size_vram", "digest")}
                  for model in json.loads(response.read()).get("models", [])]
    evidence = find_measured_execution(body)
    loaded_context = next((m["context_length"] for m in loaded if m["name"] == model_id), None)
    passed = (status in {200, 201} and evidence is not None and loaded_context is not None
              and (evidence.get("execution") or {}).get("context_window") == loaded_context)
    return harness.dump("d2", {"status": "PASS" if passed else "FAIL", "http_status": status,
                               "workspace_id": workspace, "measured_execution": evidence, "runtime_loaded": loaded,
                               "detail": body.get("detail") if isinstance(body, dict) else None})


def gate_d4(harness: Harness, project: str) -> bool:
    from pados.db import session_scope
    from pados.loop import service as loop_service
    from pados.loop.schemas import TaskGoalCreate, TaskPlanCreate

    tag = f"live-d4-{int(time.time())}"
    request, approval = harness.request_and_approval(project, tag, ["TASK_LOOP"])
    with session_scope(harness.settings) as session:  # the operator CLI's own service calls (loop create-goal/plan)
        goal = loop_service.create_task_goal(session, TaskGoalCreate(
            project_id=project, request_id=request, objective="Live acceptance: deterministic then model step",
            success_criteria=["python_version passes", "summary produced"], approval_id=approval,
            idempotency_key=f"{tag}-goal"))["id"]
    with session_scope(harness.settings) as session:
        plan = loop_service.create_task_plan(session, TaskPlanCreate.model_validate({
            "task_goal_id": str(goal), "approval_id": approval, "idempotency_key": f"{tag}-plan",
            "steps": [{"sequence": 1, "title": "Python version", "step_type": "RUN_VALIDATION",
                       "required_capability": "RUN_STATIC_ANALYSIS", "command_id": "python_version"},
                      {"sequence": 2, "title": "Summarize the context in three sentences",
                       "description": "Summarize what PADOS is, using only the provided context.",
                       "step_type": "ANALYZE", "required_capability": "SUMMARIZE",
                       "preferred_worker_types": ["OLLAMA"], "dependencies": [1]}]}),
            settings=harness.settings)["id"]
    _, created = harness.api("POST", "/control/loops", {"task_goal_id": str(goal), "active_plan_id": str(plan),
                                                         "approval_id": approval, "max_iterations": 6,
                                                         "idempotency_key": f"{tag}-loop"})
    loop = created["loop"]["id"]
    harness.api("POST", f"/control/loops/{loop}/start", {})
    decisions: list[Any] = []
    for index in range(6):
        _, step = harness.api("POST", f"/control/loops/{loop}/step", {"idempotency_key": f"{tag}-step{index}"})
        decisions.append({k: step.get(k) for k in ("status", "decision", "job_status")}
                         if isinstance(step, dict) else step)
        if isinstance(step, dict) and (step.get("loop") or {}).get("status") in TERMINAL_LOOP:
            break
    jobs = harness.sql("SELECT capability_id, worker_id, status, started_at IS NOT NULL, error_code "
                       "FROM worker_jobs WHERE request_id = CAST(:r AS uuid) ORDER BY queued_at", {"r": request})
    final = harness.sql("SELECT status FROM task_loops WHERE id = CAST(:l AS uuid)", {"l": loop})[0][0]
    model_jobs = [job for job in jobs if job[0] == "SUMMARIZE"]
    passed = (len(jobs) == 2 and jobs[0][2] == "SUCCEEDED" and bool(model_jobs) and model_jobs[0][2] == "SUCCEEDED"
              and final == "COMPLETED"
              and "PAUSE" not in [d.get("decision") for d in decisions if isinstance(d, dict)])
    return harness.dump("d4", {"status": "PASS" if passed else "FAIL", "loop_id": loop, "final_loop_status": final,
                               "decisions": decisions, "jobs": [list(job) for job in jobs]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "run", "revoke"))
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--profile-report", type=Path)
    parser.add_argument("--expected-profile-id")
    parser.add_argument("--only", default="d1,d2,d4")
    parser.add_argument("--rc-manifest", type=Path, help="rc-manifest.json of the RC artifact that was installed")
    args = parser.parse_args(argv)
    harness = Harness(args.home, args.out)
    if args.action == "prepare":
        if not args.profile_report or not args.expected_profile_id:
            parser.error("prepare needs --profile-report and --expected-profile-id")
        return prepare(harness, args.profile_report, args.expected_profile_id)
    state = harness.state()
    if not state.get("project_id"):
        print("LIVE_ACCEPTANCE FAIL NOT_PREPARED")
        return 1
    if args.action == "revoke":
        harness.dump("revoke", {"status": "PASS", **_write_registry(harness, state["project_id"], ())})
        return 0
    if args.rc_manifest is None:
        parser.error("run needs --rc-manifest (live acceptance consumes RC CI artifacts only)")
    rc_manifest = json.loads(args.rc_manifest.read_text(encoding="utf-8"))
    problems = rc_identity_check(harness.runtime_identity()["source"], rc_manifest)
    if problems:
        print(f"LIVE_ACCEPTANCE FAIL NOT_THE_RC_ARTIFACT {','.join(problems)}")
        return 1
    harness.save_state({"rc_commit": rc_manifest["RC_COMMIT"], "rc_tree": rc_manifest["RC_TREE"]})
    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    outcomes = {}
    if "d1" in wanted:
        outcomes["d1"] = gate_d1(harness, state["project_id"])
    if "d2" in wanted:
        outcomes["d2"] = gate_d2(harness, state["project_id"], state["model_id"])
    if "d4" in wanted:
        outcomes["d4"] = gate_d4(harness, state["project_id"])
    verdict = "PASS" if outcomes and all(outcomes.values()) else "FAIL"
    (args.out / "live-acceptance-summary.json").write_text(json.dumps(
        {"status": verdict, "gates": {k: "PASS" if v else "FAIL" for k, v in outcomes.items()},
         "rc": {"RC_COMMIT": rc_manifest["RC_COMMIT"], "RC_TREE": rc_manifest["RC_TREE"]},
         "runtime": harness.runtime_identity()}, indent=1), encoding="utf-8")
    print(f"LIVE_ACCEPTANCE {verdict} {outcomes}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
