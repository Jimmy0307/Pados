"""PADOS public demo scenario (synthetic, public-safe; no external commercial APIs).

Research notes -> ingest -> classify -> retrieve -> bounded evidence -> local
compute node execution -> ExecutionReceipt -> user validation (approval) ->
ResultReceipt -> CAS -> audit -> Mission Control.

``--local-ai`` uses a local Ollama through the node's Ollama backend; the model
is chosen from discovery metadata, never hard-coded.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from pados.compute_node.maker import LocalComputeNodeMaker
from pados.compute_node.provider import LocalInferenceProvider
from pados.compute_node.receipts import ReceiptLedger, verify_node_receipt
from pados.compute_node.registry import NodeRegistration, NodeRegistry
from pados.config import get_settings, lane_settings
from pados.control import service as control_service
from pados.db import session_scope
from pados.enterprise.authorization import AccessContext, authorize_capability
from pados.enterprise.classification import DataClassification
from pados.enterprise.context import EnterpriseEvidenceContext
from pados.enterprise.manifest import EnterpriseCapabilityManifest
from pados.enterprise.pipeline import local_pipeline
from pados.enterprise.registry import CapabilityRegistry
from pados.operations.service import mission_control
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.state_proof import ObjectStore, StateProofService
from pados.state_proof.service import build_proof_engine

HERE = Path(__file__).resolve().parent
TENANT = "demo-tenant"
TARGET = "briefs/MAINTENANCE_BRIEF.md"
AUTH_REF = "secretref://env/PADOS_DEMO_NODE_CREDENTIAL"
STEPS: list[dict[str, Any]] = []
# Reasoning models may spend a small budget before answering; an empty answer fails
# closed with EMPTY_COMPLETION. Both limits are configurable, never model-specific.
MAX_TOKENS = int(os.environ.get("PADOS_DEMO_MAX_TOKENS", "256"))
TIMEOUT = float(os.environ.get("PADOS_DEMO_TIMEOUT_SECONDS", "240"))


def step(name: str, ok: bool, **evidence: Any) -> None:
    STEPS.append({"step": len(STEPS) + 1, "name": name, "ok": bool(ok), **evidence})
    print(f"[{len(STEPS):02d}] {'OK ' if ok else 'ERR'} {name}", file=sys.stderr)


def discover_model(endpoint: str) -> dict[str, Any] | None:
    """Pick the smallest installed model; metadata comes from the backend."""
    try:
        tags = httpx.get(f"{endpoint}/api/tags", timeout=3).json()["models"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    if not tags:
        return None
    model = min(tags, key=lambda m: int(m.get("size") or 1 << 62))
    details = model.get("details") or {}
    return {"backend_id": "ollama", "backend_model": model["name"], "family": details.get("family") or "unknown",
            "version": str(model["digest"])[:12], "digest": str(model["digest"])[:12],
            "quantization": details.get("quantization_level"), "context_limit_tokens": 4096,
            "tasks": ["LOCAL_INFERENCE"], "resource_class": "GPU", "privacy_class": "CONFIDENTIAL"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-ai", action="store_true")
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    settings = get_settings()
    root = Path(args.root).resolve()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["PADOS_DEMO_NODE_CREDENTIAL"] = secrets.token_urlsafe(32)

    backends: list[dict[str, Any]] = [{"backend_id": "deterministic", "kind": "deterministic"}]
    models: list[dict[str, Any]] = [{"backend_id": "deterministic", "backend_model": "pados-deterministic-v1",
                                     "family": "pados-deterministic", "version": "1", "context_limit_tokens": 8192,
                                     "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT",
                                     "privacy_class": "CONFIDENTIAL"}]
    discovered = None
    if args.local_ai:
        endpoint = os.environ.get("PADOS_DEMO_OLLAMA", "http://127.0.0.1:11434")
        discovered = discover_model(endpoint)
        if discovered is None:
            step("local AI requested: Ollama model discovered", False, endpoint=endpoint)
            return _finish(args.out, discovered)
        backends = [{"backend_id": "ollama", "kind": "ollama", "endpoint": endpoint}]
        models = [discovered]
    node_config = {"node_name": "demo-node", "bind_host": "127.0.0.1", "port": 8799, "state_dir": str(root / "node"),
                   "credential_ref": AUTH_REF, "privacy_class": "CONFIDENTIAL", "capabilities": ["LOCAL_INFERENCE"],
                   "limits": {"default_timeout_seconds": TIMEOUT, "max_timeout_seconds": TIMEOUT,
                              "max_output_tokens": MAX_TOKENS},
                   "backends": backends, "models": models}
    (root / "node.json").write_text(json.dumps(node_config))
    node = subprocess.Popen(["pados", "node", "serve", "--config", str(root / "node.json")],
                            stdout=subprocess.PIPE, stderr=(root / "node.log").open("w"), text=True)
    try:
        node_id = json.loads(node.stdout.readline())["node_id"] if node.stdout else ""
        for _ in range(100):
            try:
                httpx.get("http://127.0.0.1:8799/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        return _demo(settings, root, workspace, node_id, discovered, args.out)
    finally:
        node.terminate()
        node.wait(timeout=10)


def _demo(settings: Any, root: Path, workspace: Path, node_id: str, discovered: dict[str, Any] | None,
          out: str) -> int:
    created = subprocess.run(["pados", "project", "create", "--name", f"Public Demo {uuid4().hex[:6]}",
                              "--root-path", str(workspace), "--json"], capture_output=True, text=True, check=False)
    project_id = str(json.JSONDecoder().raw_decode(created.stdout[created.stdout.index("{"):])[0]["project_id"])
    proof = StateProofService(build_proof_engine(lane_settings(settings, settings.product_database_name).database_url),
                              ObjectStore(Path(settings.product_object_root)))
    before = int(proof.ensure_scope(project_id)["current_version"])
    step("project created", bool(project_id), project_id=project_id, scope_version=before)

    pipeline = local_pipeline()
    manifest = EnterpriseCapabilityManifest.model_validate(
        json.loads((HERE.parent / "config" / "enterprise-synthetic.json").read_text(encoding="utf-8"))["capabilities"][0])
    registry = CapabilityRegistry()
    registry.register(manifest, pipeline)
    sources = {"pump-maintenance-study.md": (DataClassification.INTERNAL, ()),
               "project-charter.md": (DataClassification.PUBLIC, ()),
               "restricted-budget-note.md": (DataClassification.RESTRICTED, ("budget-owner",))}
    for name, (classification, principals) in sources.items():
        pipeline.ingest((HERE / "sources" / name).read_bytes(), media_type="text/markdown", source_type="demo-note",
                        location_ref=f"demo://{name}", classification=classification, tenant_id=TENANT,
                        project_id=project_id, allowed_principals=principals)
    step("ingest + classify synthetic research notes", len(pipeline.exact.records()) >= 3,
         classifications={k: v[0].value for k, v in sources.items()})
    who = AccessContext(principal_id="demo-user", session_id="demo", tenant_id=TENANT, project_id=project_id,
                        clearance=DataClassification.CONFIDENTIAL)
    authorization = authorize_capability(
        registry, manifest.capability_id, access=who,
        contract={"work_contract_id": "demo", "project_id": project_id, "required_capabilities": []},
        grant={"work_contract_id": "demo", "project_id": project_id, "capabilities": [manifest.grant_capability]})
    evidence = EnterpriseEvidenceContext(pipeline=pipeline, access=who, authorization=authorization,
                                         query="coolant pump inspection seals budget")
    preview = evidence.resolve(scope_id=project_id, goal="maintenance brief")
    step("retrieve bounded, authorized evidence (restricted note excluded by clearance)",
         bool(preview["package"]["evidence"]) and "RESTRICTED" not in preview["summary"]["classifications"],
         evidence=len(preview["package"]["evidence"]), classifications=preview["summary"]["classifications"],
         ceiling=preview["summary"]["authorization"]["ceiling"])

    nodes = NodeRegistry(state_dir=root / "pados-node-state", nodes=(NodeRegistration(
        node_id=node_id, node_name="demo-node", endpoint="http://127.0.0.1:8799", auth_ref=AUTH_REF, authorized=True,
        allowed_capabilities=("LOCAL_INFERENCE",), max_classification=DataClassification.CONFIDENTIAL,
        project_ids=(project_id,), security_profile="LOCAL_SINGLE_USER"),))
    probe = LocalInferenceProvider(nodes).prober.probe(nodes.nodes[0])
    step("register + probe local compute node (evidence-derived state)", probe["state"] == "ONLINE",
         node_id=node_id, state=probe["state"], backend=(discovered or {}).get("backend_id", "deterministic"),
         model=(discovered or {}).get("backend_model", "pados-deterministic-v1"))

    with session_scope(settings) as session:
        key = f"demo-{uuid4()}"
        row = control_service.create_approval(session, {
            "project_id": project_id, "proposal_hash": hashlib.sha256(b"maintenance brief").hexdigest(),
            "proposal_summary": "Demo user approves generating the maintenance brief", "idempotency_key": key})
        approval_id = str(control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED",
                                                          reason="demo user validation", idempotency_key=f"d-{key}")["id"])
    step("user validation: governed approval recorded before dispatch", bool(approval_id), approval_id=approval_id)

    criteria: list[dict[str, Any]] = [{"criterion_id": "exists", "check": "FILE_EXISTS", "target_path": TARGET},
                                      {"criterion_id": "scoped", "check": "CHANGED_PATHS_SCOPED"}]
    if discovered is None:
        criteria.append({"criterion_id": "deterministic", "check": "FILE_CONTAINS", "target_path": TARGET,
                         "expected_text": "Deterministic local completion"})
    loop = ProductLoopService(
        maker=LocalComputeNodeMaker(workspace, LocalInferenceProvider(nodes), timeout_seconds=TIMEOUT,
                                    max_output_tokens=MAX_TOKENS),
        validator=FileValidator(), approved_workspace_root=workspace, context_extensions=(evidence,),
        state_gateway=P6_19BStateGateway(proof, build_proof_engine(
            lane_settings(settings, settings.operational_database_name).database_url)))
    report = asyncio.run(loop.execute(GoalRequest.model_validate({
        "goal": "Write a short maintenance brief from the evidence", "workspace_root": str(workspace),
        "scope_id": project_id, "changes": [{"path": TARGET, "content": "Summarise the maintenance evidence in 3 bullets."}],
        "acceptance_criteria": criteria, "authorized": True, "approval_ref": approval_id,
        "idempotency_key": f"demo-run-{uuid4()}"})))
    receipt = report.final_result.execution_receipt if report.final_result else None
    node_receipt = ReceiptLedger(nodes.state_dir).latest(node_id)
    step("local execution -> artifact -> ExecutionReceipt + node receipt verified",
         bool(receipt) and node_receipt is not None and verify_node_receipt(node_receipt)
         and node_receipt["execution_receipt_id"] == receipt["receipt_id"],
         provider_id=(receipt or {}).get("provider_id"), evidence_kind=(receipt or {}).get("evidence_kind"),
         artifact_sha256=(node_receipt or {}).get("artifacts", [{}])[0].get("sha256"))
    after = int(proof.ensure_scope(project_id)["current_version"])
    step("validation -> ResultReceipt -> CAS", report.status == ProductLoopStatus.COMMITTED and after == before + 1,
         status=report.status.value, scope_version=after)
    step("immutable audit verified", proof.verify_semantic_audit_chain(project_id))
    brief = (workspace / TARGET).read_text(encoding="utf-8") if (workspace / TARGET).exists() else ""
    step("restricted note never reached the model or the artifact", "aurora-kestrel" not in brief and bool(brief.strip()),
         artifact_bytes=len(brief.encode()))
    registry_file = root / "registry.json"
    registry_file.write_text(nodes.model_dump_json())
    with session_scope(settings) as session:
        view = mission_control(session, settings.model_copy(update={"compute_nodes_config": str(registry_file)}))
    project = next((p for p in view["projects"] if p["project_id"] == project_id), {})
    node_view = view["compute_nodes"]["nodes"][0] if view["compute_nodes"]["nodes"] else {}
    step("Mission Control shows the accepted result and the node's last execution",
         any(r.get("run_id") == report.run_id for r in project.get("recent_accepted_results", []))
         and (node_view.get("last_execution") or {}).get("run_id") == report.run_id,
         node_state=node_view.get("state"), system_health=view["system_health"]["state"])
    return _finish(out, discovered)


def _finish(out: str, discovered: dict[str, Any] | None) -> int:
    failed = [s["step"] for s in STEPS if not s["ok"]]
    Path(out).write_text(json.dumps({"generated_at": datetime.now(UTC).isoformat(), "synthetic_data_only": True,
                                     "mode": "local-ai" if discovered else "deterministic",
                                     "model_selected_by_discovery": {k: discovered[k] for k in ("backend_model", "family", "quantization")}
                                     if discovered else None, "steps": STEPS}, indent=2, sort_keys=True))
    print(json.dumps({"failed_steps": failed, "passed": len(STEPS) - len(failed), "total": len(STEPS)}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
