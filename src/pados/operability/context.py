# SPDX-License-Identifier: AGPL-3.0-only
"""Request and operation context carried across PADOS boundaries (OP-001).

``request_id`` names one transport request; ``correlation_id`` names a business operation that may span several
requests or jobs. Neither is an authorization, an idempotency key or a proof of anything: they only make one operation
queryable across the response, structured logs, receipts and downstream calls.

Inbound identifiers are accepted only when they match a short safe alphabet; anything else is replaced by a generated
one and never echoed. Outside a bound context no identifier is invented for downstream calls.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import uuid4

import structlog

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOG_FIELDS = ("request_id", "correlation_id", "component", "project_id", "run_id", "job_id")

_CURRENT: ContextVar[OperationContext | None] = ContextVar("pados_operation_context", default=None)


@dataclass(frozen=True)
class OperationContext:
    request_id: str
    correlation_id: str
    component: str
    project_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None

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


def context_from_headers(request_id: str | None, correlation_id: str | None, *, component: str) -> OperationContext:
    request = accept_id(request_id) or new_id()
    return OperationContext(request_id=request, correlation_id=accept_id(correlation_id) or request,
                            component=component)


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
    return {REQUEST_ID_HEADER: context.request_id, CORRELATION_ID_HEADER: context.correlation_id}
