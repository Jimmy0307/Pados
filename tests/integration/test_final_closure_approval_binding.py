"""Optional product-loop approval_ref is verified, bound and audited; never faked."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.config import get_settings
from pados.control import service as control_service
from pados.db import get_engine, session_scope
from pados.product_loop.authorization import ProductAuthorizationError
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


def _approval(project_id: str, *, decide: bool = True) -> str:
    key = f"approval-binding-{uuid4()}"
    with session_scope() as session:
        row = control_service.create_approval(session, {"project_id": project_id, "proposal_hash": "c" * 64,
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
    approval_id = _approval(scope_id)
    report = asyncio.run(service.execute(_with_approval(
        _request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n"), approval_id)))
    assert report.status.value == "COMMITTED"
    assert report.contracts[0].approval_ref == approval_id
    assert _audit(engine, scope_id) == [{"approval_ref": approval_id, "rollback_ref": None}]


@pytest.mark.parametrize("case", ["foreign_project", "not_approved", "malformed"])
def test_unverifiable_approval_fails_closed_before_dispatch(p6_19b_product_service, case: str) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    other = _registered_scope(workspace.parent / f"other-{uuid4()}")
    approval_ref = {"foreign_project": _approval(other), "not_approved": _approval(scope_id, decide=False),
                    "malformed": "not-a-uuid"}[case]
    request = _with_approval(_request(workspace, scope_id=scope_id, content="def result():\n    return 'good'\n"),
                             approval_ref)
    with pytest.raises(ProductAuthorizationError):
        asyncio.run(service.execute(request))
    assert _audit(engine, scope_id) == []


def test_omitted_approval_keeps_existing_contract(p6_19b_product_service) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    report = asyncio.run(service.execute(_request(workspace, scope_id=scope_id,
                                                  content="def result():\n    return 'good'\n")))
    assert report.status.value == "COMMITTED"
    assert _audit(engine, scope_id) == [{"approval_ref": None, "rollback_ref": None}]
