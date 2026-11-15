# SPDX-License-Identifier: AGPL-3.0-only
"""``/identity/v1`` (design §3.5). JSON; refusals use the ``pados.error.v1`` envelope with a closed code.

Minting a pairing code needs the control token header (a session cannot mint one). ``/pair`` needs a code and an
allowlisted ``Origin``. ``GET /session`` is the only route that answers while the feature flag is off. The pairing
code appears in exactly one response: the mint response.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from pados.config import Settings
from pados.control import security as control_security
from pados.identity import http as identity_http
from pados.identity.errors import (
    IDENTITY_DISABLED,
    IDENTITY_STORE_CORRUPT,
    SESSION_EXPIRED,
    SESSION_REQUIRED,
    SESSION_REVOKED,
    IdentityError,
)
from pados.identity.sessions import ActiveSession, IdentityService
from pados.identity.tokens import display_code

PREFIX = "/identity/v1"
_DEAD_SESSION_CODES = frozenset({SESSION_EXPIRED, SESSION_REVOKED, SESSION_REQUIRED})


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


def mount_identity(app: FastAPI, settings: Settings, *, service: IdentityService | None = None) -> None:
    app.state.pados_identity = service or IdentityService(settings)

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
        anonymous = {"enabled": bool(service and service.enabled), "authenticated": False, "providers": []}
        if service is None or not service.enabled:
            return identity_http.json_no_store(anonymous)
        identity_http.check_origin(settings, request)
        if identity_http.presented_session(settings, request) is None:
            return identity_http.json_no_store(anonymous)
        try:
            active = identity_http.authorize_session(settings, request, service)
        except HTTPException as exc:
            if exc.detail == IDENTITY_STORE_CORRUPT:
                raise
            return identity_http.json_no_store(anonymous)
        return identity_http.json_no_store(_session_body(service, active))

    @app.post(f"{PREFIX}/logout")
    def logout(request: Request) -> JSONResponse:
        service = active_service(request)
        if identity_http.presented_session(settings, request) is None:
            identity_http.refuse(SESSION_REQUIRED)
        try:
            active = identity_http.authorize_session(settings, request, service)
        except HTTPException as exc:
            # A dead session (expired, revoked, unknown) is refused as before, but its cookie is cleared so the
            # browser stops presenting it. Origin and CSRF refusals keep the cookie: they are not the cookie's fault.
            if exc.detail not in _DEAD_SESSION_CODES:
                raise
            cleared = Response()
            identity_http.clear_session_cookie(cleared, settings, request)
            raise HTTPException(status_code=exc.status_code, detail=exc.detail,
                                headers={**(exc.headers or {}), "set-cookie": cleared.headers["set-cookie"]}) from exc
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
