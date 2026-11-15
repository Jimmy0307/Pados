# SPDX-License-Identifier: AGPL-3.0-only
"""Cookies and request rules of the actor (OIDC) session surface (design §4).

The actor session cookie has its own name (``pados_actor`` over loopback HTTP, ``__Host-pados_actor`` over HTTPS),
which the control plane never reads: ``control_auth`` looks only at the device session cookie and the control token.
Actor requests obey the phase 1 rules: loopback binding, allowlisted ``Origin`` and ``Sec-Fetch-Site``, and on a
state-changing method the derived ``X-PADOS-CSRF`` of the actor session. In addition the session's identity must still
be bound to the same actor, and its provider still configured.

The pre-auth cookie (``pados_oidc_preauth`` / ``__Host-pados_oidc_preauth``) binds an OIDC ``state`` to the browser
that started the sign-in. It is ``SameSite=Lax`` because the provider's redirect back is a cross-site top-level
navigation, which a ``Strict`` cookie would not accompany; it lives 10 minutes and is cleared by the callback.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, Response

from pados.config import Settings
from pados.identity import http as identity_http
from pados.identity.actor_sessions import ActiveActorSession, ActorSessions
from pados.identity.actors import ActorBinding
from pados.identity.errors import (
    CSRF_INVALID,
    IDENTITY_DISABLED,
    OIDC_PROVIDER_NOT_CONFIGURED,
    SESSION_REQUIRED,
    IdentityError,
)
from pados.identity.oidc.flow import STATE_TTL, OidcRuntime
from pados.identity.sessions import IdentityService

PLAIN_ACTOR_COOKIE = "pados_actor"
SECURE_ACTOR_COOKIE = "__Host-pados_actor"
PLAIN_PREAUTH_COOKIE = "pados_oidc_preauth"
SECURE_PREAUTH_COOKIE = "__Host-pados_oidc_preauth"


@dataclass(frozen=True)
class AuthorizedActor:
    session: ActiveActorSession
    binding: ActorBinding


def runtime_for(request: Request) -> OidcRuntime | None:
    runtime = getattr(request.app.state, "pados_oidc", None)
    return runtime if isinstance(runtime, OidcRuntime) else None


def _names(settings: Settings, request: Request) -> tuple[str, str, bool]:
    secure = identity_http.cookie_secure(settings, request)
    if secure:
        return SECURE_ACTOR_COOKIE, SECURE_PREAUTH_COOKIE, True
    return PLAIN_ACTOR_COOKIE, PLAIN_PREAUTH_COOKIE, False


def presented_actor_session(settings: Settings, request: Request) -> str | None:
    return request.cookies.get(_names(settings, request)[0]) or None


def presented_preauth(settings: Settings, request: Request) -> str | None:
    return request.cookies.get(_names(settings, request)[1]) or None


def set_actor_cookie(response: Response, settings: Settings, request: Request, session_id: str) -> None:
    name, _, secure = _names(settings, request)
    response.set_cookie(name, session_id, max_age=settings.identity_session_ttl_seconds, path="/", secure=secure,
                        httponly=True, samesite="strict")


def clear_actor_cookie(response: Response, settings: Settings, request: Request) -> None:
    name, _, secure = _names(settings, request)
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="strict")


def set_preauth_cookie(response: Response, settings: Settings, request: Request, value: str) -> None:
    _, name, secure = _names(settings, request)
    response.set_cookie(name, value, max_age=int(STATE_TTL.total_seconds()), path="/", secure=secure, httponly=True,
                        samesite="lax")


def clear_preauth_cookie(response: Response, settings: Settings, request: Request) -> None:
    _, name, secure = _names(settings, request)
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def resolve_actor_session(settings: Settings, request: Request, service: IdentityService) -> ActiveActorSession:
    """Origin, cookie, session validity and (state-changing) CSRF; no binding check (logout needs only this)."""
    if not service.enabled:
        identity_http.refuse(IDENTITY_DISABLED)
    identity_http.assert_control_preconditions(settings)
    identity_http.check_origin(settings, request)
    session_id = presented_actor_session(settings, request)
    if session_id is None:
        identity_http.refuse(SESSION_REQUIRED)
    sessions = ActorSessions(service)
    try:
        active = sessions.resolve(session_id)
        if identity_http.is_state_changing(request) and not sessions.csrf_ok(
                active, request.headers.get(identity_http.CSRF_HEADER)):
            identity_http.refuse(CSRF_INVALID)
        return active
    except IdentityError as exc:
        identity_http.refuse(exc.code)


def authorize_actor(settings: Settings, request: Request, service: IdentityService) -> AuthorizedActor:
    """Every rule of an actor request, then the binding check; returns the session with its idle timeout slid."""
    active = resolve_actor_session(settings, request, service)
    runtime = runtime_for(request)
    if runtime is None:
        identity_http.refuse(OIDC_PROVIDER_NOT_CONFIGURED)
    record = active.record
    try:
        binding = runtime.binding_for(record.actor_id, record.provider_id, record.issuer, record.subject)
        return AuthorizedActor(session=ActorSessions(service).touch(active), binding=binding)
    except IdentityError as exc:
        identity_http.refuse(exc.code)
