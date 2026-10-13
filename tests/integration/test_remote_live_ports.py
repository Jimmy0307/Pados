"""Remote approval against real PADOS authority: control_approvals + proof-lane CAS."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.config import get_settings
from pados.db import get_engine
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.remote.approval import RemoteApprovalOutcome, apply_remote_decision, bind_remote_decision
from pados.remote.intent import parse_client_intent
from pados.remote.live import LiveRemotePorts
from pados.state_proof import ObjectStore, StateProofService
from tests.integration.test_p6_21g_state_integration import _registered_scope, _request


@pytest.fixture
def remote_live(tmp_path: Path):
    engine = get_engine(get_settings())
    gateway = P6_19BStateGateway(StateProofService(engine, ObjectStore(tmp_path / "objects")))
    service = ProductLoopService(
        maker=FileDirectMaker(tmp_path / "workspace", protected_roots=(Path("C:/PADOS"),)),
        validator=FileValidator(),
        state_gateway=gateway,
    )
    ports = LiveRemotePorts(gateway=gateway, operational_engine=engine, presentation_dir=tmp_path / "presentations")
    yield service, ports, engine
    engine.dispose()


def _human_gated(service: ProductLoopService) -> tuple[str, str]:
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    request = GoalRequest.model_validate({
        **_request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n").model_dump(mode="json"),
        "human_gate": True,
    })
    report = asyncio.run(service.execute(request))
    assert report.status.value == "HUMAN_REVIEW_REQUIRED"
    return str(report.run_id), scope_id


def _decide(ports: LiveRemotePorts, run_id: str, scope_id: str, kind: str = "APPROVE"):
    summary = ports.run_summary(run_id)
    assert summary is not None and summary.get("approval_ref")
    ref = str(summary["approval_ref"])
    envelope = parse_client_intent(
        {"intent_id": f"i-{uuid4()}", "project_id": scope_id, "intent_type": kind,
         "target_run_id": run_id, "approval_ref": ref, "step_up_proof": f"step-up-proof:{uuid4()}"},
        actor_id="owner", session_id="rs-integration",
    )
    presented = ports.presented_decision(ref)
    assert presented is not None
    binding = bind_remote_decision(envelope, presented, candidate_receipt_id=presented.candidate_receipt_id)
    current = ports.current_decision(run_id)
    assert current is not None
    result = asyncio.run(apply_remote_decision(
        binding, current=current, current_scope_version=ports.scope_version(scope_id),
        actor_may_approve=lambda actor, scope: True, commit=ports.commit(), decide=ports.decide()))
    return result, ref


def _audit(engine, scope_id: str) -> list[dict]:
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT actor, approval_ref FROM state_proof_semantic_audit WHERE scope_id = :s ORDER BY sequence_id"),
            {"s": scope_id}).mappings()]


def _approval_status(engine, ref: str) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT status FROM control_approvals WHERE id::text = :id"),
                                      {"id": ref}).scalar())


def test_remote_approval_commits_via_cas_and_records_approval(remote_live) -> None:
    service, ports, engine = remote_live
    run_id, scope_id = _human_gated(service)
    before = ports.scope_version(scope_id)
    result, ref = _decide(ports, run_id, scope_id)
    assert result.outcome is RemoteApprovalOutcome.COMMITTED, result
    assert ports.scope_version(scope_id) == before + 1
    assert _approval_status(engine, ref) == "APPROVED"
    audit = _audit(engine, scope_id)
    assert audit[-1] == {"actor": "REMOTE:owner", "approval_ref": ref}


def test_remote_approval_fails_closed_when_scope_moved(remote_live) -> None:
    service, ports, engine = remote_live
    run_id, scope_id = _human_gated(service)
    ref = str(ports.run_summary(run_id)["approval_ref"])  # type: ignore[index]
    # Another accepted change advances the scope head after the decision was presented.
    other_run, _ = _human_gated(service)
    committed, _ = _decide(ports, other_run, scope_id)
    assert committed.outcome is RemoteApprovalOutcome.COMMITTED
    envelope = parse_client_intent(
        {"intent_id": f"i-{uuid4()}", "project_id": scope_id, "intent_type": "APPROVE",
         "target_run_id": run_id, "approval_ref": ref, "step_up_proof": f"step-up-proof:{uuid4()}"},
        actor_id="owner", session_id="rs")
    presented = ports.presented_decision(ref)
    assert presented is not None
    binding = bind_remote_decision(envelope, presented, candidate_receipt_id=presented.candidate_receipt_id)
    result = asyncio.run(apply_remote_decision(
        binding, current=ports.current_decision(run_id), current_scope_version=ports.scope_version(scope_id),  # type: ignore[arg-type]
        actor_may_approve=lambda actor, scope: True, commit=ports.commit(), decide=ports.decide()))
    assert result.outcome is RemoteApprovalOutcome.SCOPE_VERSION_CHANGED
    assert _approval_status(engine, ref) == "PENDING"


def test_remote_reject_records_decision_without_commit(remote_live) -> None:
    service, ports, engine = remote_live
    run_id, scope_id = _human_gated(service)
    before = ports.scope_version(scope_id)
    result, ref = _decide(ports, run_id, scope_id, kind="REJECT")
    assert result.outcome is RemoteApprovalOutcome.REJECTED
    assert ports.scope_version(scope_id) == before
    assert _approval_status(engine, ref) == "REJECTED"


def test_tampered_presentation_is_refused(remote_live) -> None:
    service, ports, _engine = remote_live
    run_id, _scope = _human_gated(service)
    ref = str(ports.run_summary(run_id)["approval_ref"])  # type: ignore[index]
    path = ports.presentation_dir / f"{ref}.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"scope_version": ', '"scope_version": 9'), encoding="utf-8")
    assert ports.presented_decision(ref) is None
