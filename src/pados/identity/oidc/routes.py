# SPDX-License-Identifier: AGPL-3.0-only
"""``GET /identity/v1/oidc/{provider_id}/start`` and ``.../callback`` (design §4).

With the feature flag off: ``IDENTITY_DISABLED`` (404). With no configured provider of that id:
``OIDC_PROVIDER_NOT_CONFIGURED`` (404); with a refused configuration: its closed code. Both as JSON.

``start`` answers 302 to the provider's authorization endpoint and sets the pre-auth cookie. ``callback`` always
answers 302 to the configured Control Center location, with ``?sso=ok`` and the actor session cookie on success, or
``?sso_error=<CLOSED_CODE>`` and no session otherwise; never provider error text, a code, a state or a token. Both
responses are ``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from pados.config import Settings
from pados.identity import actor_http
from pados.identity import http as identity_http
from pados.identity.errors import (
    IDENTITY_DISABLED,
    OIDC_PROVIDER_NOT_CONFIGURED,
    OIDC_STATE_INVALID,
    ORIGIN_NOT_ALLOWED,
    IdentityError,
)
from pados.identity.oidc.flow import OidcRuntime
from pados.identity.sessions import IdentityService

PREFIX = "/identity/v1/oidc"
REDIRECT_HEADERS = {**identity_http.NO_STORE, "Referrer-Policy": "no-referrer"}


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=302, headers=REDIRECT_HEADERS)


def mount_oidc_routes(app: FastAPI, settings: Settings, *, runtime: OidcRuntime | None = None) -> None:
    app.state.pados_oidc = runtime or OidcRuntime(settings)

    def prerequisites(request: Request, provider_id: str) -> tuple[IdentityService, OidcRuntime]:
        """Flag on, loopback binding, and a configured provider; otherwise a JSON refusal (never a redirect)."""
        service = identity_http.service_for(request)
        if service is None or not service.enabled:
            identity_http.refuse(IDENTITY_DISABLED)
        identity_http.assert_control_preconditions(settings)
        found = actor_http.runtime_for(request)
        if found is None:
            identity_http.refuse(OIDC_PROVIDER_NOT_CONFIGURED)
        try:
            found.provider(provider_id)
        except IdentityError as exc:
            identity_http.refuse(exc.code)
        return service, found

    @app.get(f"{PREFIX}/{{provider_id}}/start")
    def oidc_start(provider_id: str, request: Request) -> RedirectResponse:
        service, runtime = prerequisites(request, provider_id)
        identity_http.check_origin(settings, request)  # a cross-site page cannot start a sign-in
        if (request.headers.get("sec-fetch-site") or "").strip().lower() == "same-site":
            identity_http.refuse(ORIGIN_NOT_ALLOWED)  # nor another localhost port (same site, other origin)
        try:
            started = runtime.start(service, provider_id)
        except IdentityError as exc:
            identity_http.refuse(exc.code)
        response = _redirect(started.authorization_url)
        actor_http.set_preauth_cookie(response, settings, request, started.preauth_value)
        return response

    @app.get(f"{PREFIX}/{{provider_id}}/callback")
    def oidc_callback(provider_id: str, request: Request) -> RedirectResponse:
        service, runtime = prerequisites(request, provider_id)
        target = runtime.post_login_redirect()
        params = dict(request.query_params)
        if len(request.query_params.multi_items()) != len(params):
            params = {}  # a repeated parameter (two states, two codes) is refused as a missing state
        try:
            issued = runtime.callback(service, provider_id, params,
                                      actor_http.presented_preauth(settings, request),
                                      actor_http.presented_actor_session(settings, request))
        except IdentityError as exc:
            response = _redirect(f"{target}?sso_error={exc.code}")
            if exc.code != OIDC_STATE_INVALID:
                # The state was consumed. A refused state leaves the browser's pre-auth cookie alone, so a forged
                # callback link cannot cancel this browser's sign-in in progress (the cookie expires on its own).
                actor_http.clear_preauth_cookie(response, settings, request)
            return response
        response = _redirect(f"{target}?sso=ok")
        actor_http.clear_preauth_cookie(response, settings, request)
        actor_http.set_actor_cookie(response, settings, request, issued.session_id)
        return response
