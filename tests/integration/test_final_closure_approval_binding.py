"""Product-loop approval_ref is required, verified against this exact operation, bound and audited (RC2 ARCH-1)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.config import get_settings
from pados.control import service as control_service
from pados.db import get_engine, session_scope
from pados.product_loop.authorization import ProductAuthorizationError, product_dispatch_hash
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.state_proof import ObjectStore, StateProofService
from tests.integration.test_p6_21g_state_integration import _registered_scope, _request


@pytest.fixture
def p6_19b_product_service(tmp_path: Path):
    engine = get_engine(get_settings())
    service = ProductLoopService(
        maker=FileDirectMaker(tmp_path / "workspace", protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES"))),
        validator=FileValidator(),
        state_gateway=P6_19BStateGateway(StateProofService(engine, ObjectStore(tmp_path / "objects"))),
    )
    yield service, engine
    engine.dispose()


def _approval(project_id: str, request: GoalRequest | None = None, *, decide: bool = True) -> str:
    """An approval in ``project_id``; it names ``request``'s exact operation, or an unrelated proposal."""
    key = f"approval-binding-{uuid4()}"
    proposal_hash = product_dispatch_hash(request) if request is not None else "c" * 64
    with session_scope() as session:
        row = control_service.create_approval(session, {"project_id": project_id, "proposal_hash": proposal_hash,
                                                        "proposal_summary": key, "idempotency_key": key})
        if decide:
            row = control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED",
                                                  reason="integration", idempotency_key=f"decide-{key}")
        return str(row["id"])


def _with_approval(request: GoalRequest, approval_ref: str | None) -> GoalRequest:
    return GoalRequest.model_validate({**request.model_dump(mode="json"), "approval_ref": approval_ref})


def _audit(engine, scope_id: str) -> list[dict]:
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(text(
            "SELECT approval_ref, rollback_ref FROM state_proof_semantic_audit WHERE scope_id = :s ORDER BY sequence_id"),
            {"s": scope_id}).mappings()]


def test_verified_approval_is_bound_into_contract_and_audit(p6_19b_product_service) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    request = _request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n")
    approval_id = _approval(scope_id, request)
    report = asyncio.run(service.execute(_with_approval(request, approval_id)))
    assert report.status.value == "COMMITTED"
    assert report.contracts[0].approval_ref == approval_id
    assert _audit(engine, scope_id) == [{"approval_ref": approval_id, "rollback_ref": None}]


@pytest.mark.parametrize("case", ["foreign_project", "not_approved", "malformed", "other_operation"])
def test_unverifiable_approval_fails_closed_before_dispatch(p6_19b_product_service, case: str) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    other = _registered_scope(workspace.parent / f"other-{uuid4()}")
    request = _request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n")
    approval_ref = {"foreign_project": lambda: _approval(other, request),
                    "not_approved": lambda: _approval(scope_id, request, decide=False),
                    "malformed": lambda: "not-a-uuid",
                    # An APPROVED approval in this project that names a different proposal (RC2 ARCH-1).
                    "other_operation": lambda: _approval(scope_id)}[case]()
    with pytest.raises(ProductAuthorizationError):
        asyncio.run(service.execute(_with_approval(request, approval_ref)))
    assert _audit(engine, scope_id) == []


def test_omitted_approval_never_dispatches_or_commits(p6_19b_product_service) -> None:
    """RC2 ARCH-1 / SEC-1: ``authorized=True`` without a verified approval is a request, not authority."""
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    report = asyncio.run(service.execute(_request(workspace, scope_id=scope_id,
                                                  content="def result():\n    return 'good'\n")))
    assert report.status.value == "AUTHORITY_BLOCK"
    assert report.attempts == []
    assert _audit(engine, scope_id) == []


def test_approval_revoked_during_the_attempt_stops_the_cas(p6_19b_product_service) -> None:
    """RC2 ARCH-1: the approval is verified again immediately before the semantic CAS commit."""
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    request = _request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n")
    approval_id = _approval(scope_id, request)
    inner = service.maker.make

    async def make_then_reject(contract, *, run_id, attempt):
        result = await inner(contract, run_id=run_id, attempt=attempt)
        with session_scope() as session:
            session.execute(text("UPDATE control_approvals SET status = 'REJECTED' WHERE id = CAST(:a AS uuid)"),
                            {"a": approval_id})
        return result

    service.maker.make = make_then_reject
    report = asyncio.run(service.execute(_with_approval(request, approval_id)))
    assert report.status.value == "AUTHORITY_BLOCK"
    assert report.semantic_state == "NOT_COMMITTED"
    assert _audit(engine, scope_id) == []


def test_gateway_reads_the_registered_project_classification(p6_19b_product_service) -> None:
    """RC2 SEC-2: the cloud Maker's floor is the project's recorded classification; unknown ids read as None."""
    service, engine = p6_19b_product_service
    scope_id = _registered_scope(Path(service.maker.approved_root))
    gateway = service.state_gateway
    assert gateway.project_classification(scope_id) == "internal"  # the PADOS default for a registered project
    with engine.begin() as connection:
        connection.execute(text("UPDATE projects SET classification = 'confidential' WHERE id = CAST(:p AS uuid)"),
                           {"p": scope_id})
    assert gateway.project_classification(scope_id) == "confidential"
    assert gateway.project_classification(str(uuid4())) is None
    assert gateway.project_classification("not-a-uuid") is None


def test_a_committed_approval_cannot_dispatch_again_after_a_restart(p6_19b_product_service, tmp_path: Path) -> None:
    """Review F1: after a restart the in-process idempotency cache is empty; the committed approval is not reusable."""
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    request = _request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n")
    approved = _with_approval(request, _approval(scope_id, request))
    assert asyncio.run(service.execute(approved)).status.value == "COMMITTED"
    calls: list[int] = []
    inner = service.maker.make

    async def counting(contract, *, run_id, attempt):
        calls.append(attempt)
        return await inner(contract, run_id=run_id, attempt=attempt)

    restarted = ProductLoopService(maker=service.maker, validator=FileValidator(), state_gateway=P6_19BStateGateway(
        StateProofService(engine, ObjectStore(tmp_path / "objects-restarted"))))
    restarted.maker.make = counting
    with pytest.raises(ProductAuthorizationError, match="APPROVAL_CONSUMED"):
        asyncio.run(restarted.execute(approved))
    assert calls == []
    assert len(_audit(engine, scope_id)) == 1
