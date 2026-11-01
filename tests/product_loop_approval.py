"""Test stand-in for the human approval a Product Loop dispatch needs (RC2 ARCH-1 / SEC-1).

``GoalRequest.authorized`` only asks for dispatch. The service dispatches when the state gateway verifies an
APPROVED approval whose proposal hash names exactly the request's operation. These helpers record that approval in
the test gateway (in memory) or through the existing control-approval service (PostgreSQL), and return the request
carrying its ``approval_ref``.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from pados.product_loop.authorization import product_dispatch_hash
from pados.product_loop.schemas import GoalRequest


def approved(request: GoalRequest, gateway: Any) -> GoalRequest:
    """Approve exactly this operation in an ``InMemoryP6_19BStateGateway``; requests not asking for dispatch pass."""
    if not request.authorized or request.approval_ref is not None:
        return request
    ref = gateway.grant_approval(request.scope_id, product_dispatch_hash(request))
    return request.model_copy(update={"approval_ref": ref})


def approved_payload(payload: dict[str, Any], gateway: Any) -> dict[str, Any]:
    """``approved`` for a JSON request body submitted through the HTTP route."""
    request = approved(GoalRequest.model_validate(payload), gateway)
    return {**payload, "idempotency_key": request.idempotency_key, "approval_ref": request.approval_ref}


def approved_in_database(request: GoalRequest) -> GoalRequest:
    """Approve exactly this operation through the existing control-approval service (operational database)."""
    if not request.authorized or request.approval_ref is not None:
        return request
    from pados.control import service as control_service
    from pados.db import session_scope

    key = f"product-dispatch-{uuid4()}"
    with session_scope() as session:
        row = control_service.create_approval(session, {
            "project_id": request.scope_id, "proposal_hash": product_dispatch_hash(request),
            "proposal_summary": key, "idempotency_key": key})
        row = control_service.decide_approval(session, str(row["id"]), version=int(row["version"]),
                                              status="APPROVED", reason="test approval of this exact operation",
                                              idempotency_key=f"decide-{key}")
    return request.model_copy(update={"approval_ref": str(row["id"])})
