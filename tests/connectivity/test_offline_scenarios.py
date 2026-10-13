"""Required scenarios A, B, D and E (C lives in tests/compute_node: needs the node fixture)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pados.connectivity.adapter import SimulatedConnectivity, TransportUnavailable
from pados.connectivity.egress import WanEgressGate
from pados.connectivity.maker import ConnectivityGatedMaker
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator
from tests.connectivity.connectivity_support import SimRemoteClient
from tests.remote.remote_slice_support import PROJECT, build_slice


def _gate(net: SimulatedConnectivity) -> WanEgressGate:
    return WanEgressGate(wan_available=lambda: net.wan, endpoint_reachable=net.endpoint_reachable)


def _object_count(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def test_scenario_a_offline_job_continuity_then_reconnect_and_approve(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    net = SimulatedConnectivity()
    phone = SimRemoteClient(s, net)
    # local maker behind the connectivity gate (local endpoint: never WAN-gated)
    s.loop.maker = ConnectivityGatedMaker(s.loop.maker, endpoint="http://127.0.0.1:0", gate=_gate(net))  # type: ignore[assignment]

    submitted = phone.post("/intents", {"intent_id": "a-start", "project_id": PROJECT, "intent_type": "START_TASK",
                                        "human_message": "apply the bounded change"}).json()
    assert submitted["outcome"] == "SUBMITTED"
    net.clients_down.add(phone.client_id)  # 3. remote client disconnects
    s.service.jobs.on_client_disconnect("rs-phone")
    net.go_offline()  # 4. WAN offline
    with pytest.raises(TransportUnavailable):
        phone.get("/attention")

    run_id = s.human_gated_run()  # 5-9. local execution continues to the human gate
    read = s.gateway.read_run(run_id)
    assert read is not None and read.status == "HUMAN_REVIEW_REQUIRED"
    assert s.gateway.receipts and s.gateway.versions.get(PROJECT, 0) == 0  # receipt, no auto-commit
    offline_artifacts = s.ports.run_summary(run_id)["artifacts"]  # type: ignore[index]
    assert offline_artifacts and offline_artifacts[0]["output_content_hash"]

    net.recover()  # 10-11. WAN recovers, client reconnects (fresh authentication per request)
    net.clients_down.clear()
    attention = phone.get("/attention").json()
    assert [r["run_id"] for r in attention["waiting_approval"]] == [run_id]  # 12. existing job visible
    detail = phone.get(f"/jobs/{run_id}").json()
    assert detail["artifacts"] == offline_artifacts  # 13. existing artifact visible
    ref = attention["waiting_approval"][0]["approval_ref"]
    phone.headers, proof = s.step_up(ref)
    decision = phone.post(f"/approvals/{ref}/approve", {"intent_id": "a-approve", "project_id": PROJECT,
                                                        "target_run_id": run_id, "step_up_proof": proof}).json()
    assert decision["outcome"] == "COMMITTED" and s.gateway.versions[PROJECT] == 1  # 14-15
    assert len(s.ports.work_requests) == 1 and len(s.gateway.read_index) == 1  # no duplicate run
    assert len(s.ports.commit_calls) == 1


def test_scenario_b_cloud_provider_job_fails_closed_without_fallback(tmp_path: Path) -> None:
    net = SimulatedConnectivity()
    net.go_offline()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    calls: list[str] = []

    class CloudMaker(FileDirectMaker):
        async def make(self, contract: Any, *, run_id: str, attempt: int) -> Any:
            calls.append("cloud")
            return await super().make(contract, run_id=run_id, attempt=attempt)

    maker = ConnectivityGatedMaker(CloudMaker(workspace), endpoint="https://api.provider.example/v1", gate=_gate(net))
    loop = ProductLoopService(maker=maker, validator=FileValidator(), state_gateway=gateway)  # type: ignore[arg-type]
    report = asyncio.run(loop.execute(GoalRequest.model_validate({
        "goal": "cloud job", "workspace_root": str(workspace), "scope_id": "proj-cloud",
        "changes": [{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        "acceptance_criteria": [{"criterion_id": "c", "check": "FILE_CONTAINS", "target_path": "app.py",
                                 "expected_text": "return 'good'"}], "authorized": True})))
    assert report.status is not ProductLoopStatus.COMMITTED
    assert report.final_result is not None and report.final_result.summary == "PROVIDER_UNREACHABLE"
    assert report.final_result.retryable is True
    assert calls == [] and not gateway.commits and not (workspace / "app.py").exists()


def test_scenario_d_repeated_disconnects_never_duplicate(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    net = SimulatedConnectivity()
    phone = SimRemoteClient(s, net)
    start = {"intent_id": "d-start", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "do it"}
    first = phone.post("/intents", start).json()
    for _ in range(4):  # disconnect/reconnect, client retries the same intent
        net.clients_down.add(phone.client_id)
        with pytest.raises(TransportUnavailable):
            phone.post("/intents", start)
        net.clients_down.clear()
        again = phone.post("/intents", start).json()
        assert again["replayed"] is True and again["run_id"] == first["run_id"]
    assert len(s.ports.work_requests) == 1
    reused = phone.post("/intents", {**start, "human_message": "something else"})
    assert reused.status_code == 422 and reused.json()["detail"] == "INTENT_ID_REUSED"

    run_id = s.human_gated_run()
    ref = phone.get("/approvals").json()["pending"][0]["approval_ref"]
    phone.headers, proof = s.step_up(ref)
    approve = {"intent_id": "d-approve", "project_id": PROJECT, "target_run_id": run_id, "step_up_proof": proof}
    outcomes = []
    for _ in range(3):
        net.clients_down.add(phone.client_id)
        net.clients_down.clear()
        outcomes.append(phone.post(f"/approvals/{ref}/approve", approve).json()["outcome"])
    assert outcomes == ["COMMITTED", "COMMITTED", "COMMITTED"]
    assert len(s.ports.commit_calls) == 1 and s.gateway.versions[PROJECT] == 1  # one CAS, one approval
    fresh = phone.post(f"/approvals/{ref}/approve", {**approve, "intent_id": "d-approve-2"})
    assert fresh.status_code == 422 and fresh.json()["detail"] == "APPROVAL_REF_NOT_FOUND"
    latest = phone.get("/attention").json()
    assert latest["waiting_approval"] == [] and s.service.ledger.verify()


def test_scenario_e_offline_artifact_is_returned_not_regenerated(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    net = SimulatedConnectivity()
    phone = SimRemoteClient(s, net)
    net.go_offline()
    run_id = s.human_gated_run()
    original = s.ports.run_summary(run_id)["artifacts"]  # type: ignore[index]
    objects_before = _object_count(tmp_path / "objects")
    receipts_before = dict(s.gateway.receipts)
    for _ in range(3):
        net.recover()
        detail = phone.get(f"/jobs/{run_id}").json()
        assert detail["artifacts"] == original
        net.go_offline()
    assert original[0]["receipt_id"] and original[0]["output_content_hash"] and original[0]["completed_at"]
    assert _object_count(tmp_path / "objects") == objects_before  # no regeneration
    assert s.gateway.receipts == receipts_before
