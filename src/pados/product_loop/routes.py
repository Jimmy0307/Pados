# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from pados.config import Settings, lane_settings
from pados.control import security as control_security
from pados.product_loop.authorization import (
    AUTHENTICATION_FAILED,
    PRODUCT_REQUEST_REJECTED,
    PRODUCT_RUN_NOT_FOUND,
    PRODUCT_SCOPE_HEADER,
    ProductAuthorizationError,
    assert_scope_matches,
    require_declared_scope,
    resolve_approved_workspace_root,
)
from pados.product_loop.codex_provider import CodexCliMaker
from pados.product_loop.disclosure import ProductReadView, build_read_view
from pados.product_loop.schemas import GoalRequest
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.state_proof import ObjectStore, StateProofService
from pados.state_proof.service import build_proof_engine

PRODUCT_READ_UNAVAILABLE = "PRODUCT_READ_UNAVAILABLE"

ScopeHeader = Annotated[str | None, Header(alias=PRODUCT_SCOPE_HEADER)]


def register_product_loop_routes(
    app: FastAPI,
    settings: Settings,
    *,
    service: ProductLoopService | None = None,
) -> None:
    if service is None:
        try:
            active_service = build_product_loop_service(settings)
        except PermissionError:
            return
    else:
        active_service = service

    def local_guard(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="product loop surface is local-only")
        if settings.api_host not in {"127.0.0.1", "localhost"}:
            raise HTTPException(status_code=503, detail="product loop requires localhost binding")

    def control_auth(
        x_pados_control_token: Annotated[
            str | None, Header(alias=control_security.CONTROL_TOKEN_HEADER)
        ] = None,
    ) -> None:
        """Authenticate a trusted local control-plane caller.

        This reuses the existing PADOS control-token mechanism unchanged. It
        proves neither user identity nor semantic authority, and it never
        substitutes for the Executable Work Contract or the CAS scope version.
        """
        try:
            control_security.authorize_control_request(settings, x_pados_control_token)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=AUTHENTICATION_FAILED) from exc

    # Ordered gates: network/feature gate, then authentication. FastAPI solves
    # route dependencies before the request body is validated, so an
    # unauthenticated caller never reaches contract parsing, the Direct Maker,
    # or any durable write.
    gates = [Depends(local_guard), Depends(control_auth)]

    @app.post("/product-loop/runs", dependencies=gates)
    async def create_product_loop_run(
        payload: GoalRequest,
        x_pados_product_scope: ScopeHeader = None,
    ) -> dict[str, Any]:
        # RESOURCE / PROJECT SCOPE VALIDATION
        declared_scope = _authorize_request_scope(payload, x_pados_product_scope)
        # WORKSPACE ROOT VALIDATION (defense in depth; the service and the
        # Direct Maker keep their own independent checks)
        _authorize_workspace_root(settings, payload, active_service)
        try:
            report = await active_service.execute(payload)
        except ProductAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=PRODUCT_REQUEST_REJECTED) from exc
        # Re-verify the binding server-side before projecting. Submission may be
        # answered from an idempotent replay, so the run that is about to be
        # disclosed must itself belong to the declared scope.
        return _authorized_run_view(active_service, report.run_id, declared_scope)

    @app.get("/product-loop/runs/{run_id}", dependencies=gates)
    def get_product_loop_run(
        run_id: str,
        x_pados_product_scope: ScopeHeader = None,
    ) -> dict[str, Any]:
        # RESOURCE EXISTS
        recorded_scope = _lookup_run_scope(active_service, run_id)
        if recorded_scope is None:
            raise HTTPException(status_code=404, detail=PRODUCT_RUN_NOT_FOUND)
        # PROJECT / SCOPE BINDING: a run identifier alone is never sufficient.
        try:
            declared_scope = assert_scope_matches(
                declared=x_pados_product_scope,
                actual=recorded_scope,
            )
        except ProductAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        # SAFE READ MODEL PROJECTION
        return _run_view(active_service, run_id, declared_scope).model_dump(mode="json")


def _authorized_run_view(
    service: ProductLoopService, run_id: str, declared_scope: str
) -> dict[str, Any]:
    try:
        assert_scope_matches(declared=declared_scope, actual=service.read_run_scope(run_id))
    except ProductAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    return _run_view(service, run_id, declared_scope).model_dump(mode="json")


def _authorize_request_scope(payload: GoalRequest, declared: str | None) -> str:
    """Bind one submission to an explicitly declared bounded scope.

    The transport-declared scope and the contract scope must agree, so a caller
    cannot submit work into a scope it did not declare.
    """
    try:
        requested = require_declared_scope(declared)
        return assert_scope_matches(declared=requested, actual=payload.scope_id)
    except ProductAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc


def _authorize_workspace_root(
    settings: Settings,
    payload: GoalRequest,
    service: ProductLoopService,
) -> None:
    approved = service.approved_workspace_root or (settings.product_workspace_root or None)
    if approved is None:
        return
    try:
        resolve_approved_workspace_root(payload.workspace_root, approved)
    except ProductAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc


def _lookup_run_scope(service: ProductLoopService, run_id: str) -> str | None:
    try:
        UUID(run_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return service.read_run_scope(run_id)


def _run_view(service: ProductLoopService, run_id: str, scope_id: str) -> ProductReadView:
    read = service.read_run(run_id)
    if read is None:
        raise HTTPException(status_code=404, detail=PRODUCT_RUN_NOT_FOUND)
    return build_read_view(read, scope_id=scope_id)


def build_product_loop_service(settings: Settings) -> ProductLoopService:
    if not settings.product_workspace_root:
        raise PermissionError("PRODUCT_WORKSPACE_ROOT_REQUIRED")
    if not settings.product_object_root:
        raise PermissionError("PRODUCT_OBJECT_ROOT_REQUIRED")
    if not settings.product_database_name:
        raise PermissionError("PRODUCT_DATABASE_NAME_REQUIRED")
    configured_workspace = Path(settings.product_workspace_root).expanduser()
    configured_object = Path(settings.product_object_root).expanduser()
    if not configured_workspace.is_absolute():
        raise PermissionError("PRODUCT_WORKSPACE_ROOT_MUST_BE_ABSOLUTE")
    if not configured_object.is_absolute():
        raise PermissionError("PRODUCT_OBJECT_ROOT_MUST_BE_ABSOLUTE")
    workspace_root = configured_workspace.resolve()
    object_root = configured_object.resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    object_root.mkdir(parents=True, exist_ok=True)
    if settings.product_database_name == settings.operational_database_name:
        # P6.15 separation: the proof/state substrate is its own database.
        raise PermissionError("PROOF_DATABASE_MUST_BE_SEPARATE")
    if settings.db_host not in {"127.0.0.1", "localhost"}:
        raise PermissionError("PRODUCT_DATABASE_MUST_BE_LOCAL")
    proof_engine = build_proof_engine(lane_settings(settings, settings.product_database_name).database_url)
    operational_engine = build_proof_engine(lane_settings(settings, settings.operational_database_name).database_url)
    proof_service = StateProofService(proof_engine, ObjectStore(object_root))
    from pados.connectivity.provider_gate import gate_product_loop_maker

    return ProductLoopService(
        # Requirement-aware connectivity gate only when PADOS_CONNECTIVITY_CONFIG is set.
        maker=gate_product_loop_maker(CodexCliMaker(workspace_root, settings=settings), settings),
        validator=FileValidator(),
        state_gateway=P6_19BStateGateway(proof_service, operational_engine),
        approved_workspace_root=workspace_root,
    )
