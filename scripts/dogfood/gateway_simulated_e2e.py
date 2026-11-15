# SPDX-License-Identifier: AGPL-3.0-only
"""Simulated enterprise gateway E2E: real processes, real loopback HTTP, synthetic data only. Label: SIMULATED_E2E_PASS.

Processes: the simulated gateway (``tests/compute_node/gateway_sim.py``, its own process, stopped and restarted to
simulate a VPN outage and reconnect), this harness driving the production ``GatewayBackend`` over HTTP, and
``pados serve`` (its own process) serving ``/control/providers`` from a temporary runtime directory. With
``--playwright`` the live browser spec ``web/e2e/providers-live.spec.ts`` then drives the AI 模型與連線 page against that
real backend. With ``--real-ollama-catalog`` the temporary runtime also gets a copy of a measured catalog and a
temporary registration naming its model, so the page shows a real measured and authorized local model next to the
simulated gateway (diagnostics only: discovery and identity, no inference).

Steps (each recorded): discovery and model selection, auth success and failure, rate limit, timeout and dropped
connection with no re-send and no duplicate side effect, an honoured idempotency key retried without a second side
effect, data-classification refusal (a CONFIDENTIAL project to an INTERNAL-ceiling gateway sends nothing), outage ->
circuit OPEN -> refused without traffic -> gateway back -> HALF_OPEN -> CLOSED, a receipt for every attempt, and the
provider routes over real HTTP. Nothing is written into the repository or the operator's runtime.

    python scripts/dogfood/gateway_simulated_e2e.py --out <dir outside the repo> [--playwright] [--real-ollama-catalog F]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tests.compute_node.gateway_support import KEY_ENV, sim_contract  # noqa: E402

from pados.compute_node.circuit import CircuitState  # noqa: E402
from pados.compute_node.gateway import AttemptRecorder, GatewayBackend, GatewayError, GatewayFailure  # noqa: E402
from pados.compute_node.gateway_contract import parse_gateway_contract  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402

LABEL = "SIMULATED_E2E_PASS"
PROMPT = {"messages": [{"role": "user", "content": "Synthetic E2E probe: summarize the word alpha."}]}


class Run:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.steps: list[dict[str, Any]] = []

    def record(self, name: str, passed: bool, **detail: Any) -> bool:
        self.steps.append({"step": name, "passed": bool(passed), "at": datetime.now(UTC).isoformat(), **detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}", flush=True)
        return passed


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - loopback only
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


class Sim:
    """The simulated gateway as its own process (stop = outage, start on the same port = reconnect)."""

    def __init__(self, env: dict[str, str], log: Path) -> None:
        self.env, self.log, self.port = env, log, 0
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        handle = self.log.open("a", encoding="utf-8")
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, str(REPO / "tests/compute_node/gateway_sim.py"), "--port", str(self.port),
             "--key-env", KEY_ENV], env=self.env, stdout=subprocess.PIPE, stderr=handle, text=True)
        assert self.process.stdout is not None
        self.port = int(json.loads(self.process.stdout.readline())["port"])

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def mode(self, mode: str, **extra: Any) -> None:
        http("POST", f"http://127.0.0.1:{self.port}/__sim/mode", {"mode": mode, **extra})

    def stats(self) -> dict[str, Any]:
        return dict(http("GET", f"http://127.0.0.1:{self.port}/__sim/stats")[1])


def adapter_steps(run: Run, sim: Sim, contract_doc: dict[str, Any], receipts: Path) -> None:
    contract = parse_gateway_contract(contract_doc)
    recorder = AttemptRecorder(receipts)
    gateway = GatewayBackend("e2e", contract=contract, recorder=recorder)

    def chat(model: str = "sim-small", classification: DataClassification = DataClassification.INTERNAL,
             backend: GatewayBackend = gateway, **kwargs: Any) -> Any:
        return backend.run(NodeCapability.LOCAL_INFERENCE, model, PROMPT, max_output_tokens=32, timeout_seconds=10,
                           classification=classification, **kwargs)

    def code(call: Any) -> GatewayError | None:
        try:
            call()
        except GatewayError as exc:
            return exc
        return None

    models = gateway.discover_detailed()
    chosen = next((m["model"] for m in models if m["loaded"]), None)
    run.record("discovery_and_model_selection", chosen == "sim-small",
               models=[{k: m[k] for k in ("model", "loaded")} for m in models], selected=chosen)
    output = chat(chosen or "sim-small")
    run.record("auth_success_and_chat", output.runtime["authority"] == "NON_AUTHORITATIVE_OBSERVATION",
               usage=output.runtime["usage"], measured=output.runtime["measured"])
    saved = os.environ[KEY_ENV]
    os.environ[KEY_ENV] = "sim-wrong-value"
    wrong = code(lambda: gateway.check_health())
    del os.environ[KEY_ENV]
    missing = code(lambda: chat())
    os.environ[KEY_ENV] = saved
    run.record("auth_failure", wrong is not None and wrong.failure is GatewayFailure.AUTH_FAILED
               and missing is not None and missing.failure is GatewayFailure.AUTH_NOT_CONFIGURED,
               wrong=wrong.code if wrong else None, missing=missing.code if missing else None)

    sim.mode("rate_limited", times=1)
    limited = code(lambda: chat())
    run.record("rate_limit", limited is not None and limited.failure is GatewayFailure.RATE_LIMITED,
               retry_after=limited.retry_after if limited else None)

    slow = GatewayBackend("e2e-slow", contract=contract.model_copy(update={"provider_id": "sim-gateway-slow",
                                                                          "timeouts": contract.timeouts.model_copy(
                                                                              update={"read_seconds": 0.5})}),
                          recorder=recorder)
    before = sim.stats()["generate_requests"]
    sim.mode("slow", times=1, delay=1.5)
    timed_out = code(lambda: chat(backend=slow))
    time.sleep(1.3)
    run.record("timeout_unknown_outcome_not_resent", timed_out is not None
               and timed_out.failure is GatewayFailure.WRITE_OUTCOME_UNKNOWN
               and timed_out.cause is GatewayFailure.TIMEOUT and sim.stats()["generate_requests"] == before + 1)

    stats = sim.stats()
    sim.mode("drop", times=1)
    dropped = code(lambda: chat())
    after = sim.stats()
    run.record("dropped_connection_no_duplicate_side_effect", dropped is not None
               and dropped.cause is GatewayFailure.CONNECTION_LOST
               and after["generate_requests"] == stats["generate_requests"] + 1
               and after["side_effects"] == stats["side_effects"] + 1)

    keyed = GatewayBackend("e2e-keyed", contract=contract.model_copy(update={
        "provider_id": "sim-gateway-keyed",
        "idempotency": contract.idempotency.model_copy(update={"header_name": "Idempotency-Key", "honoured": True})}),
        recorder=recorder)
    stats = sim.stats()
    sim.mode("drop", times=1)
    retried = chat(backend=keyed, idempotency_key="e2e-job:0")
    after = sim.stats()
    run.record("honoured_idempotency_retry_without_duplicate", retried.runtime["attempts"] == 2
               and after["side_effects"] == stats["side_effects"] + 1 and after["replayed"] == stats["replayed"] + 1)

    before = sim.stats()["generate_requests"]
    refused = code(lambda: chat(classification=DataClassification.CONFIDENTIAL))
    run.record("classification_refusal_confidential_project", refused is not None
               and refused.failure is GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION
               and sim.stats()["generate_requests"] == before)

    sim.stop()  # the VPN drops
    outage = code(lambda: gateway.check_health())
    opened = gateway.circuit.state is CircuitState.OPEN
    paused = code(lambda: chat())
    run.record("outage_opens_circuit_and_refuses_without_traffic", outage is not None
               and outage.failure is GatewayFailure.NETWORK_UNREACHABLE and opened
               and paused is not None and paused.failure is GatewayFailure.CIRCUIT_OPEN)
    sim.start()  # the VPN is back, same address
    time.sleep(contract.circuit.open_seconds + 0.2)
    half_open = gateway.circuit.state is CircuitState.HALF_OPEN
    recovered = gateway.check_health() == 200 and gateway.circuit.state is CircuitState.CLOSED
    run.record("recovery_half_open_then_closed", half_open and recovered,
               transitions=[t["to"] for t in gateway.circuit.snapshot()["transitions"]])

    lines = [json.loads(line) for path in receipts.glob("*.jsonl") for line in path.read_text("utf-8").splitlines()]
    content_free = not any("Synthetic E2E probe" in json.dumps(item) or "SIMULATED" in json.dumps(item) for item in lines)
    run.record("receipt_for_every_attempt", len(lines) == len(recorder.recent(10_000)) and content_free
               and all(item["semantic_authority"] is False for item in lines), receipts=len(lines))


def write_runtime(root: Path, contract_doc: dict[str, Any], real_catalog: Path | None) -> dict[str, str]:
    private = root / "private"
    private.mkdir(parents=True)
    contract_path = private / "gateway-contract.json"
    contract_path.write_text(json.dumps(contract_doc, indent=2), encoding="utf-8")
    setup = {"schema_version": "pados.providers.setup.v1", "revision": 1, "providers": [{
        "provider_id": "sim-gateway", "kind": "enterprise_gateway", "display_name": "Simulated enterprise gateway",
        "enabled": True, "contract_path": str(contract_path), "timeout_seconds": 10, "project_ids": ["*"],
        "max_classification": "INTERNAL", "capability_requirement": "TEXT_SUMMARY"}]}
    (root / "local").mkdir()
    (root / "local" / "providers.json").write_text(json.dumps(setup, indent=2), encoding="utf-8")
    env = {"PADOS_PROVIDERS_SETUP": str(root / "local" / "providers.json")}
    if real_catalog is not None:
        from pados.local_intelligence.catalog_store import ModelCatalogStore

        catalog = root / "state" / "model-catalog.json"
        catalog.parent.mkdir()
        shutil.copyfile(real_catalog, catalog)
        profile = ModelCatalogStore(catalog).load().capability_profiles()[0]
        registry = {"schema_version": "pados.compute-nodes.registry.v1", "state_dir": str(root / "nodes"), "nodes": [{
            "node_id": "lcn-" + secrets.token_hex(10), "node_name": "temporary e2e registration",
            "endpoint": "http://127.0.0.1:11434", "auth_ref": "secretref://env/PADOS_E2E_UNUSED_NODE_KEY",
            "authorized": True, "allowed_capabilities": ["LOCAL_INFERENCE"], "max_classification": "INTERNAL",
            "project_ids": ["*"], "allowed_models": [profile.artifact.model_ref],
            "security_profile": "LOCAL_SINGLE_USER"}]}
        (root / "state" / "compute-nodes.json").write_text(json.dumps(registry, indent=2), encoding="utf-8")
        env.update({"PADOS_MODEL_CATALOG": str(catalog), "PADOS_COMPUTE_NODES_CONFIG": str(root / "state" /
                                                                                      "compute-nodes.json"),
                    "PADOS_CHAT_PROVIDER": profile.artifact.runtime_family,
                    "PADOS_CHAT_MODEL": profile.artifact.artifact_ref})
    return env


def wait_healthy(url: str, process: subprocess.Popen[Any], seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            if http("GET", url)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - a linear, recorded script
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--playwright", action="store_true")
    parser.add_argument("--real-ollama-catalog", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.is_relative_to(REPO):
        print("REFUSED: --out must be outside the repository")
        return 2
    out.mkdir(parents=True, exist_ok=False)
    run = Run(out)
    root = Path(tempfile.mkdtemp(prefix="pados-gateway-e2e-"))
    key = "sim-" + secrets.token_hex(16)
    os.environ[KEY_ENV] = key
    env = {**os.environ, KEY_ENV: key, "PYTHONUTF8": "1"}
    sim = Sim(env, out / "sim.log")
    server: subprocess.Popen[Any] | None = None
    try:
        sim.start()
        contract_doc = sim_contract(f"http://127.0.0.1:{sim.port}",
                                    circuit={"failure_threshold": 2, "open_seconds": 2})
        adapter_steps(run, sim, contract_doc, out / "receipts")

        operator = "e2e-" + secrets.token_hex(16)
        api_port = free_port()
        server_env = {**env, **write_runtime(root, contract_doc, args.real_ollama_catalog),
                      "PADOS_API_PORT": str(api_port), "PADOS_API_HOST": "127.0.0.1", "PADOS_ENV": "test",
                      "PADOS_CONTROL_API_TOKEN_HASH": hashlib.sha256(operator.encode()).hexdigest().upper()}
        with (out / "pados-serve.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen([sys.executable, "-m", "pados.cli.main", "serve"], cwd=root,  # noqa: S603
                                      env=server_env, stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{api_port}"
        run.record("pados_serve_started", wait_healthy(base + "/health/live", server))
        headers = {"X-PADOS-Control-Token": operator}
        status, listed = http("GET", base + "/control/providers", headers=headers)
        ids = [item["provider_id"] for item in listed.get("providers", [])] if status == 200 else []
        run.record("routes_list_providers", status == 200 and "sim-gateway" in ids, providers=ids)
        status, diagnosed = http("POST", base + "/control/providers/sim-gateway/diagnostics", {"probe_chat": True},
                                 headers)
        view = diagnosed.get("provider", {}) if status == 200 else {}
        run.record("routes_diagnostics_two_levels", view.get("reachability", {}).get("state") == "REACHABLE"
                   and all(m["governance"] == "NOT_MEASURED" for m in view.get("models", []))
                   and key not in json.dumps(diagnosed),
                   checks=[(c["check"], c["status"]) for c in view.get("checks", [])])
        if args.playwright:
            spec_env = {**server_env, "PADOS_E2E_PROVIDERS_LIVE": "1", "PADOS_E2E_CONTROL_VALUE": operator,
                        "PADOS_E2E_SIM_URL": f"http://127.0.0.1:{sim.port}", "PADOS_WEB_BACKEND_URL": base,
                        "PADOS_WEB_PORT": str(free_port()), "PADOS_E2E_EVIDENCE_DIR": str(out / "screenshots"),
                        "PADOS_E2E_REAL_OLLAMA": "1" if args.real_ollama_catalog else "0"}
            (out / "screenshots").mkdir()
            npx = shutil.which("npx") or "npx"
            result = subprocess.run([npx, "playwright", "test", "e2e/providers-live.spec.ts", "--workers=1",  # noqa: S603
                                     "--reporter=list"], cwd=REPO / "web", env=spec_env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=900, check=False)
            (out / "playwright.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            run.record("playwright_live_providers_page", result.returncode == 0 and " passed" in result.stdout,
                       tail=result.stdout.strip().splitlines()[-3:])
    except Exception as exc:  # noqa: BLE001 - every failure is reported
        run.record("harness_error", False, error=f"{type(exc).__name__}:{str(exc)[:300]}")
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait(timeout=20)
        sim.stop()
        shutil.rmtree(root, ignore_errors=True)  # the temporary registration and runtime are removed
    passed = all(step["passed"] for step in run.steps) and bool(run.steps)
    result = {"schema": "pados.dogfood.gateway-simulated-e2e.v1", "label": LABEL if passed else "SIMULATED_E2E_FAIL",
              "status": "ENTERPRISE_LIVE_NOT_VALIDATED", "synthetic_data_only": True,
              "temporary_runtime_removed": not root.exists(), "steps": run.steps}
    (out / "GATEWAY_SIMULATED_E2E_RESULT.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(result["label"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
