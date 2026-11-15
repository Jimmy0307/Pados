# SPDX-License-Identifier: AGPL-3.0-only
"""Cookie variants and the request rules of a cookie-authenticated request (design §3.4).

The cookie variant follows the ASGI request scheme (``PADOS_IDENTITY_COOKIE_SECURE=auto``); ``X-Forwarded-*`` headers
are never read here. ``never`` is refused over HTTPS: the ``__Host-`` Secure variant is used there whatever is
configured. A state-changing request (any method but GET, HEAD and OPTIONS) needs an allowlisted ``Origin``, the
derived ``X-PADOS-CSRF`` and, when sent, ``Sec-Fetch-Site`` of ``same-origin`` or ``none``. A safe request is refused
when it carries a non-allowlisted ``Origin`` or ``Sec-Fetch-Site: cross-site``.
"""
from __future__ import annotations

from typing import Any, NoReturn

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from pados.config import Settings
from pados.control import security as control_security
from pados.identity.errors import (
    CSRF_INVALID,
    IDENTITY_DISABLED,
    ORIGIN_NOT_ALLOWED,
    SESSION_REQUIRED,
    IdentityError,
)
from pados.identity.sessions import ActiveSession, IdentityService

PLAIN_COOKIE = "pados_session"
SECURE_COOKIE = "__Host-pados_session"
CSRF_HEADER = "X-PADOS-CSRF"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
NO_STORE = {"Cache-Control": "no-store"}


def service_for(request: Request) -> IdentityService | None:
    service = getattr(request.app.state, "pados_identity", None)
    return service if isinstance(service, IdentityService) else None


def cookie_secure(settings: Settings, request: Request) -> bool:
    if request.url.scheme == "https":
        return True  # ``never`` is refused over HTTPS
    return settings.identity_cookie_secure == "always"


def cookie_name(settings: Settings, request: Request) -> str:
    return SECURE_COOKIE if cookie_secure(settings, request) else PLAIN_COOKIE


def presented_session(settings: Settings, request: Request) -> str | None:
    value = request.cookies.get(cookie_name(settings, request))
    return value or None


def set_session_cookie(response: Response, settings: Settings, request: Request, session_id: str) -> None:
    secure = cookie_secure(settings, request)
    response.set_cookie(SECURE_COOKIE if secure else PLAIN_COOKIE, session_id,
                        max_age=settings.identity_session_ttl_seconds, path="/", secure=secure, httponly=True,
                        samesite="strict")


def clear_session_cookie(response: Response, settings: Settings, request: Request) -> None:
    secure = cookie_secure(settings, request)
    response.delete_cookie(SECURE_COOKIE if secure else PLAIN_COOKIE, path="/", secure=secure, httponly=True,
                           samesite="strict")


def refuse(code: str) -> NoReturn:
    raise HTTPException(status_code=IdentityError(code).status_code, detail=code, headers=NO_STORE)


def is_state_changing(request: Request) -> bool:
    return request.method.upper() not in SAFE_METHODS


def check_origin(settings: Settings, request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    allowed = settings.identity_origins
    if is_state_changing(request):
        if origin is None or origin not in allowed:
            refuse(ORIGIN_NOT_ALLOWED)
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            refuse(ORIGIN_NOT_ALLOWED)
        return
    if origin is not None and origin not in allowed:
        refuse(ORIGIN_NOT_ALLOWED)
    if fetch_site == "cross-site":
        refuse(ORIGIN_NOT_ALLOWED)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
SECURE_COOKIE_REQUIRED = "IDENTITY_SECURE_COOKIE_REQUIRED"


def assert_session_transport(settings: Settings) -> None:
    """Sessions off loopback need the ``__Host-`` Secure cookie (design §3.9).

    A plain ``pados_session`` cookie is sent over HTTP, so off loopback anyone on the path could read it. With a
    non-loopback ``PADOS_API_HOST`` sessions refuse unless ``PADOS_IDENTITY_COOKIE_SECURE=always``. Even then the control
    plane's own loopback-binding rule still refuses (``control api must bind to localhost``): both checks fail closed,
    and there is no supported non-loopback session deployment in v1.
    """
    if settings.identity_sessions_enabled and settings.api_host not in LOOPBACK_HOSTS             and settings.identity_cookie_secure != "always":
        raise PermissionError(f"{SECURE_COOKIE_REQUIRED}: a non-loopback bind needs PADOS_IDENTITY_COOKIE_SECURE=always")


def assert_control_preconditions(settings: Settings) -> None:
    """The control plane's own preconditions apply to the session path too (transport, loopback binding, token)."""
    try:
        assert_session_transport(settings)
        control_security.assert_localhost_binding(settings)
        control_security.assert_unique_control_token(settings)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def authorize_session(settings: Settings, request: Request, service: IdentityService) -> ActiveSession:
    """Every rule of a cookie-authenticated request; returns the session with its idle timeout slid."""
    if not service.enabled:
        refuse(IDENTITY_DISABLED)
    assert_control_preconditions(settings)
    check_origin(settings, request)
    session_id = presented_session(settings, request)
    if session_id is None:
        refuse(SESSION_REQUIRED)
    try:
        active = service.resolve(session_id)
        if is_state_changing(request) and not service.csrf_ok(active, request.headers.get(CSRF_HEADER)):
            refuse(CSRF_INVALID)
        return service.touch(active)
    except IdentityError as exc:
        refuse(exc.code)


def session_cookie_applies(settings: Settings, request: Request) -> bool:
    """Whether the control plane should try the cookie: flag on, an identity service mounted, a cookie sent."""
    return (settings.identity_sessions_enabled and service_for(request) is not None
            and presented_session(settings, request) is not None)


def authorize_control_session(settings: Settings, request: Request) -> None:
    service = service_for(request)
    if service is None:
        refuse(SESSION_REQUIRED)
    authorize_session(settings, request, service)


def json_no_store(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers=NO_STORE)
