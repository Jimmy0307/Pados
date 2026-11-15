# SPDX-License-Identifier: AGPL-3.0-only
"""What an OIDC actor session may do (design §4): exactly ``GET /me`` and, with ``contributor`` on the project,
work-request creation through the shared creation function. Never the control plane, device management, pairing,
approvals or result acceptance."""
from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS
from tests.identity.oidc.oidc_harness import PROJECT_A, PROJECT_B, OidcHarness, write_headers

WORK = {"title": "Actor work", "body": "Created by a bound SSO actor.", "idempotency_key": "actor-wr-1"}
SOME_ID = "00000000-0000-0000-0000-0000000000aa"


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def _create(client: TestClient, csrf: str, project: str = PROJECT_A, body: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> Any:
    return client.post(f"/identity/v1/projects/{project}/work-requests", json=body or WORK,
                       headers=write_headers(csrf) if headers is None else headers)


# --- the role model -----------------------------------------------------------------------------------------------
def test_contributor_creates_through_the_shared_creation_function(oidc: OidcHarness,
                                                                  created: list[dict[str, Any]]) -> None:
    client, csrf = oidc.signed_in("entra-sub-alice")
    response = _create(client, csrf)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert created == [{"project_id": PROJECT_A, "title": WORK["title"], "body": WORK["body"],
                        "idempotency_key": "actor:alice:actor-wr-1"}]


def test_the_control_route_and_the_actor_route_share_one_creation_function(oidc: OidcHarness,
                                                                           created: list[dict[str, Any]]) -> None:
    client = oidc.client()
    control = client.post("/control/work-requests", headers=TOKEN_HEADERS,
                          json={"project_id": PROJECT_A, **WORK})
    assert control.status_code == 200 and created[-1]["idempotency_key"] == "actor-wr-1"
    actor, csrf = oidc.signed_in("entra-sub-alice")
    assert _create(actor, csrf).status_code == 200 and created[-1]["idempotency_key"] == "actor:alice:actor-wr-1"


def test_viewer_cannot_create(oidc: OidcHarness, created: list[dict[str, Any]]) -> None:
    client, csrf = oidc.signed_in("entra-sub-carol")
    response = _create(client, csrf)
    assert response.status_code == 403 and _code(response) == "ACTOR_ROLE_REQUIRED" and created == []


def test_approver_role_grants_no_creation(oidc: OidcHarness, created: list[dict[str, Any]]) -> None:
    client, csrf = oidc.signed_in("entra-sub-dave")
    assert client.get("/identity/v1/me").json()["projects"] == [{"project_id": PROJECT_A, "roles": ["approver"]}]
    response = _create(client, csrf)
    assert response.status_code == 403 and _code(response) == "ACTOR_ROLE_REQUIRED" and created == []


def test_contributor_on_a_cannot_create_in_b(oidc: OidcHarness, created: list[dict[str, Any]]) -> None:
    client, csrf = oidc.signed_in("entra-sub-alice")  # contributor on A, viewer on B
    for project in (PROJECT_B, "33333333-3333-4333-8333-333333333333"):
        response = _create(client, csrf, project)
        assert response.status_code == 403 and _code(response) == "ACTOR_ROLE_REQUIRED"
    assert created == []


def test_cross_account_a_session_cannot_act_as_another_actor(oidc: OidcHarness,
                                                            created: list[dict[str, Any]]) -> None:
    alice, alice_csrf = oidc.signed_in("entra-sub-alice")
    bob, bob_csrf = oidc.signed_in("google-sub-bob", "google")  # contributor on B only
    # Bob cannot borrow Alice's project by naming her or the project in the body.
    for extra in ({"actor_id": "alice"}, {"project_id": PROJECT_A}, {"subject": "entra-sub-alice"}):
        response = _create(bob, bob_csrf, PROJECT_A, body={**WORK, **extra})
        assert response.status_code == 422
    assert _code(_create(bob, bob_csrf, PROJECT_A)) == "ACTOR_ROLE_REQUIRED"
    # Alice's CSRF token does not work with Bob's session, nor Bob's with Alice's.
    assert _code(_create(bob, alice_csrf, PROJECT_B)) == "CSRF_INVALID"
    assert _code(_create(alice, bob_csrf, PROJECT_A)) == "CSRF_INVALID"
    assert created == []
    assert _create(bob, bob_csrf, PROJECT_B).status_code == 200
    assert created[-1]["idempotency_key"] == "actor:bob:actor-wr-1"
    assert alice.get("/identity/v1/me").json()["actor_id"] == "alice"
    assert bob.get("/identity/v1/me").json()["actor_id"] == "bob"


@pytest.mark.parametrize(("headers", "code"), [
    ({"Origin": ORIGIN}, "CSRF_INVALID"),
    ({"X-PADOS-CSRF": "<csrf>"}, "ORIGIN_NOT_ALLOWED"),
    ({"Origin": "http://evil.example", "X-PADOS-CSRF": "<csrf>"}, "ORIGIN_NOT_ALLOWED"),
    ({"Origin": ORIGIN, "X-PADOS-CSRF": "<csrf>", "Sec-Fetch-Site": "cross-site"}, "ORIGIN_NOT_ALLOWED"),
])
def test_actor_writes_need_csrf_and_origin(oidc: OidcHarness, created: list[dict[str, Any]],
                                           headers: dict[str, str], code: str) -> None:
    client, csrf = oidc.signed_in("entra-sub-alice")
    sent = {key: csrf if value == "<csrf>" else value for key, value in headers.items()}
    response = _create(client, csrf, headers=sent)
    assert response.status_code == 403 and _code(response) == code and created == []


def test_removing_a_binding_ends_its_authority(oidc: OidcHarness, created: list[dict[str, Any]]) -> None:
    from pados.identity.actors import ActorDirectory

    client, csrf = oidc.signed_in("entra-sub-alice")
    oidc.harness.app.state.pados_oidc.actors = ActorDirectory()
    assert _code(client.get("/identity/v1/me")) == "SSO_IDENTITY_NOT_BOUND"
    assert _code(_create(client, csrf)) == "SSO_IDENTITY_NOT_BOUND"
    assert "actor" not in client.get("/identity/v1/session").json()
    assert created == []


def test_me_needs_an_actor_session(oidc: OidcHarness) -> None:
    client = oidc.client()
    assert _code(client.get("/identity/v1/me")) == "SESSION_REQUIRED"
    paired = oidc.harness.paired(client)  # a device session is not an actor session
    assert paired["device_id"] and _code(client.get("/identity/v1/me")) == "SESSION_REQUIRED"
    assert _code(client.post(f"/identity/v1/projects/{PROJECT_A}/work-requests", json=WORK,
                             headers=write_headers(str(paired["csrf_token"])))) == "SESSION_REQUIRED"


# --- never the control plane --------------------------------------------------------------------------------------
def _control_routes(oidc: OidcHarness) -> list[tuple[str, str]]:
    found = []
    for route in oidc.harness.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/control"):
            path = re.sub(r"\{[^}]+\}", SOME_ID, route.path)
            found.extend((method, path) for method in sorted(route.methods))
    return found


def test_an_oidc_session_is_refused_by_every_control_route(oidc: OidcHarness, created: list[dict[str, Any]],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.control import service as control_service

    reached: list[str] = []
    for name in ("decide_approval", "list_projects", "list_approvals", "get_approval", "create_approval"):
        monkeypatch.setattr(control_service, name, lambda *a, _n=name, **k: reached.append(_n))
    client, csrf = oidc.signed_in("entra-sub-alice")
    actor_cookie = str(client.cookies.get("pados_actor"))
    routes = _control_routes(oidc)
    assert len(routes) >= 20 and ("POST", "/control/work-requests") in routes
    smuggled = oidc.client()
    smuggled.cookies.set("pados_session", actor_cookie)  # the actor id presented as a device session
    anonymous = oidc.client()
    protected = 0
    for method, path in routes:
        baseline = anonymous.request(method, path, json={}, headers=write_headers(csrf))
        if baseline.status_code != 403:  # a public route (``/control/health``): the session changes nothing
            assert client.request(method, path, json={}, headers=write_headers(csrf)).status_code \
                == baseline.status_code
            continue
        protected += 1
        for browser, expected in ((client, "missing control token"), (smuggled, None)):
            response = browser.request(method, path, json={}, headers=write_headers(csrf))
            assert response.status_code == 403, (method, path, response.status_code)
            if expected is not None:
                assert response.json()["detail"] == expected, (method, path)
            else:
                assert _code(response) == "SESSION_REQUIRED", (method, path)
    assert protected >= 20 and created == [] and reached == []


def test_an_oidc_session_cannot_decide_a_result_acceptance_approval(oidc: OidcHarness,
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   no_database: None) -> None:
    from pados.control import service as control_service

    decided: list[object] = []
    monkeypatch.setattr(control_service, "decide_approval", lambda *a, **k: decided.append(a))
    monkeypatch.setattr(control_service, "_backs_a_result_candidate", lambda _s, _id: True)
    client, csrf = oidc.signed_in("entra-sub-dave")  # even the approver role
    decision = {"version": 1, "reason": "", "idempotency_key": "sso-decide-1"}
    for action in ("approve", "reject", "revise", "revoke"):
        response = client.post(f"/control/approvals/{SOME_ID}/{action}", json=decision, headers=write_headers(csrf))
        assert response.status_code == 403 and response.json()["detail"] == "missing control token"
    assert decided == []


def test_an_oidc_session_has_no_device_or_pairing_authority(oidc: OidcHarness) -> None:
    owner = oidc.client()
    device = oidc.harness.paired(owner)["device_id"]
    client, csrf = oidc.signed_in("entra-sub-alice")
    assert _code(client.get("/identity/v1/devices")) == "SESSION_REQUIRED"
    revoke = client.post(f"/identity/v1/devices/{device}/revoke", json={}, headers=write_headers(csrf))
    assert revoke.status_code == 403 and _code(revoke) == "SESSION_REQUIRED"
    mint = client.post("/identity/v1/pairing-codes", json={}, headers=write_headers(csrf))
    assert mint.status_code == 403
    assert owner.get("/identity/v1/session").json()["device_id"] == device  # untouched


def test_device_and_actor_sessions_coexist_without_mixing(oidc: OidcHarness) -> None:
    """One browser holding both: /session reports the device session and the actor apart; each keeps its CSRF."""
    client = oidc.client()
    paired = oidc.harness.paired(client)
    assert oidc.sign_in(client, "entra-sub-alice").headers["location"] == "/?sso=ok"
    session = client.get("/identity/v1/session").json()
    assert session["authenticated"] is True and session["device_id"] == paired["device_id"]
    assert session["auth_method"] == "device_pairing" and session["actor"]["actor_id"] == "alice"
    actor_csrf = client.get("/identity/v1/me").json()["csrf_token"]
    assert actor_csrf != session["csrf_token"]
    assert _code(_create(client, session["csrf_token"])) == "CSRF_INVALID"  # the device CSRF is not the actor's


def test_an_oidc_session_grants_nothing_on_the_remote_surface(oidc: OidcHarness) -> None:
    client, csrf = oidc.signed_in("entra-sub-dave")
    for method, path in (("GET", "/remote/v1/approvals"), ("POST", "/remote/v1/approvals/any-ref/approve"),
                         ("POST", "/remote/v1/approvals/any-ref/step-up")):
        response = client.request(method, path, json={}, headers=write_headers(csrf))
        assert response.status_code in (401, 403, 404), (method, path, response.status_code)
        assert "challenge_id" not in response.text
