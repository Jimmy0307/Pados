# SPDX-License-Identifier: AGPL-3.0-only
"""Least privilege for device sessions (design §3.8): an explicit, fail-closed allowlist of route templates.

Every route guarded by ``control_auth`` is classified (allowed for sessions, or denied with a reason); a new route
that is neither fails here. A session is refused with ``SESSION_SCOPE_DENIED`` on everything off the allowlist; the
control token header keeps its full authority on every route; result acceptance still needs WebAuthn step-up.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pados.identity.scopes import SESSION_ALLOWED, SESSION_DENIED, enforce_session_scope, session_may_call
from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS, Harness

DUMMY_ID = "00000000-0000-0000-0000-0000000000ab"
AUTH_DETAILS = frozenset({"missing control token", "invalid control token"})


def _code(response_json: dict[str, Any]) -> str | None:
    error = response_json.get("error")
    return str(error.get("code")) if isinstance(error, dict) else None


def _uses(dependant: Dependant, target: object) -> bool:
    return any(dep.call is target or _uses(dep, target) for dep in dependant.dependencies)


def control_auth_routes(app: FastAPI) -> set[tuple[str, str]]:
    target = app.state.pados_control_auth
    return {(method, route.path) for route in app.routes if isinstance(route, APIRoute) and _uses(route.dependant, target)
            for method in route.methods}


def all_routes(app: FastAPI) -> set[tuple[str, str]]:
    return {(method, route.path) for route in app.routes if isinstance(route, APIRoute) for method in route.methods}


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", DUMMY_ID, path)


@pytest.fixture
def session_client(harness: Harness, no_database: None) -> Iterator[tuple[TestClient, dict[str, str]]]:
    client = TestClient(harness.app, raise_server_exceptions=False)
    paired = harness.pair(client, harness.mint(client))
    assert paired.status_code == 200, paired.text
    yield client, {"Origin": ORIGIN, "X-PADOS-CSRF": str(paired.json()["csrf_token"]),
                   "Sec-Fetch-Site": "same-origin"}
    client.close()


# --- classification ----------------------------------------------------------------------------------------------

def test_every_control_auth_route_is_classified_and_no_entry_is_stale(harness: Harness) -> None:
    guarded = control_auth_routes(harness.app)
    assert len(guarded) >= 40  # the inventory really found the control plane
    unclassified = guarded - SESSION_ALLOWED - set(SESSION_DENIED)
    assert sorted(unclassified) == [], "classify each new control_auth route in pados.identity.scopes"
    assert SESSION_ALLOWED.isdisjoint(SESSION_DENIED)
    assert sorted(set(SESSION_DENIED) - guarded) == []  # a denial names a real control_auth route
    assert sorted(SESSION_ALLOWED - all_routes(harness.app)) == []  # no allowlist entry for a route that is not there
    # The one allowlisted route without control_auth is the public health probe.
    assert SESSION_ALLOWED - guarded == {("GET", "/control/health")}


def test_the_deliberate_denials() -> None:
    assert set(SESSION_DENIED) == {("POST", "/context/assemble"), ("POST", "/memory/facts"),
                                   ("POST", "/memory/facts/{memory_id}/promote"),
                                   ("POST", "/control/projections/render"),
                                   ("PUT", "/control/providers/{provider_id}"),
                                   ("POST", "/control/providers/{provider_id}/enable"),
                                   ("POST", "/control/loops/{loop_id}/pause"),
                                   ("POST", "/control/loops/{loop_id}/resume")}
    assert all(reason.strip() for reason in SESSION_DENIED.values())


def test_the_allowlist_is_only_get_and_post_of_the_control_center_contract() -> None:
    assert {method for method, _ in SESSION_ALLOWED} == {"GET", "POST"}
    assert all(path.startswith(("/control/", "/memory/search")) for _, path in SESSION_ALLOWED)
    assert ("GET", "/memory/search") in SESSION_ALLOWED
    assert not any(path.startswith("/memory/facts") for _, path in SESSION_ALLOWED)
    # The product work loop (integrated from feat/product-work-loop-ui-20261116) is part of the session contract.
    for key in [("POST", "/control/projects"), ("POST", "/control/projects/{project_id}/documents"),
                ("GET", "/control/projects/{project_id}/documents"),
                ("GET", "/control/projects/{project_id}/documents/content"),
                ("GET", "/control/projects/{project_id}/work"),
                ("GET", "/control/jobs/{job_id}/artifacts/{artifact_type}/content"),
                ("GET", "/control/projects/{project_id}/work-map"), ("GET", "/control/resources"),
                ("GET", "/control/providers"), ("GET", "/control/providers/{provider_id}"),
                ("POST", "/control/providers/{provider_id}/diagnostics"),
                ("POST", "/control/providers/{provider_id}/disable")]:
        assert key in SESSION_ALLOWED, key
    # Editing a provider can repoint an endpoint or a gateway contract: control token (CLI) only.
    assert ("PUT", "/control/providers/{provider_id}") not in SESSION_ALLOWED
    assert ("PUT", "/control/providers/{provider_id}") in SESSION_DENIED
    # Re-enabling lifts an operator restriction: control token only (final review MEDIUM-1).
    assert ("POST", "/control/providers/{provider_id}/enable") in SESSION_DENIED


@pytest.mark.parametrize(("method", "template", "allowed"), [
    ("GET", "/control/projects", True),
    ("get", "/control/projects", True),
    ("HEAD", "/control/projects", True),
    ("DELETE", "/control/projects", False),
    ("POST", "/control/projects", True),
    ("PUT", "/control/projects", False),
    ("GET", "/control/projects/", False),
    ("GET", "/control/projects/p-1", False),  # a raw path is never a template
    ("POST", "/context/assemble", False),
    ("GET", None, False),
])
def test_the_decision_is_by_method_and_template(method: str, template: str | None, allowed: bool) -> None:
    assert session_may_call(method, template) is allowed


def test_a_request_without_a_matched_route_is_refused() -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/control/projects", "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as refused:
        enforce_session_scope(request)
    assert refused.value.status_code == 403 and refused.value.detail == "SESSION_SCOPE_DENIED"


# --- enforcement through the one control_auth dependency ----------------------------------------------------------

@pytest.mark.parametrize("key", sorted(SESSION_DENIED))
def test_a_session_is_refused_on_every_denied_route(session_client: tuple[TestClient, dict[str, str]],
                                                    key: tuple[str, str]) -> None:
    client, headers = session_client
    method, template = key
    response = client.request(method, concrete(template), json={}, headers=headers)
    assert response.status_code == 403, response.text
    assert _code(response.json()) == "SESSION_SCOPE_DENIED"


def test_memory_writes_and_context_assembly_never_run_for_a_session(
        session_client: tuple[TestClient, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.api import app as api_app

    called: list[str] = []
    monkeypatch.setattr(api_app.memory_service, "add_memory_record", lambda *a, **k: called.append("add"))
    monkeypatch.setattr(api_app.memory_service, "promote_memory", lambda *a, **k: called.append("promote"))
    monkeypatch.setattr(api_app.context_packet, "assemble_context_packet", lambda *a, **k: called.append("assemble"))
    client, headers = session_client
    bodies = {"/memory/facts": {"project_id": "p", "content": "c", "kind": "fact"},
              f"/memory/facts/{DUMMY_ID}/promote": {"trust": "high"},
              "/context/assemble": {"project_id": "p", "query": "q"}}
    for path, body in bodies.items():
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 403 and _code(response.json()) == "SESSION_SCOPE_DENIED", path
    assert called == []


@pytest.mark.parametrize("key", sorted(SESSION_ALLOWED - {("GET", "/control/health")}))
def test_a_session_passes_the_scope_on_every_allowed_route(session_client: tuple[TestClient, dict[str, str]],
                                                           key: tuple[str, str]) -> None:
    client, headers = session_client
    method, template = key
    params = {"project_id": "p-1", "query": "x"} if template == "/memory/search" else None
    response = client.request(method, concrete(template), json={} if method == "POST" else None, headers=headers,
                              params=params)
    # Past authorization: the handler (or body validation) answers. With the database stubbed that may be 4xx/5xx,
    # but never an authorization refusal.
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    assert _code(body) not in {"SESSION_SCOPE_DENIED", "SESSION_REQUIRED", "CSRF_INVALID", "ORIGIN_NOT_ALLOWED"}, \
        response.text
    assert str(body.get("detail")) not in AUTH_DETAILS


def test_the_control_token_keeps_full_authority_on_every_control_auth_route(harness: Harness,
                                                                            no_database: None) -> None:
    client = TestClient(harness.app, raise_server_exceptions=False)
    harness.pair(client, harness.mint(client))  # a session cookie travels too: the header alone decides
    assert client.cookies.get("pados_session")
    for method, template in sorted(control_auth_routes(harness.app)):
        params = {"project_id": "p-1", "query": "x"} if template == "/memory/search" else None
        response = client.request(method, concrete(template), json={} if method == "POST" else None,
                                  headers=TOKEN_HEADERS, params=params)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        assert _code(body) != "SESSION_SCOPE_DENIED", (method, template)
        assert str(body.get("detail")) not in AUTH_DETAILS, (method, template)
    client.close()


def test_a_denied_route_still_refuses_an_unauthenticated_cookie_with_its_session_code(harness: Harness,
                                                                                      no_database: None) -> None:
    client = TestClient(harness.app, raise_server_exceptions=False)
    client.cookies.set("pados_session", "not-a-session")
    response = client.post("/context/assemble", json={}, headers={"Origin": ORIGIN, "X-PADOS-CSRF": "x"})
    assert response.status_code == 403 and _code(response.json()) == "SESSION_REQUIRED"


def test_a_session_still_cannot_accept_a_result(session_client: tuple[TestClient, dict[str, str]],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """``approve`` is on the allowlist; the step-up refusal inside ``decide_approval`` still decides."""
    from pados.control import service as control_service

    monkeypatch.setattr(control_service, "get_approval", lambda _s, _id: {
        "id": DUMMY_ID, "status": "PENDING", "version": 1, "decision_reason": None, "subject_kind": None})
    monkeypatch.setattr(control_service, "_backs_a_result_candidate", lambda _s, _id: True)
    client, headers = session_client
    decision = {"version": 1, "reason": "", "idempotency_key": "scope-decide-1"}
    for action in ("approve", "reject", "revise"):
        response = client.post(f"/control/approvals/{DUMMY_ID}/{action}", json=decision, headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "RESULT_ACCEPTANCE_REQUIRES_STEP_UP"


def test_a_scope_denial_is_logged_by_template_only(session_client: tuple[TestClient, dict[str, str]]) -> None:
    import structlog

    client, headers = session_client
    with structlog.testing.capture_logs() as events:
        client.post(f"/memory/facts/{DUMMY_ID}/promote", json={}, headers=headers)
    denied = [e for e in events if e.get("event") == "identity.session.scope_denied"]
    assert denied and denied[0]["route"] == "/memory/facts/{memory_id}/promote" and denied[0]["method"] == "POST"
    assert DUMMY_ID not in repr(denied) and headers["X-PADOS-CSRF"] not in repr(denied)
