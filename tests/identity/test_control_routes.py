# SPDX-License-Identifier: AGPL-3.0-only
"""A device session on the control plane: exactly the control-token authority, with CSRF and Origin rules, and
nothing more (design §2, §3.4)."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.identity.identity_support import CONTROL_VALUE, ORIGIN, TOKEN_HEADERS, Harness

WORK_REQUEST = {"project_id": "00000000-0000-0000-0000-000000000001", "title": "Session work",
                "body": "Created through a paired browser session.", "idempotency_key": "identity-session-1"}
APPROVAL = "00000000-0000-0000-0000-0000000000aa"


def _code(response_json: dict[str, Any]) -> str:
    return str(response_json["error"]["code"])


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, no_database: None) -> list[dict[str, Any]]:
    from pados.control import service as control_service

    calls: list[dict[str, Any]] = []

    def create(_session: object, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"id": "wr-1", **payload}

    monkeypatch.setattr(control_service, "create_work_request", create)
    monkeypatch.setattr(control_service, "list_projects", lambda _session: [{"id": "p-1"}])
    return calls


def _paired(harness: Harness) -> tuple[TestClient, str]:
    client = harness.client()
    return client, str(harness.paired(client)["csrf_token"])


def test_session_creates_a_work_request_with_csrf_and_origin(harness: Harness, created: list[dict[str, Any]]) -> None:
    client, csrf = _paired(harness)
    response = client.post("/control/work-requests", json=WORK_REQUEST,
                           headers={"Origin": ORIGIN, "X-PADOS-CSRF": csrf, "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200, response.text
    assert response.json()["work_request"]["id"] == "wr-1" and len(created) == 1


@pytest.mark.parametrize(("headers", "code"), [
    ({"Origin": ORIGIN}, "CSRF_INVALID"),
    ({"Origin": ORIGIN, "X-PADOS-CSRF": "forged"}, "CSRF_INVALID"),
    ({"X-PADOS-CSRF": "<csrf>"}, "ORIGIN_NOT_ALLOWED"),
    ({"Origin": "http://evil.example", "X-PADOS-CSRF": "<csrf>"}, "ORIGIN_NOT_ALLOWED"),
    ({"Origin": ORIGIN, "X-PADOS-CSRF": "<csrf>", "Sec-Fetch-Site": "cross-site"}, "ORIGIN_NOT_ALLOWED"),
    ({"Origin": ORIGIN, "X-PADOS-CSRF": "<csrf>", "Sec-Fetch-Site": "same-site"}, "ORIGIN_NOT_ALLOWED"),
])
def test_session_writes_are_refused_without_every_rule(harness: Harness, created: list[dict[str, Any]],
                                                         headers: dict[str, str], code: str) -> None:
    client, csrf = _paired(harness)
    sent = {key: csrf if value == "<csrf>" else value for key, value in headers.items()}
    response = client.post("/control/work-requests", json=WORK_REQUEST, headers=sent)
    assert response.status_code == 403 and _code(response.json()) == code
    assert created == []


def test_csrf_of_another_session_is_refused(harness: Harness, created: list[dict[str, Any]]) -> None:
    client, _ = _paired(harness)
    _, other_csrf = _paired(harness)
    response = client.post("/control/work-requests", json=WORK_REQUEST,
                           headers={"Origin": ORIGIN, "X-PADOS-CSRF": other_csrf})
    assert response.status_code == 403 and _code(response.json()) == "CSRF_INVALID" and created == []


def test_session_reads_work_and_reads_obey_origin(harness: Harness, created: list[dict[str, Any]]) -> None:
    client, _ = _paired(harness)
    assert client.get("/control/projects").json()["projects"] == [{"id": "p-1"}]
    assert client.get("/control/projects", headers={"Origin": ORIGIN}).status_code == 200
    foreign = client.get("/control/projects", headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403 and _code(foreign.json()) == "ORIGIN_NOT_ALLOWED"
    cross = client.get("/control/projects", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and _code(cross.json()) == "ORIGIN_NOT_ALLOWED"


def test_memory_routes_accept_the_session(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                                          no_database: None) -> None:
    from pados.api import app as api_app

    monkeypatch.setattr(api_app, "search_memory", lambda _session, _request: [])
    client, _ = _paired(harness)
    response = client.get("/memory/search", params={"project_id": "p-1", "query": "x"})
    assert response.status_code == 200, response.text


def test_an_invalid_token_header_is_refused_even_with_a_valid_cookie(harness: Harness,
                                                                       created: list[dict[str, Any]]) -> None:
    client, csrf = _paired(harness)
    for presented in ("wrong", ""):
        response = client.post("/control/work-requests", json=WORK_REQUEST,
                               headers={"X-PADOS-Control-Token": presented, "Origin": ORIGIN, "X-PADOS-CSRF": csrf})
        assert response.status_code == 403
    assert created == []
    assert client.post("/control/work-requests", json=WORK_REQUEST, headers=TOKEN_HEADERS).status_code == 200


def test_a_valid_token_header_decides_over_a_revoked_or_garbage_cookie(harness: Harness,
                                                                       created: list[dict[str, Any]]) -> None:
    client, _ = _paired(harness)
    device = client.get("/identity/v1/session").json()["device_id"]
    harness.client().post(f"/identity/v1/devices/{device}/revoke", headers=TOKEN_HEADERS)
    assert client.get("/control/projects").status_code == 403  # the revoked cookie alone is refused
    for cookie in (str(client.cookies.get("pados_session")), "garbage"):
        browser = harness.client()
        browser.cookies.set("pados_session", cookie)
        assert browser.get("/control/projects", headers=TOKEN_HEADERS).status_code == 200
        assert browser.post("/control/work-requests", json=WORK_REQUEST, headers=TOKEN_HEADERS).status_code == 200
    assert len(created) == 2


def test_revoked_and_expired_cookies_are_refused_with_their_code(harness: Harness,
                                                                  created: list[dict[str, Any]]) -> None:
    client, csrf = _paired(harness)
    harness.clock.advance(7201)
    expired = client.get("/control/projects")
    assert expired.status_code == 403 and _code(expired.json()) == "SESSION_EXPIRED"
    other, other_csrf = _paired(harness)
    device = other.get("/identity/v1/session").json()["device_id"]
    harness.client().post(f"/identity/v1/devices/{device}/revoke", headers=TOKEN_HEADERS)
    revoked = other.post("/control/work-requests", json=WORK_REQUEST,
                         headers={"Origin": ORIGIN, "X-PADOS-CSRF": other_csrf})
    assert revoked.status_code == 403 and _code(revoked.json()) == "SESSION_REVOKED" and created == []


def _strip_ids(body: dict[str, Any]) -> dict[str, Any]:
    error = {k: v for k, v in body["error"].items() if k not in {"request_id", "correlation_id"}}
    return {**body, "error": error}


def test_flag_off_ignores_the_cookie_and_answers_exactly_as_before(make_harness: Callable[..., Harness],
                                                                    created: list[dict[str, Any]]) -> None:
    on = make_harness()
    client, csrf = _paired(on)
    cookie = str(client.cookies.get("pados_session"))
    off = make_harness(enabled=False, store_dir=on.store_dir, clock=on.clock)
    with_cookie = off.client()
    with_cookie.cookies.set("pados_session", cookie)
    plain = off.client()
    headers = {"Origin": ORIGIN, "X-PADOS-CSRF": csrf}
    for method, path, body in (("GET", "/control/projects", None), ("POST", "/control/work-requests", WORK_REQUEST)):
        refused = with_cookie.request(method, path, json=body, headers=headers)
        baseline = plain.request(method, path, json=body, headers=headers)
        assert refused.status_code == baseline.status_code == 403
        assert _strip_ids(refused.json()) == _strip_ids(baseline.json())
        assert refused.json()["detail"] == "missing control token"
        assert "set-cookie" not in refused.headers
    assert created == []
    assert with_cookie.get("/control/projects", headers=TOKEN_HEADERS).status_code == 200


def test_a_session_cannot_decide_a_result_candidate_approval(harness: Harness, monkeypatch: pytest.MonkeyPatch,
                                                            no_database: None) -> None:
    """The step-up refusal in ``decide_approval`` holds for a session exactly as for the control token."""
    from pados.control import service as control_service

    monkeypatch.setattr(control_service, "get_approval", lambda _s, _id: {
        "id": APPROVAL, "status": "PENDING", "version": 1, "decision_reason": None, "subject_kind": None})
    monkeypatch.setattr(control_service, "_backs_a_result_candidate", lambda _s, _id: True)
    client, csrf = _paired(harness)
    decision = {"version": 1, "reason": "", "idempotency_key": "identity-decide-1"}
    for action in ("approve", "reject", "revise"):
        response = client.post(f"/control/approvals/{APPROVAL}/{action}", json=decision,
                               headers={"Origin": ORIGIN, "X-PADOS-CSRF": csrf})
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "RESULT_ACCEPTANCE_REQUIRES_STEP_UP"


def test_a_session_grants_nothing_on_the_remote_actor_surface(tmp_path: Path) -> None:
    from pados.config import Settings
    from pados.identity.routes import mount_identity
    from pados.identity.sessions import IdentityService
    from tests.remote.remote_slice_support import build_slice

    remote = build_slice(tmp_path / "remote")
    settings = Settings(PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper(),
                        PADOS_IDENTITY_SESSIONS_ENABLED=True, PADOS_IDENTITY_STORE_DIR=str(tmp_path / "identity"))
    app = remote.client.app
    mount_identity(app, settings, service=IdentityService(settings))  # type: ignore[arg-type]
    client = TestClient(app)  # type: ignore[arg-type]
    code = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS).json()["pairing_code"]
    csrf = client.post("/identity/v1/pair", json={"pairing_code": code}, headers={"Origin": ORIGIN}).json()["csrf_token"]
    headers = {"Origin": ORIGIN, "X-PADOS-CSRF": csrf}
    listed = client.get("/remote/v1/approvals", headers=headers)
    assert listed.status_code == 401
    decided = client.post("/remote/v1/approvals/any-ref/approve", json={"proof_ref": "x"}, headers=headers)
    assert decided.status_code == 401
    step_up = client.post("/remote/v1/approvals/any-ref/step-up", json={"action": "APPROVE"}, headers=headers)
    assert step_up.status_code in (401, 403) and "challenge_id" not in step_up.text
    assert client.get("/remote/v1/approvals", headers=remote.owner_headers).status_code == 200


def test_graph_runs_still_require_the_control_token_header(harness: Harness) -> None:
    """The graph run gate reads only the header: a session never widens into it."""
    client, csrf = _paired(harness)
    response = client.post("/api/graph/workspaces/w-1/selection/run", json={"node_ids": ["n-1"]},
                           headers={"Origin": ORIGIN, "X-PADOS-CSRF": csrf})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "CONTROL_AUTH_REQUIRED"
