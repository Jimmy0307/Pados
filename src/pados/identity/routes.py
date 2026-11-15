# SPDX-License-Identifier: AGPL-3.0-only
"""``/identity/v1`` (design §3.5). JSON; refusals use the ``pados.error.v1`` envelope with a closed code.

Minting a pairing code needs the control token header (a session cannot mint one). ``/pair`` needs a code and an
allowlisted ``Origin``. ``GET /session`` is the only route that answers while the feature flag is off. The pairing
code appears in exactly one response: the mint response.

Phase 2 (design §4) mounts the OIDC routes (``pados.identity.oidc.routes``) and the actor surface
(``pados.identity.actor_routes``) here. ``GET /session`` keeps its phase 1 meaning (``authenticated`` is the device
session, the only one the control plane accepts), lists the configured providers, and reports a presented actor
session separately under ``actor`` (``auth_method: "oidc"``; absent when there is none; its CSRF token comes from
``GET /me``). ``POST /logout``
ends a device session, or an actor session when no device cookie is sent.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pados.config import Settings
from pados.control import security as control_security
from pados.identity import actor_http
from pados.identity import http as identity_http
from pados.identity.actor_routes import mount_actor_routes
from pados.identity.actor_sessions import ActorSessions
from pados.identity.errors import IDENTITY_DISABLED, IDENTITY_STORE_CORRUPT, SESSION_REQUIRED, IdentityError
from pados.identity.oidc.flow import OidcRuntime
from pados.identity.oidc.routes import mount_oidc_routes
from pados.identity.sessions import ActiveSession, IdentityService
from pados.identity.tokens import display_code

PREFIX = "/identity/v1"


class PairingCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=256)


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairing_code: str = Field(min_length=1, max_length=64)
    device_label: str | None = Field(default=None, max_length=256)


def _session_body(service: IdentityService, active: ActiveSession) -> dict[str, Any]:
    return {"enabled": True, "authenticated": True, "device_id": active.device.device_id,
            "device_label": active.device.label, "auth_method": "device_pairing",
            "expires_at": active.record.expires_at.isoformat(),
            "idle_expires_at": active.idle_expires_at.isoformat(),
            "csrf_token": service.csrf_for(active), "providers": []}


def _providers(request: Request) -> list[dict[str, str]]:
    runtime = actor_http.runtime_for(request)
    return runtime.public_providers() if runtime is not None else []


def _actor_summary(settings: Settings, request: Request, service: IdentityService) -> dict[str, Any] | None:
    """The actor session presented with this request, still bound; ``None`` when there is none."""
    if actor_http.presented_actor_session(settings, request) is None:
        return None
    try:
        actor = actor_http.authorize_actor(settings, request, service)
    except HTTPException as exc:
        if exc.detail == IDENTITY_STORE_CORRUPT:
            raise
        return None
    record = actor.session.record
    return {"auth_method": record.auth_method, "actor_id": record.actor_id, "provider_id": record.provider_id,
            "issuer": record.issuer, "subject": record.subject, "expires_at": record.expires_at.isoformat(),
            "idle_expires_at": actor.session.idle_expires_at.isoformat()}


def mount_identity(app: FastAPI, settings: Settings, *, service: IdentityService | None = None,
                   oidc: OidcRuntime | None = None,
                   create_work_request: Callable[[Any], object] | None = None) -> None:
    app.state.pados_identity = service or IdentityService(settings)
    mount_oidc_routes(app, settings, runtime=oidc)
    mount_actor_routes(app, settings, create_work_request=create_work_request)

    def active_service(request: Request) -> IdentityService:
        found = identity_http.service_for(request)
        if found is None or not found.enabled:
            identity_http.refuse(IDENTITY_DISABLED)
        return found

    def control_token(control_value: str | None) -> None:
        try:
            control_security.authorize_control_request(settings, control_value)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc), headers=identity_http.NO_STORE) from exc

    def token_or_session(request: Request, service: IdentityService) -> ActiveSession | None:
        """The control token header alone decides when present (no fallback); otherwise the session cookie."""
        header = request.headers.get(control_security.CONTROL_TOKEN_HEADER)
        if header is not None:
            control_token(header)
            return None
        return identity_http.authorize_session(settings, request, service)

    def call(action: Any) -> Any:
        try:
            return action()
        except IdentityError as exc:
            identity_http.refuse(exc.code)

    @app.post(f"{PREFIX}/pairing-codes")
    def mint_pairing_code(request: Request, payload: PairingCodeRequest | None = None) -> JSONResponse:
        service = active_service(request)
        control_token(request.headers.get(control_security.CONTROL_TOKEN_HEADER))
        minted = call(lambda: service.mint_pairing_code(payload.label if payload else None))
        return identity_http.json_no_store({"pairing_code": display_code(minted.code),
                                           "expires_at": minted.expires_at.isoformat()})

    @app.post(f"{PREFIX}/pair")
    def pair(request: Request, payload: PairRequest) -> JSONResponse:
        service = active_service(request)
        identity_http.assert_control_preconditions(settings)
        identity_http.check_origin(settings, request)
        result = call(lambda: service.pair(payload.pairing_code, payload.device_label,
                                           identity_http.presented_session(settings, request)))
        response = identity_http.json_no_store({
            "device_id": result.device.device_id, "device_label": result.device.label,
            "csrf_token": result.csrf_token, "expires_at": result.session.expires_at.isoformat(),
            "idle_timeout_seconds": settings.identity_idle_timeout_seconds})
        identity_http.set_session_cookie(response, settings, request, result.session_id)
        return response

    @app.get(f"{PREFIX}/session")
    def session(request: Request) -> JSONResponse:
        service = identity_http.service_for(request)
        if service is None or not service.enabled:
            return identity_http.json_no_store({"enabled": False, "authenticated": False, "providers": []})
        identity_http.check_origin(settings, request)
        actor = _actor_summary(settings, request, service)
        phase_two: dict[str, Any] = {"providers": _providers(request), **({"actor": actor} if actor else {})}
        anonymous = {"enabled": True, "authenticated": False, **phase_two}
        if identity_http.presented_session(settings, request) is None:
            return identity_http.json_no_store(anonymous)
        try:
            active = identity_http.authorize_session(settings, request, service)
        except HTTPException as exc:
            if exc.detail == IDENTITY_STORE_CORRUPT:
                raise
            return identity_http.json_no_store(anonymous)
        return identity_http.json_no_store({**_session_body(service, active), **phase_two})

    @app.post(f"{PREFIX}/logout")
    def logout(request: Request) -> JSONResponse:
        service = active_service(request)
        if identity_http.presented_session(settings, request) is None:
            if actor_http.presented_actor_session(settings, request) is None:
                identity_http.refuse(SESSION_REQUIRED)
            actor = actor_http.resolve_actor_session(settings, request, service)
            call(lambda: ActorSessions(service).logout(actor))
            ended = identity_http.json_no_store({"status": "ok", "authenticated": False})
            actor_http.clear_actor_cookie(ended, settings, request)
            return ended
        active = identity_http.authorize_session(settings, request, service)
        call(lambda: service.logout(active))
        response = identity_http.json_no_store({"status": "ok", "authenticated": False})
        identity_http.clear_session_cookie(response, settings, request)
        return response

    @app.get(f"{PREFIX}/devices")
    def devices(request: Request) -> JSONResponse:
        service = active_service(request)
        active = token_or_session(request, service)
        current = active.device.device_id if active else None
        return identity_http.json_no_store({"devices": call(lambda: service.list_devices(current))})

    @app.post(f"{PREFIX}/devices/{{device_id}}/revoke")
    def revoke_device(device_id: str, request: Request) -> JSONResponse:
        service = active_service(request)
        active = token_or_session(request, service)
        device = call(lambda: service.revoke_device(device_id))
        response = identity_http.json_no_store({
            "device_id": device.device_id,
            "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None})
        if active is not None and active.device.device_id == device.device_id:
            identity_http.clear_session_cookie(response, settings, request)
        return response
