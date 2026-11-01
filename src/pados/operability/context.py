# SPDX-License-Identifier: AGPL-3.0-only
"""Request and operation context carried across PADOS boundaries (OP-001).

``request_id`` names one transport request; ``correlation_id`` names a business operation that may span several
requests or jobs. Neither is an authorization, an idempotency key or a proof of anything: they only make one operation
queryable across the response, structured logs, receipts and downstream calls.

Inbound identifiers are accepted only when they match a short safe alphabet; anything else is replaced by a generated
one and never echoed. Outside a bound context no identifier is invented for downstream calls.

OP-008 adds W3C trace context: a valid inbound ``traceparent`` continues its trace id, and each bound operation gets
its own span id. Trace and span ids go to structured logs and downstream ``traceparent`` headers only; the trace
persisted beside durable records (``trace_fields``) stays request and correlation id, so sealed identities are
unchanged.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import uuid4

import structlog

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
TRACEPARENT_HEADER = "traceparent"
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOG_FIELDS = ("request_id", "correlation_id", "component", "project_id", "run_id", "job_id", "trace_id",
               "span_id")

_CURRENT: ContextVar[OperationContext | None] = ContextVar("pados_operation_context", default=None)


@dataclass(frozen=True)
class OperationContext:
    request_id: str
    correlation_id: str
    component: str
    project_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def scoped(self, *, project_id: str | None = None, run_id: str | None = None,
               job_id: str | None = None) -> OperationContext:
        """The same operation narrowed to a project, run or job (a new value; the original is unchanged)."""
        return replace(self, project_id=project_id or self.project_id, run_id=run_id or self.run_id,
                       job_id=job_id or self.job_id)

    def log_fields(self) -> dict[str, str]:
        return {name: value for name in _LOG_FIELDS if (value := getattr(self, name)) is not None}


def new_id() -> str:
    return uuid4().hex


def accept_id(value: str | None) -> str | None:
    """The inbound identifier when it is safe to store, log and echo; otherwise None."""
    if value is None or not _SAFE_ID.fullmatch(value):
        return None
    return value


def new_span_id() -> str:
    return secrets.token_hex(8)


def accept_traceparent(value: str | None) -> str | None:
    """The trace id of a valid W3C ``traceparent`` (version 00, non-zero ids); otherwise None."""
    match = _TRACEPARENT.fullmatch(value or "")
    if match is None or set(match.group(1)) == {"0"} or set(match.group(2)) == {"0"}:
        return None
    return match.group(1)


def context_from_headers(request_id: str | None, correlation_id: str | None, *, component: str,
                         traceparent: str | None = None) -> OperationContext:
    request = accept_id(request_id) or new_id()
    return OperationContext(request_id=request, correlation_id=accept_id(correlation_id) or request,
                            component=component, trace_id=accept_traceparent(traceparent) or new_id(),
                            span_id=new_span_id())


def current_context() -> OperationContext | None:
    return _CURRENT.get()


@contextmanager
def bind_context(context: OperationContext) -> Iterator[OperationContext]:
    """Make ``context`` current for this task and its structured logs, restoring the previous one afterwards."""
    reset_handle = _CURRENT.set(context)
    previous = structlog.contextvars.get_contextvars()
    structlog.contextvars.unbind_contextvars(*_LOG_FIELDS)
    structlog.contextvars.bind_contextvars(**context.log_fields())
    try:
        yield context
    finally:
        structlog.contextvars.unbind_contextvars(*_LOG_FIELDS)
        restored = {name: previous[name] for name in _LOG_FIELDS if name in previous}
        if restored:
            structlog.contextvars.bind_contextvars(**restored)
        _CURRENT.reset(reset_handle)


def downstream_headers() -> dict[str, str]:
    """Headers that carry the current context to a downstream HTTP call; empty outside a bound context."""
    context = _CURRENT.get()
    if context is None:
        return {}
    headers = {REQUEST_ID_HEADER: context.request_id, CORRELATION_ID_HEADER: context.correlation_id}
    if context.trace_id and context.span_id:
        headers[TRACEPARENT_HEADER] = f"00-{context.trace_id}-{context.span_id}-01"
    return headers


def trace_fields() -> dict[str, str]:
    """The trace persisted beside a durable record: identifiers only, never authority or proof.

    Callers store it outside any sealed or hashed content (receipt, capability result, idempotency key), so the same
    work traced under two different requests keeps one identity.
    """
    context = _CURRENT.get()
    if context is None:
        return {}
    return {"request_id": context.request_id, "correlation_id": context.correlation_id}


@contextmanager
def operation_scope(component: str, *, project_id: str | None = None, run_id: str | None = None,
                    job_id: str | None = None) -> Iterator[OperationContext]:
    """Continue the caller's operation in ``component`` (or start one, for CLI and background work)."""
    current = _CURRENT.get()
    if current is None:
        generated = new_id()
        current = OperationContext(request_id=generated, correlation_id=generated, component=component,
                                   trace_id=new_id())
    # Each scope is its own span of the same trace.
    scoped = replace(current, component=component, span_id=new_span_id() if current.trace_id else None).scoped(project_id=project_id, run_id=run_id, job_id=job_id)
    with bind_context(scoped) as bound:
        yield bound


def error_code_of(exc: BaseException) -> str:
    """A stable code for a log record: the exception's declared code, a code-shaped message prefix, or its type."""
    declared = getattr(exc, "code", None)
    if isinstance(declared, str) and re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", declared):
        return declared
    prefix = str(exc).split(":", 1)[0].split(" ", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", prefix) and len(prefix) <= 80:
        return prefix
    return type(exc).__name__.upper() if type(exc).__name__.isupper() else "UNCLASSIFIED_" + re.sub(
        r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).upper()
