"""Final Convergence V1 — live product loop on disposable PostgreSQL (operational + proof lanes).

TaskEnvelope -> Goal A COMPLETE / Goal B PARTIAL -> residual B2 (AUTO_AMENDABLE) through the
existing P6.10 TaskLoop -> ExecutionReceipts -> ResultCandidate -> verification -> WAITING_APPROVAL
-> Remote Operations (real HTTP routes) -> fresh WebAuthn step-up -> single-use proof ->
ResultReceipt -> existing StateProofService CAS -> rebuilt lineage graph.

Needs PADOS_TEST_OPERATIONAL_DATABASE (operational lane at head) with PADOS_DB_NAME /
PADOS_PRODUCT_DATABASE_NAME naming the proof lane, both on loopback. Process launch for the
deterministic worker is replaced at the OS boundary only (same seam as the accepted P6.10 tests).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from pados.config import Settings, get_settings, lane_settings
from pados.control import service as control_service
from pados.loop import acceptance as result_acceptance
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, PlanStepCreate, StepType, TaskGoalCreate, TaskPlanCreate
from pados.memory.service import bootstrap_project
from pados.product_loop.state import P6_19BStateGateway
from pados.remote.auth import RemoteActorRegistry
from pados.remote.context import RemoteContextReconstructor
from pados.remote.deployment import check_remote_deployment
from pados.remote.jobs import RemoteJobController
from pados.remote.ledger import IntentLedger
from pados.remote.live import LiveRemotePorts
from pados.remote.routes import ACTOR_HEADER, PROOF_HEADER, register_remote_routes
from pados.remote.service import RemoteOperationsService
from pados.remote.staging import StagingArea
from pados.remote.step_up import SESSION_HEADER, PostgresStepUpStore, StepUpService
from pados.state_proof import ObjectStore, StateProofService
from pados.state_proof import repository as proof_repository
from pados.state_proof.service import build_proof_engine, read_scope_version
from pados.work_protocol.models import (
    CapabilityResult,
    Goal,
    GoalRelation,
    GoalRelationType,
    TaskEnvelope,
    WorkEdgeKind,
)
from pados.worker.schemas import CapabilityId, WorkerType
from tests.integration.test_residual_plan_amendment_postgres import _approve, _install_deterministic_router
from tests.remote.presence_support import SoftwareAuthenticator, verifier
from tests.remote.remote_slice_support import NoControl, hardened_settings

OPERATIONAL = os.environ.get("PADOS_TEST_OPERATIONAL_DATABASE", "")
pytestmark = pytest.mark.skipif(
    not OPERATIONAL, reason="DATABASE_INTEGRATION_SUITE: set PADOS_TEST_OPERATIONAL_DATABASE"
)
ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_INTENT = "Read the source notes, verify every reference, and resolve anything still missing."


@dataclass
class Live:
    settings: Settings
    op_engine: Engine
    proof: StateProofService
    ports: LiveRemotePorts
    service: RemoteOperationsService
    client: TestClient
    authenticator: SoftwareAuthenticator
    headers: dict[str, str]
    project_id: str
    tmp: Path


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Live]:
    base = get_settings()
    if base.db_host not in {"127.0.0.1", "localhost"} or not base.db_name or base.db_name == OPERATIONAL:
        pytest.fail("final convergence integration requires distinct loopback proof and operational lanes")
    settings = base.model_copy(update={
        "operational_database_name": OPERATIONAL,
        "product_database_name": base.db_name,
        "product_object_root": str(tmp_path / "objects"),
    })
    proof_engine = build_proof_engine(lane_settings(settings, settings.product_database_name).database_url)
    op_engine = build_proof_engine(lane_settings(settings, OPERATIONAL).database_url)
    proof = StateProofService(proof_engine, ObjectStore(tmp_path / "objects"))
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    with Session(op_engine) as session, session.begin():
        project_id = bootstrap_project(session, name=f"final-convergence-{uuid4()}", root_path=workspace,
                                       classification="internal")
    proof.ensure_scope(project_id)
    gateway = P6_19BStateGateway(proof, op_engine)
    ports = LiveRemotePorts(gateway=gateway, operational_engine=op_engine,
                            presentation_dir=tmp_path / "presentations")
    authenticator = SoftwareAuthenticator()
    access_value = uuid4().hex
    import hashlib

    registry = RemoteActorRegistry.from_mapping({"actors": [
        {"actor_id": "owner", "credential_hash": hashlib.sha256(access_value.encode()).hexdigest(),
         "projects": [project_id], "may_approve": True,
         "presence_credentials": [authenticator.descriptor("owner-phone")]},
    ]})
    service = RemoteOperationsService(
        registry=registry,
        reconstructor=RemoteContextReconstructor(semantic_source=lambda _p: None,
                                                 workspace_resolver=lambda _p: None),
        staging=StagingArea(tmp_path / "staging", project_ceiling=lambda _p: None),
        jobs=RemoteJobController(NoControl(ports)),  # type: ignore[arg-type]
        ledger=IntentLedger(tmp_path / "ledger" / "intents.jsonl"),
        ports=ports,
        step_up=StepUpService(PostgresStepUpStore(op_engine), verifier()),
    )
    app = FastAPI()
    hardened = hardened_settings()
    register_remote_routes(app, hardened, service=service,
                           deployment=check_remote_deployment(hardened, remote_bind_host="100.64.0.10"),
                           nodes=lambda: {"nodes": []})
    yield Live(settings, op_engine, proof, ports, service, TestClient(app), authenticator,
               {ACTOR_HEADER: "owner", PROOF_HEADER: access_value}, project_id, tmp_path)
    proof_engine.dispose()
    op_engine.dispose()


def _goal(goal_id: str, title: str, capability: str) -> Goal:
    return Goal(protocol_version="1.0", schema_version=Goal.SCHEMA, goal_id=goal_id, title=title,
                objective=title, required_capabilities=(capability,))


def _envelope(project_id: str, *, child_capability: str) -> TaskEnvelope:
    return TaskEnvelope(
        protocol_version="1.0", schema_version=TaskEnvelope.SCHEMA, task_id=f"task:{uuid4().hex}",
        original_intent=ORIGINAL_INTENT,
        normalized_goals=(_goal("goal-a", "Read source notes", "READ_REPOSITORY"),
                          _goal("goal-b", "Verify references", "READ_REPOSITORY"),
                          _goal("goal-b2", "Resolve the missing reference", child_capability)),
        goal_relations=(GoalRelation(protocol_version="1.0", schema_version=GoalRelation.SCHEMA,
                                     source_goal_id="goal-b", target_goal_id="goal-b2",
                                     relation=GoalRelationType.PARENT_OF),),
        project_ref=project_id, classification="INTERNAL", priority="NORMAL", deadline=None,
        scope_version_ref="scope:0", created_by="human:owner", created_at=datetime.now(UTC),
    )


def _step(sequence: int, goal_id: str, title: str, dependencies: list[int]) -> PlanStepCreate:
    return PlanStepCreate(
        sequence=sequence, product_goal_id=goal_id, title=title, step_type=StepType.INSPECT,
        required_capability=CapabilityId.READ_REPOSITORY,
        preferred_worker_types=[WorkerType.DETERMINISTIC_TOOL], dependencies=dependencies,
        allowed_paths=["docs/"], allowed_actions=["READ_REPOSITORY"], command_id="python_version",
        output_contract={"pados_capability_outcome": "pados.capability-outcome.v1"},
    )


def _run_task(live: Live, monkeypatch: pytest.MonkeyPatch, *, child_capability: str = "READ_REPOSITORY",
              outcomes: tuple[str, ...] = ("COMPLETE", "PARTIAL", "COMPLETE")) -> dict[str, Any]:
    _install_deterministic_router(monkeypatch, list(outcomes))
    envelope = _envelope(live.project_id, child_capability=child_capability)
    with Session(live.op_engine) as session, session.begin():
        request = control_service.create_work_request(session, {
            "project_id": live.project_id, "title": "Final convergence task",
            "body": json.dumps({"task_envelope": envelope.model_dump(mode="json"),
                                "task_envelope_hash": envelope.content_hash}, sort_keys=True),
            "idempotency_key": f"fc-request-{uuid4()}"})
        approval_id = _approve(session, live.project_id, uuid4().hex)
        goal = loop_service.create_task_goal(session, TaskGoalCreate(
            project_id=live.project_id, request_id=str(request["id"]),
            objective="Execute the approved task plan and report discrete goal outcomes.",
            success_criteria=["each goal has a normalized result"], approval_id=approval_id,
            idempotency_key=f"fc-goal-{uuid4()}"))
        plan = loop_service.create_task_plan(session, TaskPlanCreate(
            task_goal_id=str(goal["id"]), approval_id=approval_id,
            steps=[_step(1, "goal-a", "Read source notes", []), _step(2, "goal-b", "Verify references", [1])],
            idempotency_key=f"fc-plan-{uuid4()}"), settings=live.settings)
        loop = loop_service.create_loop(session, LoopCreate(
            task_goal_id=str(goal["id"]), active_plan_id=str(plan["id"]), approval_id=approval_id,
            max_iterations=6, idempotency_key=f"fc-loop-{uuid4()}"))
        loop_service.start_loop(session, str(loop["id"]), owner_instance="final-convergence")
    statuses = []
    for _ in range(len(outcomes)):
        with Session(live.op_engine) as session, session.begin():
            result = loop_service.run_one_iteration(
                session, loop_id=str(loop["id"]), settings=live.settings,
                artifact_root=live.tmp / "artifacts", idempotency_key=f"fc-run-{uuid4()}")
        statuses.append(result["status"])
    return {"envelope": envelope, "request_id": str(request["id"]), "loop_id": str(loop["id"]),
            "plan_id": str(plan["id"]), "approval_id": approval_id, "statuses": statuses}


def _submit(live: Live, loop_id: str) -> dict[str, Any]:
    with Session(live.op_engine) as session, session.begin():
        return result_acceptance.submit_result_candidate(
            session, loop_id, current_scope_version=read_scope_version(live.proof.engine, live.project_id))


def _step_up(live: Live, approval_ref: str, action: str = "APPROVE") -> tuple[dict[str, str], str]:
    session = live.client.post("/remote/v1/sessions", headers=live.headers)
    assert session.status_code == 200, session.text
    headers = {**live.headers, SESSION_HEADER: session.json()["session_handle"]}
    challenge = live.client.post(f"/remote/v1/approvals/{approval_ref}/step-up", headers=headers,
                                 json={"action": action})
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    verified = live.client.post(
        f"/remote/v1/approvals/{approval_ref}/step-up/{body['challenge_id']}/verify", headers=headers,
        json={"assertion": live.authenticator.assertion(body["public_key"])})
    assert verified.status_code == 200, verified.text
    return headers, verified.json()["proof_ref"]


def _decide(live: Live, headers: dict[str, str], approval_ref: str, run_id: str, proof: str | None,
            action: str = "approve") -> Any:
    body: dict[str, Any] = {"intent_id": f"fc-{action}-{uuid4().hex[:12]}", "project_id": live.project_id,
                            "target_run_id": run_id}
    if proof is not None:
        body["step_up_proof"] = proof
    return live.client.post(f"/remote/v1/approvals/{approval_ref}/{action}", headers=headers, json=body)


def _scope(live: Live) -> int:
    value = read_scope_version(live.proof.engine, live.project_id)
    assert value is not None
    return value


def _commits(live: Live) -> list[dict[str, Any]]:
    with live.proof._session() as session:
        return [dict(row) for row in session.execute(
            text("SELECT * FROM state_proof_commits WHERE scope_id = :s ORDER BY created_at"),
            {"s": live.project_id}).mappings()]


def test_end_to_end_product_loop_with_fresh_step_up_and_single_cas(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch)
    envelope: TaskEnvelope = task["envelope"]
    assert task["statuses"] == ["RUNNING", "RUNNING", "COMPLETED"]
    with Session(live.op_engine) as session:
        jobs = session.execute(text(
            "SELECT payload_json->>'goal_id' AS goal_id, count(*) AS n FROM task_loop_events "
            "WHERE loop_id = CAST(:l AS uuid) AND event_type = 'CAPABILITY_RESULT_NORMALIZED' "
            "GROUP BY payload_json->>'goal_id'"), {"l": task["loop_id"]}).mappings().all()
        job_count = session.execute(text("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:r AS uuid)"),
                                    {"r": task["request_id"]}).scalar_one()
        iterations = session.execute(text("SELECT count(*) FROM task_loop_iterations WHERE loop_id = CAST(:l AS uuid)"),
                                     {"l": task["loop_id"]}).scalar_one()
        amendment = session.execute(text("SELECT * FROM residual_plan_amendments WHERE loop_id = CAST(:l AS uuid)"),
                                    {"l": task["loop_id"]}).mappings().one()
    assert {row["goal_id"]: row["n"] for row in jobs} == {"goal-a": 1, "goal-b": 1, "goal-b2": 1}
    # One job per goal: completed Goal A and the completed part of Goal B never re-run;
    # the residual handles only B2; every job came through the one P6.10 scheduler.
    assert job_count == 3 and iterations == 3
    assert amendment["authority_decision"] == "AUTO_AMENDABLE" and amendment["goal_id"] == "goal-b2"
    assert _scope(live) == 0  # scheduler completion never commits

    candidate_row = _submit(live, task["loop_id"])
    assert candidate_row["verification_status"] == "PASSED"
    verification = json.loads(candidate_row["verification_json"]) if isinstance(
        candidate_row["verification_json"], str) else candidate_row["verification_json"]
    assert all(check["passed"] for check in verification["checks"])
    assert verification["semantic_authority"] is False
    candidate_id = candidate_row["candidate_id"]
    assert _scope(live) == 0  # verification never commits

    graph = live.client.get(f"/remote/v1/jobs/{candidate_id}/graph", headers=live.headers).json()
    assert graph["authority"] == "PROJECTION"
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    assert nodes[f"approval:{candidate_id}"]["status"] == "WAITING_APPROVAL"
    assert nodes[envelope.task_id]["status"] == "WAITING_APPROVAL"
    assert nodes["goal-b"]["status"] == "PARTIAL" and nodes["goal-b2"]["status"] == "COMPLETE"

    attention = live.client.get("/remote/v1/attention", headers=live.headers).json()
    waiting = [row for row in attention["waiting_approval"] if row["run_id"] == candidate_id]
    assert len(waiting) == 1
    approval_ref = waiting[0]["approval_ref"]
    detail = live.client.get(f"/remote/v1/jobs/{candidate_id}", headers=live.headers).json()
    assert detail["artifacts"][0]["candidate_digest"] == candidate_row["candidate_digest"]

    # A standing authenticated browser session is not enough.
    plain = live.client.post("/remote/v1/sessions", headers=live.headers).json()
    session_headers = {**live.headers, SESSION_HEADER: plain["session_handle"]}
    assert _decide(live, session_headers, approval_ref, candidate_id, None).status_code == 422
    assert _scope(live) == 0

    headers, proof = _step_up(live, approval_ref)
    decision = _decide(live, headers, approval_ref, candidate_id, proof)
    assert decision.status_code == 200, decision.text
    outcome = decision.json()
    assert outcome["outcome"] == "COMMITTED" and outcome["step_up_proof_ref"] == proof
    assert outcome["presence_method"] == "WEBAUTHN_USER_VERIFICATION"
    assert _scope(live) == 1

    # Replay of the consumed proof and a duplicate commit are both no-ops.
    replay = _decide(live, headers, approval_ref, candidate_id, proof)
    assert replay.status_code in {403, 422}
    duplicate = asyncio.run(result_acceptance.accept_result_candidate(
        operational_engine=live.op_engine, proof_service=live.proof, candidate_id=candidate_id,
        expected_version=0, actor_id="owner"))
    assert duplicate["status"] == "COMMITTED"
    commits = _commits(live)
    assert [c["status"] for c in commits] == ["COMMITTED"] and _scope(live) == 1

    # Canonical ResultReceipt: references only, bound to the approval proof and ScopeVersion.
    receipt = live.proof.get_result(duplicate["result_receipt_id"])
    assert receipt is not None and receipt["operation_type"] == "RESULT_ACCEPTANCE"
    with live.proof._session() as session:
        manifest_object = proof_repository.get_object(session, str(receipt["input_manifest_object_id"]))
    assert manifest_object is not None
    manifest = json.loads(Path(manifest_object["storage_uri"]).read_text(encoding="utf-8"))
    spec = manifest["specification"]
    assert spec["schema_version"] == "pados.result-receipt.v1"
    assert spec["candidate_id"] == candidate_id and spec["candidate_digest"] == candidate_row["candidate_digest"]
    assert spec["task_envelope_hash"] == envelope.content_hash
    assert spec["approval"]["proof_ref"] == proof and spec["approval"]["actor_id"] == "owner"
    assert spec["approval"]["presence_method"] == "WEBAUTHN_USER_VERIFICATION"
    with Session(live.op_engine) as session:
        stored_digest = session.execute(text(
            "SELECT assertion_digest FROM approval_step_up_challenges WHERE proof_id = CAST(:p AS uuid)"),
            {"p": proof.removeprefix("step-up-proof:")}).scalar_one()
    assert spec["approval"]["assertion_digest"] == stored_digest
    assert spec["expected_scope_version_ref"] == "scope:0" and spec["classification"] == "INTERNAL"
    assert spec["verification"]["digest"] == candidate_row["verification_digest"]
    assert len(spec["execution_receipts"]) == 3 and len(spec["capability_results"]) == 3
    assert "original_intent" not in json.dumps(spec)  # no prompt/context payload in the receipt
    assert manifest["scope_version"] == 0

    # Audit chain complete and intact; the commit names the step-up proof.
    assert live.proof.verify_semantic_audit_chain(live.project_id)
    with live.proof._session() as session:
        records = proof_repository.semantic_audit_records(session, live.project_id)
    assert [r["action"] for r in records] == ["SEMANTIC_COMMIT"]
    assert records[0]["approval_ref"] == proof and records[0]["actor"] == "REMOTE:owner"

    # Rebuilt graph shows the complete accepted lineage, deterministically.
    graph = live.client.get(f"/remote/v1/jobs/{candidate_id}/graph", headers=live.headers).json()
    again = live.client.get(f"/remote/v1/jobs/{candidate_id}/graph", headers=live.headers).json()
    assert graph["projection_id"] == again["projection_id"]
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    edges = {(e["source_id"], e["kind"], e["target_id"]) for e in graph["edges"]}
    receipt_node = f"receipt:{duplicate['result_receipt_id']}"
    state_node = f"semantic:{live.project_id}:1"
    assert nodes[envelope.task_id]["status"] == "ACCEPTED"
    assert nodes[f"decision:{candidate_id}"]["status"] == "ACCEPTED"
    assert nodes[f"verification:{candidate_id}"]["status"] == "PASSED"
    assert nodes[f"approval:{candidate_id}"]["status"] == "APPROVED"
    assert nodes[state_node]["status"] == "ACCEPTED"
    residual_id = str(amendment["residual_id"])
    assert (residual_id, WorkEdgeKind.RESIDUAL_OF.value, "goal-b") in edges
    assert any(k == "AMENDMENT" for k in (n["kind"] for n in graph["nodes"]))
    assert sum(1 for n in graph["nodes"] if n["kind"] == "RECEIPT") >= 4  # 3 execution + 1 result receipt
    for edge in [
        (envelope.task_id, "CONTAINS", "goal-a"),
        (envelope.task_id, "DERIVED_FROM", f"decision:{candidate_id}"),
        (f"decision:{candidate_id}", "VERIFIED_BY", f"verification:{candidate_id}"),
        (f"verification:{candidate_id}", "PRODUCED", f"approval:{candidate_id}"),
        (f"approval:{candidate_id}", "PRODUCED", receipt_node),
        (receipt_node, "PRODUCED", state_node),
    ]:
        assert edge in edges, edge
    with Session(live.op_engine) as session:
        rebuilt = result_acceptance.rebuild_task_work_graph(session, live.proof, task["loop_id"])
    assert rebuilt.projection_id == graph["projection_id"]
    assert rebuilt.authority == "PROJECTION"


def test_candidate_substitution_after_proof_is_denied(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch)
    row = _submit(live, task["loop_id"])
    headers, proof = _step_up(live, str(row["approval_id"]))
    # The sealed results change after the proof was minted (an extra normalized result appears).
    with Session(live.op_engine) as session, session.begin():
        event = session.execute(text(
            "SELECT payload_json FROM task_loop_events WHERE loop_id = CAST(:l AS uuid) "
            "AND event_type = 'CAPABILITY_RESULT_NORMALIZED' ORDER BY sequence_number LIMIT 1"),
            {"l": task["loop_id"]}).scalar_one()
        payload = dict(event)
        changed = CapabilityResult.model_validate(
            {**payload["capability_result"], "result_id": "result:substituted", "metrics": {"edited": True}})
        payload.update(capability_result=changed.model_dump(mode="json"), capability_result_hash=changed.content_hash)
        session.execute(text(
            "INSERT INTO task_loop_events(loop_id, project_id, event_type, payload_json) VALUES "
            "(CAST(:l AS uuid), CAST(:p AS uuid), 'CAPABILITY_RESULT_NORMALIZED', CAST(:j AS jsonb))"),
            {"l": task["loop_id"], "p": live.project_id, "j": json.dumps(payload)})
    denied = _decide(live, headers, str(row["approval_id"]), row["candidate_id"], proof)
    assert denied.status_code in {403, 422}
    assert _scope(live) == 0 and _commits(live) == []


def test_stale_scope_version_after_proof_is_denied(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch)
    row = _submit(live, task["loop_id"])
    headers, proof = _step_up(live, str(row["approval_id"]))

    class _Other:
        async def synthesize(self, _specification: Any) -> str:
            return json.dumps({"title": "other", "answer": "another accepted change"})

    asyncio.run(live.proof.execute(scope_id=live.project_id, operation_id=f"other-{uuid4()}",
                                   specification={}, context_receipt={}, provider=_Other()))
    assert _scope(live) == 1
    decision = _decide(live, headers, str(row["approval_id"]), row["candidate_id"], proof)
    assert decision.status_code == 200 and decision.json()["outcome"] == "SCOPE_VERSION_CHANGED"
    assert _scope(live) == 1
    assert all(c["result_id"] != row["candidate_id"] for c in _commits(live))
    with Session(live.op_engine) as session:
        state = session.execute(text("SELECT status FROM approval_step_up_challenges WHERE proof_id = CAST(:p AS uuid)"),
                                {"p": proof.removeprefix("step-up-proof:")}).scalar_one()
    assert state == "CONSUMED"


def test_rejection_preserves_evidence_audits_and_never_commits(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch)
    row = _submit(live, task["loop_id"])
    with Session(live.op_engine) as session:
        events_before = session.execute(text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:l AS uuid)"),
                                        {"l": task["loop_id"]}).scalar_one()
    headers, proof = _step_up(live, str(row["approval_id"]), action="REJECT")
    approve_with_reject_proof = _decide(live, headers, str(row["approval_id"]), row["candidate_id"], proof)
    assert approve_with_reject_proof.status_code == 403
    assert approve_with_reject_proof.json()["detail"] == "APPROVAL_PROOF_ACTION_MISMATCH"
    rejected = _decide(live, headers, str(row["approval_id"]), row["candidate_id"], proof, action="reject")
    assert rejected.status_code == 200 and rejected.json()["outcome"] == "REJECTED"
    assert _scope(live) == 0 and _commits(live) == []
    with live.proof._session() as session:
        records = proof_repository.semantic_audit_records(session, live.project_id)
    assert [r["action"] for r in records] == ["SEMANTIC_REJECT"]
    assert records[0]["approval_ref"] == proof and records[0]["after_state"]["semantic_state"] == "UNCHANGED"
    assert live.proof.verify_semantic_audit_chain(live.project_id)
    with Session(live.op_engine) as session:
        events_after = session.execute(text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:l AS uuid)"),
                                       {"l": task["loop_id"]}).scalar_one()
        decision = session.execute(text("SELECT * FROM result_acceptance_decisions WHERE candidate_id = :c"),
                                   {"c": row["candidate_id"]}).mappings().one()
        graph = result_acceptance.rebuild_task_work_graph(session, live.proof, task["loop_id"])
    assert events_after == events_before  # execution history is never rewritten
    assert decision["decision"] == "REJECT" and decision["next_action"] == "REQUEST_REPLAN"
    nodes = {node.node_id: node for node in graph.nodes}
    assert nodes[f"approval:{row['candidate_id']}"].status == "REJECTED"
    assert nodes[f"approval:{row['candidate_id']}"].metadata["next_action"] == "REQUEST_REPLAN"
    for statement in ("UPDATE result_acceptance_candidates SET task_id = 'x'",
                      "DELETE FROM result_acceptance_decisions"):
        with Session(live.op_engine) as session, pytest.raises(DBAPIError):
            session.execute(text(statement))
    # The rejected candidate can no longer be approved.
    again_headers = {**live.headers, SESSION_HEADER: live.client.post(
        "/remote/v1/sessions", headers=live.headers).json()["session_handle"]}
    late = live.client.post(f"/remote/v1/approvals/{row['approval_id']}/step-up", headers=again_headers,
                            json={"action": "APPROVE"})
    assert late.status_code in {403, 422}


def test_authority_expanding_residual_requires_reapproval_and_never_executes(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch, child_capability="MODIFY_CODE", outcomes=("COMPLETE", "PARTIAL"))
    assert task["statuses"] == ["RUNNING", "WAITING_HUMAN"]
    with Session(live.op_engine) as session:
        amendment = session.execute(text("SELECT * FROM residual_plan_amendments WHERE loop_id = CAST(:l AS uuid)"),
                                    {"l": task["loop_id"]}).mappings().one()
        steps = session.execute(text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:p AS uuid)"),
                                {"p": task["plan_id"]}).scalar_one()
        jobs = session.execute(text("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:r AS uuid)"),
                               {"r": task["request_id"]}).scalar_one()
    assert amendment["authority_decision"] == "REAPPROVAL_REQUIRED" and amendment["step_id"] is None
    assert "CAPABILITY_EXPANSION" in amendment["reason_codes"]
    assert steps == 2 and jobs == 2  # no executable step was created for the residual
    with Session(live.op_engine) as session, session.begin(), pytest.raises(PermissionError):
        result_acceptance.submit_result_candidate(session, task["loop_id"], current_scope_version=0)
    assert _scope(live) == 0


def test_direct_commit_paths_without_consumed_proof_are_refused(live: Live, monkeypatch) -> None:
    task = _run_task(live, monkeypatch)
    row = _submit(live, task["loop_id"])
    with pytest.raises(PermissionError, match="RESULT_ACCEPTANCE_REQUIRES_RECORDED_APPROVAL"):
        asyncio.run(result_acceptance.accept_result_candidate(
            operational_engine=live.op_engine, proof_service=live.proof, candidate_id=row["candidate_id"],
            expected_version=0, actor_id="owner"))
    with Session(live.op_engine) as session, session.begin(), pytest.raises(PermissionError):
        result_acceptance.record_candidate_decision(
            session, candidate_id=row["candidate_id"], decision="APPROVE", actor_id="owner",
            session_id="rs-" + "0" * 32, proof_id=str(uuid4()), binding_hash="a" * 64,
            step_up_binding_digest="b" * 64)
    forged = live.client.post(
        f"/remote/v1/approvals/{row['approval_id']}/approve", headers={
            **live.headers, SESSION_HEADER: live.client.post("/remote/v1/sessions", headers=live.headers).json()[
                "session_handle"]},
        json={"intent_id": "forged", "project_id": live.project_id, "target_run_id": row["candidate_id"],
              "step_up_proof": f"step-up-proof:{uuid4()}"})
    assert forged.status_code == 403 and forged.json()["detail"] == "APPROVAL_PROOF_NOT_FOUND"
    assert _scope(live) == 0 and _commits(live) == []


def test_single_cas_entrypoint_and_projection_modules_cannot_commit() -> None:
    """Static boundary: only the known gateways call the one CAS entrypoint."""
    allowed = {
        "src/pados/state_proof/service.py",
        "src/pados/product_loop/state.py",
        "src/pados/remote/live.py",
        "src/pados/loop/acceptance.py",
    }
    callers = set()
    for path in (ROOT / "src" / "pados").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "commit_result":
                callers.add(path.relative_to(ROOT).as_posix())
    assert callers <= allowed and "src/pados/state_proof/repository.py" not in callers
    writers = set()
    for path in (ROOT / "src" / "pados").rglob("*.py"):
        if "state_proof_scope_heads" in path.read_text(encoding="utf-8-sig") and "UPDATE state_proof_scope_heads" in \
                path.read_text(encoding="utf-8-sig"):
            writers.add(path.relative_to(ROOT).as_posix())
    assert writers == {"src/pados/state_proof/repository.py"}
    for module in ("work_protocol/graph.py", "work_protocol/composition.py", "work_protocol/acceptance.py",
                   "loop/service.py", "providers", "compute_node"):
        target = ROOT / "src" / "pados" / module
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"commit_result", "commit"} or "session" in ast.unparse(
                        node.func.value), (path, ast.unparse(node))
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("pados.state_proof"), (path, node.module)


def test_consumed_proof_does_not_authorize_a_late_acceptance(live: Live, monkeypatch) -> None:
    from datetime import timedelta

    task = _run_task(live, monkeypatch)
    row = _submit(live, task["loop_id"])
    headers, proof = _step_up(live, str(row["approval_id"]))
    monkeypatch.setattr(result_acceptance, "ACCEPTANCE_WINDOW", timedelta(seconds=-1))
    decision = _decide(live, headers, str(row["approval_id"]), row["candidate_id"], proof)
    assert decision.status_code == 200 and decision.json()["outcome"] == "COMMIT_CONFLICT"
    assert _scope(live) == 0 and _commits(live) == []
    with pytest.raises(PermissionError, match="RESULT_ACCEPTANCE_WINDOW_EXPIRED"):
        asyncio.run(result_acceptance.accept_result_candidate(
            operational_engine=live.op_engine, proof_service=live.proof, candidate_id=row["candidate_id"],
            expected_version=0, actor_id="owner"))
    with Session(live.op_engine) as session:
        graph = result_acceptance.rebuild_task_work_graph(session, live.proof, task["loop_id"])
    assert {node.node_id: node.status for node in graph.nodes}[task["envelope"].task_id] == "APPROVED_NOT_COMMITTED"
