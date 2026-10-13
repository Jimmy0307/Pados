# SPDX-License-Identifier: AGPL-3.0-only
"""Stage B M4.1 operator run: real-task dogfood + benchmark-integrity probe on the reference node.

Run from a checkout of the Stage B branch (NOT C:\\PADOS) with the checkout's own virtual environment, with the
runtime already serving the M2.1-verified artifact (this script never pulls, downloads or installs a model):

    .venv\\Scripts\\python.exe scripts\\stage_b\\windows_m4_1_dogfood.py --expected-commit <sha> --expected-tree <sha>

What it does, in order (every step is recorded; any refusal stops the dependent steps and is reported):

1. Verifies the checkout identity, then persists the M3 profile (from the tracked M3 report) into a fresh
   ModelCatalog store and reloads it (profile id, manifest, evidence and binding must be unchanged).
2. Observes the LIVE device (DeviceProfiler) and runtime (runtime discovery) and binds the profile.
3. Authorization boundary, live: model not named -> not routable; explicit temporary dogfood registration -> the
   task runs through planner -> Worker Router -> executor -> gated LocalInferenceProvider -> node -> runtime;
   revoked after planning -> execution refused; revoked before selection -> not routable.
4. Integrity probe (benchmark 1.1.0 instrumentation): server effective configuration vs client environment,
   process tree and GPU attribution during inference, a TOTAL-deadline timeout with recovery check, and
   concurrency semantics at level 2.
5. Writes ``M4_1_DOGFOOD_RESULT.json``, raw JSONL, SHA256SUMS and one zip archive (no weights, credentials,
   hostname or user names; endpoints are scheme://host:port only).

``--rehearsal`` (Linux, development only) replaces the runtime and host with the deterministic test fakes. A
rehearsal archive is labelled REHEARSAL and is never dogfood evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
# M3.1: the same verified M2 evidence, now with the measured execution conditions recorded (M4.2 contract fix).
M3_REPORT = ROOT / "runtime/artifacts/reports/stage-b-local-intelligence-v1/m4_2/m3_1/M3_MODEL_CAPABILITY_PROFILE.json"
EXPECTED_PROFILE_ID = "mcp-bd7794d49caed931f7063d8e021f97bd2e1b2cc6c10c09f46f1e04b5906ccadb"
PROTECTED = Path(r"C:\PADOS")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,  # noqa: S603,S607
                          text=True, check=False).stdout.strip()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--runtime-url", default="http://127.0.0.1:11434")
    parser.add_argument("--out", default=None)
    parser.add_argument("--skip-integrity", action="store_true")
    parser.add_argument("--rehearsal", action="store_true", help="Linux development rehearsal with test fakes")
    args = parser.parse_args(argv)

    if str(ROOT).lower().startswith(str(PROTECTED).lower()):
        print("REFUSED: run from a Stage B checkout, not C:\\PADOS")
        return 2
    head, tree = _git("rev-parse", "HEAD"), _git("rev-parse", "HEAD^{tree}")
    if head != args.expected_commit or tree != args.expected_tree:
        print(f"REFUSED: checkout is {head}/{tree}, expected {args.expected_commit}/{args.expected_tree}")
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = "rehearsal" if args.rehearsal else "live"
    out = Path(args.out) if args.out else ROOT / "runtime/artifacts/reports/stage-b-local-intelligence-v1/windows" / \
        f"m4_1-{label}-{stamp}"
    out.mkdir(parents=True, exist_ok=False)
    raw = out / "M4_1_RAW.jsonl"
    result: dict[str, Any] = {"schema": "pados.stage-b.m4-1.dogfood-result.v2", "mode": label.upper(),
                              "evidence_class": "REHEARSAL_NOT_EVIDENCE" if args.rehearsal else "MACHINE_CAPTURED",
                              "commit": head, "tree": tree, "started_at": _now(), "steps": {}}

    def record(entry: dict[str, Any]) -> None:
        with raw.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now(), **entry}, ensure_ascii=False, default=str) + "\n")

    try:
        run(args, out, result, record)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never hidden
        result["error"] = f"{type(exc).__name__}:{str(exc)[:200]}"
        record({"record_type": "fatal", "error": result["error"]})
    result["finished_at"] = _now()
    (out / "M4_1_DOGFOOD_RESULT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str),
                                                  encoding="utf-8")
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text("".join(f"{_sha(p)}  {p.relative_to(out).as_posix()}\n" for p in files),
                                    encoding="utf-8")
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            bundle.write(path, f"{out.name}/{path.relative_to(out).as_posix()}")
    print(json.dumps({"archive": str(archive), "archive_sha256": _sha(archive),
                      "REAL_TASK_DOGFOOD": result.get("REAL_TASK_DOGFOOD"), "error": result.get("error")}, indent=2))
    return 0 if result.get("REAL_TASK_DOGFOOD") == "PASS" else 1


def run(args: argparse.Namespace, out: Path, result: dict[str, Any], record: Callable[[dict[str, Any]], None]) -> None:
    from m4_1_live_chain import CAPABILITY, PROJECT, REQUIREMENT, build_chain, validate_extraction

    from pados.capability.executor import ExecutionRefused, ExecutionStatus
    from pados.capability.inference import measured_local_workers, worker_id_for
    from pados.worker.schemas import CapabilityId
    from pados.local_intelligence.catalog_store import ModelCatalogStore
    from pados.resources.capability_profile import ModelCapabilityProfile
    from pados.resources.models import ModelCatalog, ModelRuntimeProfile

    steps = result["steps"]
    # 1. persistence
    if args.rehearsal:  # the fakes' own profile; the tracked M3.1 report is internal evidence (not exported)
        profile, description, fakes = _rehearsal_profile()
    else:
        report = json.loads(M3_REPORT.read_text(encoding="utf-8"))
        profile = ModelCapabilityProfile.model_validate(report["profile"])
        description = ModelRuntimeProfile.model_validate(report["catalog_description"])
        if profile.profile_id != EXPECTED_PROFILE_ID:
            raise RuntimeError("M3_PROFILE_ID_UNEXPECTED")
    store = ModelCatalogStore(out / "state" / "model-catalog.json")
    digest = store.save(ModelCatalog([description]).register_capability_profile(profile))
    reloaded = store.load().capability_profile(description.model_id)
    persisted = reloaded is not None and reloaded.model_dump(mode="json") == profile.model_dump(mode="json")
    steps["persistence"] = {"catalog_digest": digest, "profile_id": profile.profile_id,
                            "reloaded_identical": persisted, "authorization_in_document": False}
    record({"record_type": "persistence", **steps["persistence"]})
    if not persisted:
        raise RuntimeError("PERSISTENCE_CHANGED_IDENTITY")

    # 2. live observation and binding
    if args.rehearsal:
        clock, profiler, fake = fakes
        fake.__enter__()
        runtime_url = fake.url
    else:
        from pados.local_intelligence.device import DeviceProfiler

        clock, profiler, fake, runtime_url = None, (lambda: DeviceProfiler.local().profile()), None, args.runtime_url
    chain, stop = build_chain(out / "chain", catalog_path=store.path, runtime_endpoint=runtime_url, profiler=profiler,
                              transport="inprocess" if args.rehearsal else "uvicorn", clock=clock)
    try:
        binding = chain.binding(chain.profile)
        steps["live_state"] = chain.live.evidence()
        steps["binding"] = binding.model_dump(mode="json")
        record({"record_type": "binding", **steps["binding"]})
        local = worker_id_for(chain.profile)
        if not binding.bindable:
            result["REAL_TASK_DOGFOOD"] = "BLOCKED"
            result["blocked_reason"] = list(binding.reasons)
            return

        # 3. authorization boundary, live
        chain.write_registration(models=())
        planner, executor, bridge, adapter, task = chain.fabric()
        plan = planner.plan(task)
        steps["unauthorized"] = {"feasible": plan.feasible, "codes": list(plan.infeasible_codes)}
        record({"record_type": "unauthorized_plan", **steps["unauthorized"]})

        chain.write_registration()
        planner, executor, bridge, adapter, task = chain.fabric()
        started = time.perf_counter()
        plan = planner.plan(task)
        stage = plan.stages[-1]
        candidates = {worker: {"availability": spec.availability.value, "reason": spec.reason}
                      for worker, spec in measured_local_workers(chain.measured(), _request(task, stage, PROJECT,
                                                                                            CAPABILITY),
                                                                 CapabilityId.SUMMARIZE).items()}
        steps["plan"] = {"feasible": plan.feasible, "plan_hash": plan.plan_hash, "router_status": stage.router_status,
                         "router_selection_hash": stage.router_selection_hash, "candidates": candidates,
                         "selected": local if plan.feasible else None}
        record({"record_type": "plan", **steps["plan"]})
        telemetry: dict[str, Any] = {}
        if plan.feasible:
            from pados.local_intelligence.benchmark import process_tree_snapshot, server_config_snapshot

            name = chain.profile.binding.runtime_family
            telemetry["before"] = process_tree_snapshot(name)
            outcome = executor.execute(plan, task)
            elapsed = time.perf_counter() - started
            telemetry["after"] = process_tree_snapshot(name)
            telemetry["server_config"] = server_config_snapshot(name)
            payload = chain.output_bytes(outcome) or b""
            run_entry = adapter.executions[-1] if adapter.executions else {}
            validation = validate_extraction(payload) if payload else {"passed": False}
            selection = outcome.router_evidence.get(str(stage.router_selection_hash))
            steps["dogfood"] = {
                "task_id": task.task_id, "required_capability": CAPABILITY,
                "requirement": REQUIREMENT.model_dump(mode="json"), "requirement_hash": REQUIREMENT.requirement_hash,
                "status": outcome.status.value, "failure_code": outcome.failure_code,
                "selected_worker_id": selection.selected_worker_id if selection else None,
                "selection_reason": selection.explanation if selection else None,
                "profile_id": chain.profile.profile_id, "manifest_digest": chain.profile.artifact.manifest_digest,
                "runtime": {"runtime_id": binding.runtime_id, "runtime_version": binding.runtime_version,
                            "runtime_ref": binding.runtime_ref},
                "device_profile_id": binding.device_profile_id,
                "authorization_evidence": chain.events,
                "measured_route": _route(run_entry.get("measured")),
                "node_execution_receipt": run_entry.get("receipt"),
                "node_record_state": (run_entry.get("record") or {}).get("state"),
                "node_record_execution": (run_entry.get("record") or {}).get("execution"),
                "node_refusal": run_entry.get("refused"), "node_failure": run_entry.get("failure"),
                "execution_receipt": outcome.plan_receipt.model_dump(mode="json"),
                "attempts": [{"stage_id": a.stage_id, "adapter_id": a.adapter_id, "failure_code": a.failure_code}
                             for a in outcome.attempts],
                "response": payload.decode("utf-8", errors="replace")[:4000],
                "response_sha256": hashlib.sha256(payload).hexdigest(), "validation": validation,
                "wall_s": round(elapsed, 3), "resources": telemetry,
                "graph_projection": chain.projection(), "semantic_authority": "NONE"}
            record({"record_type": "dogfood", **steps["dogfood"]})
            live_ok = (outcome.status is ExecutionStatus.COMPLETED and selection is not None
                       and selection.selected_worker_id == local and validation.get("passed") is True
                       and (run_entry.get("receipt") or {}).get("verification", {}).get("record_mac") == "VERIFIED")
            result["REAL_TASK_DOGFOOD"] = ("PASS" if live_ok and not args.rehearsal else
                                           "REHEARSAL_PASS" if live_ok else "FAIL")

            # revoked after plan, before execution -> refused (no inference)
            planner, executor, bridge, adapter, task = chain.fabric()
            plan = planner.plan(task)
            chats_before = len(adapter.executions)
            chain.revoke()
            try:
                executor.execute(plan, task)
                refused = "NOT_REFUSED"
            except ExecutionRefused as exc:
                refused = str(exc)
            steps["revoked_after_plan"] = {"plan_feasible": plan.feasible, "execution": refused,
                                           "adapter_runs": len(adapter.executions) - chats_before}
            record({"record_type": "revoked_after_plan", **steps["revoked_after_plan"]})
            planner, *_ = chain.fabric()
            replanned = planner.plan(task)
            steps["revoked_before_selection"] = {"feasible": replanned.feasible, "codes": list(replanned.infeasible_codes)}
            record({"record_type": "revoked_before_selection", **steps["revoked_before_selection"]})
        else:
            result["REAL_TASK_DOGFOOD"] = "BLOCKED"
            result["blocked_reason"] = list(plan.infeasible_codes)

        # 4. integrity probe
        if not args.skip_integrity and binding.runtime_ref:
            steps["integrity"] = integrity_probe(runtime_url, binding.runtime_ref, chain.profile.binding.runtime_family,
                                                 out, record)
    finally:
        stop()
        if fake is not None:
            fake.__exit__(None, None, None)
    result["authorization_left_in_place"] = False  # the dogfood registration ends revoked (model not named)


def _route(measured: Any) -> dict[str, Any] | None:
    """The admitted measured route, JSON-safe (the bound execution condition as its generic contract)."""
    if measured is None:
        return None
    return {key: value.model_dump(mode="json") if hasattr(value, "model_dump") else
            list(value) if isinstance(value, tuple) else value for key, value in vars(measured).items()}


def _request(task: Any, stage: Any, project: str, capability: str) -> Any:
    from pados.capability.seams import InferenceRequest

    return InferenceRequest(project_id=project, stage_id=stage.stage_id, capability_id=capability, risk_level="LOW",
                            classification=task.classification, network_policy=task.network_policy.value,
                            grant_hash="0" * 64)


def integrity_probe(url: str, model: str, process_name: str, out: Path,
                    record: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    from pados.local_intelligence.benchmark import (
        BenchmarkRunner,
        OllamaClient,
        process_tree_snapshot,
        server_config_snapshot,
    )

    records: list[dict[str, Any]] = []
    runner = BenchmarkRunner(client=OllamaClient(url), model=model, record=records.append, log=lambda _: None)
    probe: dict[str, Any] = {"server_config": server_config_snapshot(process_name)}
    runner.timeout_behaviour()
    probe["process_tree_after_timeout"] = process_tree_snapshot(process_name)
    runner.concurrency(levels=(2,))
    probe["timeout"] = next((r["details"] for r in records if r.get("task_id") == "timeout_recovery"), None)
    probe["concurrency"] = next((r["semantics"] for r in records if r.get("record_type") == "concurrency"), None)
    for entry in records:
        record({"record_type": "integrity", **entry})
    (out / "M4_1_INTEGRITY_RAW.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records), encoding="utf-8")
    return probe


def _rehearsal_profile() -> tuple[Any, Any, Any]:
    tests = ROOT / "tests" / "local_intelligence"
    sys.path.insert(0, str(tests))
    from fake_ollama import FakeOllama
    from live_chain_support import LICENSE, Clock, fake_profiler, rehearsal_profile

    from pados.resources.models import profile_from_capability

    profile = rehearsal_profile()
    clock = Clock()
    return profile, profile_from_capability(profile, license=LICENSE), (clock, fake_profiler(clock),
                                                                         FakeOllama(present=True))


if __name__ == "__main__":
    platform.system()  # imported for operators reading the source: no host name or user is recorded
    raise SystemExit(main())
