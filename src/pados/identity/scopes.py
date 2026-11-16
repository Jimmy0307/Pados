# SPDX-License-Identifier: AGPL-3.0-only
"""Least privilege for device sessions on the control plane (design §3.8).

The control token keeps the control plane's full authority, unchanged. A **device session** (the cookie path of the
single ``control_auth`` dependency in ``pados.api.app``) may call only the ``(method, route template)`` pairs listed
in ``SESSION_ALLOWED``. Everything else is refused with ``SESSION_SCOPE_DENIED``: the check is an allowlist, so a
route added later is closed to sessions until someone lists it here on purpose.

The check uses the route template FastAPI matched (``request.scope["route"].path``), never the raw URL, so encoded
slashes, trailing segments or case tricks cannot reach a different decision. A request without a matched route is
refused.

``SESSION_DENIED`` records the ``control_auth`` routes that are kept from sessions deliberately, each with its
reason. It grants nothing (the allowlist alone decides); it exists so that ``tests/identity/test_session_scopes.py``
can prove that every ``control_auth`` route is classified one way or the other.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastapi import Request

from pados.identity.errors import SESSION_SCOPE_DENIED
from pados.identity.http import refuse
from pados.logging_config import get_logger

RouteKey = tuple[str, str]

# What a paired browser may call (the Control Center's own needs). Grouped by surface; keep each list sorted.
_SESSION_READS: Final[tuple[str, ...]] = (
    "/control/approvals",
    "/control/approvals/{approval_id}",
    "/control/health",  # no control_auth at all (public); listed so the UI contract is complete
    "/control/jobs",
    "/control/jobs/{job_id}",
    "/control/jobs/{job_id}/artifacts",
    "/control/jobs/{job_id}/artifacts/{artifact_type}/content",  # digest-verified evidence text, read only
    "/control/loops",
    "/control/loops/{loop_id}",
    "/control/loops/{loop_id}/events",
    "/control/loops/{loop_id}/iterations",
    "/control/projects",
    "/control/projects/{project_id}",
    "/control/projects/{project_id}/activity",
    "/control/projects/{project_id}/context-summary",
    "/control/projects/{project_id}/documents",
    "/control/projects/{project_id}/documents/content",
    "/control/projects/{project_id}/messages",
    "/control/projects/{project_id}/work",  # projection only
    "/control/projects/{project_id}/work-map",  # projection only (Work Map)
    "/control/providers",
    "/control/providers/{provider_id}",
    "/control/resources",  # read-only measured/UNKNOWN metrics and a cleanup preview (no deletion)
    "/control/sync/events",
    "/control/task-goals/{goal_id}",
    "/control/task-plans/{plan_id}",
    "/control/work-requests/{request_id}",
    "/control/workers",
    "/control/workers/{worker_id}/health",
    "/memory/search",
)
_SESSION_WRITES: Final[tuple[str, ...]] = (
    "/control/approvals",
    "/control/approvals/{approval_id}/approve",  # a ResultCandidate approval still needs WebAuthn step-up
    "/control/approvals/{approval_id}/reject",
    "/control/approvals/{approval_id}/revise",
    "/control/approvals/{approval_id}/revoke",
    "/control/jobs/{job_id}/cancel",
    "/control/loops",
    "/control/loops/{loop_id}/cancel",
    "/control/loops/{loop_id}/result-candidate",  # submission only; acceptance stays with step-up
    "/control/loops/{loop_id}/start",
    "/control/loops/{loop_id}/step",
    "/control/projects",  # bounded: a folder under the approved workspace root, no path or id typed
    "/control/projects/{project_id}/documents",  # .md only, inside the project root, no silent overwrite
    "/control/projects/{project_id}/messages",
    "/control/providers/{provider_id}/diagnostics",  # observes; writes only content-free attempt receipts
    "/control/providers/{provider_id}/disable",  # narrows within the configured setup
    "/control/task-goals",
    "/control/task-plans",
    "/control/work-requests",
    "/control/work-requests/{request_id}/route",
)

SESSION_ALLOWED: Final[frozenset[RouteKey]] = frozenset(
    {("GET", path) for path in _SESSION_READS} | {("POST", path) for path in _SESSION_WRITES})

# control_auth routes a session may never call, with the reason (design §3.8). The control token still may.
SESSION_DENIED: Final[Mapping[RouteKey, str]] = MappingProxyType({
    ("POST", "/context/assemble"): "assembles a context packet from the memory store across scopes; an operator "
                                   "(control token) action, not a browser one",
    ("POST", "/memory/facts"): "memory write: adds a fact to long-term memory",
    ("POST", "/memory/facts/{memory_id}/promote"): "memory write: promotes a fact's trust level",
    ("POST", "/control/providers/{provider_id}/enable"): "lifts a restriction the operator set (a disabled "
        "provider); kept to the control token (CLI). A session may still disable, which only narrows",
    ("PUT", "/control/providers/{provider_id}"): "operator configuration: can repoint a runtime endpoint or a gateway "
        "contract; kept to the control token (CLI)",
    ("POST", "/control/projections/render"): "writes projection files into the vault on disk",
    ("POST", "/control/loops/{loop_id}/pause"): "with resume, a pause from a budget or human-wait stop returns the "
        "loop to RUNNING past its iteration budget (security review 2026-11-16); the Control Center does not pause, "
        "and the control token (CLI) still may",
    ("POST", "/control/loops/{loop_id}/resume"): "see pause: kept to the control token (CLI)",
})

_HEAD_AS: Final = "GET"


def route_template(request: Request) -> str | None:
    """The path template of the route FastAPI matched for this request, or ``None`` when there is none."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path.startswith("/") else None


def session_may_call(method: str, template: str | None) -> bool:
    if template is None:
        return False
    verb = method.upper()
    return ((_HEAD_AS if verb == "HEAD" else verb), template) in SESSION_ALLOWED


def enforce_session_scope(request: Request) -> None:
    """Refuse a device-session request whose (method, matched template) is not on the allowlist."""
    template = route_template(request)
    if session_may_call(request.method, template):
        return
    # The method and the route template only: never the raw URL, a cookie or a header value.
    get_logger("pados.identity").info("identity.session.scope_denied", error_code=SESSION_SCOPE_DENIED,
                                      method=request.method.upper(), route=template or "UNMATCHED")
    refuse(SESSION_SCOPE_DENIED)
