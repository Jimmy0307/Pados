"""Connectivity Closure E: security properties of probe, reconnect, notification retry and gate."""
from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.connectivity.maker import RequirementGatedMaker
from pados.connectivity.provider_gate import gate_product_loop_maker
from pados.connectivity.runtime import connectivity_reader, start_runtime, stop_runtime
from pados.connectivity.state import ConnectivityState, Link
from pados.config import Settings
from pados.product_loop import provider_selection
from pados.product_loop.codex_provider import CodexCliMaker
from pados.remote.deployment import check_remote_deployment
from pados.remote.notifications import PUSH_ALLOWED_KEYS, Notification, NotificationCenter, NotificationKind
from pados.remote.outbox import DurableNotificationOutbox
from pados.remote.routes import register_remote_routes
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType
from tests.connectivity.connectivity_support import (
    OVERLAY_HOST,
    WAN_HOST,
    FakeProbes,
    SimRemoteClient,
    write_config,
)
from tests.remote.remote_slice_support import PROJECT, Slice, build_slice, hardened_settings

ROOT = Path(__file__).resolve().parents[2]
CONNECTIVITY = ROOT / "src" / "pados" / "connectivity"
BG = {"enabled": True, "interval_seconds": 10, "timeout_seconds": 2, "max_backoff_seconds": 80}


@pytest.fixture(autouse=True)
def _no_leaked_runtime() -> Iterator[None]:
    yield
    stop_runtime()


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _connected_slice(tmp_path: Path, probes: FakeProbes) -> tuple[Slice, TestClient, Any]:
    """The remote slice with /remote/v1/connectivity served by the shared background runtime."""
    s = build_slice(tmp_path)
    path = write_config(tmp_path, background=BG)
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory)
    assert runtime is not None
    settings = hardened_settings()
    app = FastAPI()
    register_remote_routes(app, settings, service=s.service,
                           deployment=check_remote_deployment(settings, remote_bind_host="100.64.0.10"),
                           nodes=lambda: {"nodes": []}, connectivity=connectivity_reader(path))
    return s, TestClient(app), runtime


def _semantic_state(s: Slice) -> tuple[Any, ...]:
    return (dict(s.gateway.versions), len(s.gateway.commits), dict(s.gateway.receipts),
            len(s.ports.work_requests), len(s.ports.commit_calls), len(s.gateway.read_index))


# --- probe cannot mutate semantic authority -----------------------------------------------------

def test_connectivity_package_imports_no_semantic_write_path() -> None:
    banned = ("pados.state_proof", "pados.product_loop.state", "pados.memory", "pados.remote.live",
              "pados.remote.approval", "pados.remote.service", "pados.control", "pados.db", "sqlalchemy")
    for path in CONNECTIVITY.glob("*.py"):
        hits = {m for m in _imports(path) if m.startswith(banned)}
        assert hits == set(), (path.name, hits)
        text = path.read_text(encoding="utf-8")
        for call in ("commit_result(", "apply_remote_decision(", "submit_task(", "request_cancel("):
            assert call not in text, (path.name, call)


def test_background_probe_ticks_never_change_semantic_state(tmp_path: Path) -> None:
    probes = FakeProbes()
    s, _client, runtime = _connected_slice(tmp_path, probes)
    run_id = s.human_gated_run()
    before = _semantic_state(s)
    for up in (True, False, False, True, True, False, True) * 3:
        probes.wan = probes.overlay = up
        runtime.probe.run_once()
    assert _semantic_state(s) == before
    read = s.gateway.read_run(run_id)
    assert read is not None and read.status == "HUMAN_REVIEW_REQUIRED"  # no cancel, no auto-commit


# --- probe target cannot become an arbitrary SSRF target ----------------------------------------

def test_remote_clients_cannot_supply_probe_targets(tmp_path: Path) -> None:
    probes = FakeProbes()  # its factory asserts that only operator-config targets are ever probed
    s, client, runtime = _connected_slice(tmp_path, probes)
    runtime.probe.run_once()
    bad = {"host": "169.254.169.254", "port": 80, "target": "http://169.254.169.254/latest/meta-data"}
    view = client.get("/remote/v1/connectivity", params=bad, headers=s.owner_headers)
    assert view.status_code == 200 and "169.254" not in view.text
    for method in (client.post, client.put):
        assert method("/remote/v1/connectivity", json=bad, headers=s.owner_headers).status_code == 405
    assert set(probes.calls) <= {"WAN", "OVERLAY"}
    routes = (ROOT / "src" / "pados" / "remote" / "routes.py").read_text(encoding="utf-8")
    assert "probe" not in routes.lower()
    assert WAN_HOST not in view.text and OVERLAY_HOST not in view.text


# --- reconnect does not bypass authentication ---------------------------------------------------

def test_reconnect_does_not_bypass_authentication(tmp_path: Path) -> None:
    probes = FakeProbes()
    s, client, runtime = _connected_slice(tmp_path, probes)
    ok = client.get("/remote/v1/connectivity", headers=s.owner_headers)
    assert ok.status_code == 200
    probes.wan = probes.overlay = False
    runtime.probe.run_once()
    probes.wan = probes.overlay = True
    runtime.probe.run_once()
    runtime.probe.run_once()  # reconnected: RECOVERING -> CONNECTED
    assert runtime.monitor.state(Link.REMOTE_TRANSPORT) is ConnectivityState.CONNECTED
    expired = {**s.owner_headers, "X-PADOS-Remote-Proof": "previous-session-credential"}
    for path in ("/remote/v1/connectivity", "/remote/v1/attention", "/remote/v1/home-base", "/remote/v1/jobs"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=expired).status_code == 401
    session_reuse = {"X-PADOS-Remote-Actor": "owner", "X-PADOS-Remote-Proof": "rs-" + "0" * 24}
    assert client.get("/remote/v1/attention", headers=session_reuse).status_code == 401
    assert client.post("/remote/v1/intents", json={"intent_id": "x", "project_id": PROJECT,
                                                   "intent_type": "START_TASK"}).status_code == 401
    assert client.get("/remote/v1/attention", headers=s.owner_headers).status_code == 200


# --- reconnect does not replay approval / duplicate a job ---------------------------------------

def _count_calls(target: Any, name: str) -> list[int]:
    calls: list[int] = []
    original = getattr(target, name)

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await original(*args, **kwargs)

    setattr(target, name, wrapped)
    return calls


def _count_port(ports: Any, name: str) -> list[int]:
    calls: list[int] = []
    factory: Callable[[], Callable[..., Awaitable[Any]]] = getattr(ports, name)

    def counted() -> Callable[..., Awaitable[Any]]:
        inner = factory()

        async def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return await inner(*args, **kwargs)

        return call

    setattr(ports, name, counted)
    return calls


def test_reconnect_does_not_duplicate_job_or_replay_approval(tmp_path: Path) -> None:
    from pados.connectivity.adapter import SimulatedConnectivity, TransportUnavailable

    s = build_slice(tmp_path)
    net = SimulatedConnectivity()
    phone = SimRemoteClient(s, net)
    submits = _count_calls(s.ports, "submit_task")
    decides, commits = _count_port(s.ports, "decide"), _count_port(s.ports, "commit")
    start = {"intent_id": "e-start", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "do it"}
    first = phone.post("/intents", start).json()
    for _ in range(3):
        net.go_offline()
        with pytest.raises(TransportUnavailable):
            phone.post("/intents", start)
        net.recover()
        again = phone.post("/intents", start).json()
        assert again["replayed"] is True and again["run_id"] == first["run_id"]
    assert len(submits) == 1 and len(s.ports.work_requests) == 1

    run_id = s.human_gated_run()
    ref = phone.get("/approvals").json()["pending"][0]["approval_ref"]
    phone.headers, proof = s.step_up(ref)  # fresh, user-verified, single-use
    approve = {"intent_id": "e-approve", "project_id": PROJECT, "target_run_id": run_id, "step_up_proof": proof}
    original = phone.post(f"/approvals/{ref}/approve", approve).json()
    for _ in range(3):
        net.go_offline()
        net.recover()
        replay = phone.post(f"/approvals/{ref}/approve", approve).json()
        assert replay["outcome"] == original["outcome"] == "COMMITTED"
        assert replay["ledger_entry_hash"] == original["ledger_entry_hash"]  # the original ledger outcome
    assert len(decides) == 1 and len(commits) == 1 and s.gateway.versions[PROJECT] == 1


# --- notification retry cannot duplicate a semantic operation -----------------------------------

def test_notification_retry_cannot_trigger_semantic_operations(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    before = _semantic_state(s)
    sent: list[dict[str, Any]] = []
    up = {"value": False}

    def push(payload: dict[str, Any]) -> None:
        if not up["value"]:
            raise ConnectionError("offline")
        sent.append(payload)

    now = [datetime.now(UTC)]
    outbox = DurableNotificationOutbox(tmp_path / "outbox.json", clock=lambda: now[0],
                                       base_backoff=timedelta(seconds=1), max_backoff=timedelta(seconds=1))
    center = NotificationCenter(push=push, durable=outbox)
    center.emit(Notification(NotificationKind.APPROVAL_REQUIRED, PROJECT, run_id))
    for flips in range(10):
        up["value"] = flips >= 5
        now[0] += timedelta(seconds=5)
        center.flush()
    assert len(sent) == 1 and set(sent[0]) <= PUSH_ALLOWED_KEYS
    assert _semantic_state(s) == before
    imported = _imports(ROOT / "src" / "pados" / "remote" / "outbox.py")
    assert not {m for m in imported if m.startswith(("pados.remote.service", "pados.remote.approval",
                                                     "pados.remote.live", "pados.state_proof",
                                                     "pados.product_loop"))}


# --- provider outage never triggers implicit local fallback -------------------------------------

def _worker(worker_id: str, worker_type: WorkerType) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id, worker_type=worker_type, display_name=worker_id,
        availability=WorkerAvailability.AVAILABLE, capabilities=[CapabilityId.MODIFY_CODE],
        risk_ceiling=RiskLevel.MEDIUM, supports_structured_output=True, supports_file_changes=True,
        supports_tool_calls=False, supports_streaming=False, max_context_tokens=8000,
        default_timeout_seconds=60,
    )


def test_provider_outage_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, background=BG)
    probes = FakeProbes(wan=False)
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory)
    assert runtime is not None
    runtime.probe.run_once()
    routed: list[Any] = []
    real_select = provider_selection.select_worker

    def spy_select(*args: Any, **kwargs: Any) -> Any:
        routed.append(kwargs.get("allowed_worker_types"))
        return real_select(*args, **kwargs)

    monkeypatch.setattr(provider_selection, "select_worker", spy_select)
    workers = {"codex-cli": _worker("codex-cli", WorkerType.CODEX_CLI),
               "ollama-local": _worker("ollama-local", WorkerType.OLLAMA),
               "claude-code-runtime": _worker("claude-code-runtime", WorkerType.CLAUDE_CODE)}
    executed: list[Any] = []
    (tmp_path / "ws").mkdir()
    settings = Settings(_env_file=None, PADOS_CONNECTIVITY_CONFIG=path)
    codex = CodexCliMaker(tmp_path / "ws", settings=settings, runner=lambda *a, **k: executed.append(a),
                          worker_discovery=lambda _s: workers, executable_resolver=lambda: "/nonexistent")
    maker = gate_product_loop_maker(codex, settings)
    assert isinstance(maker, RequirementGatedMaker)
    for attempt in (1, 2, 3):
        result = asyncio.run(maker.make(None, run_id="run-x", attempt=attempt))  # type: ignore[arg-type]
        assert result.summary == "PROVIDER_UNREACHABLE"
    assert executed == []
    # Only THE Worker Router was consulted, narrowed to product-loop adapters; no other provider tried.
    assert routed and all(allowed == frozenset(provider_selection.PRODUCT_LOOP_ADAPTERS) for allowed in routed)
    assert runtime.monitor.state(Link.WAN) is ConnectivityState.OFFLINE
    for path_ in CONNECTIVITY.glob("*.py"):
        assert "pados.worker.router" not in _imports(path_), path_.name  # no second router
