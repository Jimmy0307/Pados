# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP ingress adapter for the OP-001 context and error envelope.

``install_operability`` adds, to an existing FastAPI app, without changing any route's authorization or behaviour:

* a request context (``X-Request-ID`` / ``X-Correlation-ID``, accepted only when safe), echoed on every response and
  bound to the structured log of the request;
* one ``http.request.finished`` log record per request (method, route template, status, outcome, duration, error code);
  never headers, bodies, query strings or exception text;
* error responses that keep the existing ``detail`` and add ``error`` (``pados.error.v1``); an unhandled exception
  becomes a safe ``INTERNAL_ERROR`` instead of a bare 500.

Install it after the app's routes and middleware so it is the outermost layer and also covers their responses.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pados.logging_config import get_logger
from pados.operability.context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    TRACEPARENT_HEADER,
    bind_context,
    context_from_headers,
    current_context,
    new_id,
)
from pados.operability.metrics import METRICS, record_http_request
from pados.operability.resilience import DEADLINE_HEADER, deadline_from_header, deadline_scope
from pados.operability.errors import (
    GENERIC_CODE_BY_STATUS,
    SAFE_MESSAGE_BY_CATEGORY,
    ErrorCategory,
    ErrorEnvelope,
    PadosError,
    WriteOutcome,
    category_for_status,
    is_error_code,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _ids() -> tuple[str, str]:
    context = current_context()
    if context is None:  # only reachable when the adapter is not installed as the outermost layer
        generated = new_id()
        return generated, generated
    return context.request_id, context.correlation_id


def _write_outcome(method: str) -> WriteOutcome:
    # A safe method never writes. For any other method a refusal cannot tell on its own whether earlier work in the
    # request committed, so it says UNKNOWN unless the raiser declares the outcome (PadosError).
    return WriteOutcome.NO if method.upper() in SAFE_METHODS else WriteOutcome.UNKNOWN


def envelope_for_http_error(status: int, detail: Any, *, method: str) -> ErrorEnvelope:
    """The envelope for an existing ``HTTPException``: a code-shaped detail becomes the code, prose never leaks."""
    category = category_for_status(status)
    code = GENERIC_CODE_BY_STATUS.get(status, f"HTTP_{status}")
    if isinstance(detail, str):
        candidate = detail.split(":", 1)[0].strip()
        if is_error_code(candidate):
            code = candidate
    request_id, correlation_id = _ids()
    return ErrorEnvelope(code=code, category=category, message=SAFE_MESSAGE_BY_CATEGORY[category], retryable=False,
                         request_id=request_id, correlation_id=correlation_id, write_occurred=_write_outcome(method))


def _envelope_response(request: Request, status: int, detail: Any, envelope: ErrorEnvelope,
                       headers: dict[str, str] | None = None) -> JSONResponse:
    request.state.error_code = envelope.code
    return JSONResponse(status_code=status, headers=headers,
                        content={"detail": jsonable_encoder(detail), "error": envelope.model_dump(mode="json")})


def install_operability(app: FastAPI, *, component: str) -> None:
    logger = get_logger("pados.http", component=component)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        envelope = envelope_for_http_error(exc.status_code, exc.detail, method=request.method)
        return _envelope_response(request, exc.status_code, exc.detail, envelope, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        request_id, correlation_id = _ids()
        envelope = ErrorEnvelope(code="VALIDATION_FAILED", category=ErrorCategory.VALIDATION,
                                 message=SAFE_MESSAGE_BY_CATEGORY[ErrorCategory.VALIDATION], retryable=False,
                                 request_id=request_id, correlation_id=correlation_id,
                                 write_occurred=_write_outcome(request.method))
        return _envelope_response(request, 422, exc.errors(), envelope)

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def database_unavailable(request: Request, exc: Exception) -> Response:
        # OP-005: a database disconnect is a dependency failure, not an internal defect. A safe method may be retried;
        # for any other method the write outcome is unknown, so the client must read state before retrying.
        logger.warning("http.request.dependency_failed", dependency="database", error_type=type(exc).__name__,
                       error_code="DATABASE_UNAVAILABLE")
        METRICS.inc("pados_dependency_failures_total", {"dependency": "database", "error_code": "DATABASE_UNAVAILABLE"})
        request_id, correlation_id = _ids()
        safe = request.method.upper() in SAFE_METHODS
        envelope = ErrorEnvelope(code="DATABASE_UNAVAILABLE", category=ErrorCategory.DEPENDENCY,
                                 message=SAFE_MESSAGE_BY_CATEGORY[ErrorCategory.DEPENDENCY], retryable=safe,
                                 request_id=request_id, correlation_id=correlation_id,
                                 write_occurred=_write_outcome(request.method), details={"dependency": "database"})
        return _envelope_response(request, 503, "DATABASE_UNAVAILABLE", envelope)

    @app.exception_handler(PadosError)
    async def declared_error(request: Request, exc: PadosError) -> Response:
        request_id, correlation_id = _ids()
        envelope = ErrorEnvelope(code=exc.code, category=exc.category, message=exc.message, retryable=exc.retryable,
                                 request_id=request_id, correlation_id=correlation_id,
                                 write_occurred=exc.write_occurred, details=exc.details)
        return _envelope_response(request, exc.status_code, exc.code, envelope)

    @app.middleware("http")
    async def operability_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        context = context_from_headers(request.headers.get(REQUEST_ID_HEADER),
                                       request.headers.get(CORRELATION_ID_HEADER), component=component,
                                       traceparent=request.headers.get(TRACEPARENT_HEADER))
        started = time.perf_counter()
        deadline = deadline_from_header(request.headers.get(DEADLINE_HEADER))  # OP-004: optional parent budget
        with bind_context(context), deadline_scope(deadline):
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - every failure leaves as a safe envelope, never a bare 500
                logger.error("http.request.unhandled_exception", error_type=type(exc).__name__,
                             error_code="INTERNAL_ERROR")
                envelope = ErrorEnvelope(code="INTERNAL_ERROR", category=ErrorCategory.INTERNAL,
                                         message=SAFE_MESSAGE_BY_CATEGORY[ErrorCategory.INTERNAL], retryable=False,
                                         request_id=context.request_id, correlation_id=context.correlation_id,
                                         write_occurred=_write_outcome(request.method))
                response = _envelope_response(request, 500, "Internal Server Error", envelope)
            response.headers[REQUEST_ID_HEADER] = context.request_id
            response.headers[CORRELATION_ID_HEADER] = context.correlation_id
            status = response.status_code
            route_path = getattr(request.scope.get("route"), "path", "UNMATCHED")
            error_code = getattr(request.state, "error_code", None)
            duration = time.perf_counter() - started
            logger.info("http.request.finished", method=request.method, route=route_path, status_code=status,
                        outcome="completed" if status < 400 else "rejected" if status < 500 else "failed",
                        error_code=error_code, duration_ms=round(duration * 1000, 3))
            # OP-008: labelled by route template and stable code only, never by raw path, id or query.
            record_http_request(method=request.method, route=route_path, status=status, duration_s=duration,
                                error_code=error_code)
            return response
