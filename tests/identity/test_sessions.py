# SPDX-License-Identifier: AGPL-3.0-only
"""Sessions and devices: reload, logout, revoke, lifetimes, fixation, forged cookies (design §3.3-§3.5)."""
from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS, Harness


def _code(response_json: dict[str, object]) -> str:
    return str(response_json["error"]["code"])  # type: ignore[index]


def _session_cookie(client: TestClient) -> str:
    return str(client.cookies.get("pados_session"))


def test_reload_restores_the_session_with_the_same_csrf(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    restored = client.get("/identity/v1/session")
    assert restored.status_code == 200
    body = restored.json()
    assert body["enabled"] is True and body["authenticated"] is True
    assert body["device_id"] == paired["device_id"] and body["auth_method"] == "device_pairing"
    assert body["csrf_token"] == paired["csrf_token"] and body["providers"] == []
    assert set(body) == {"enabled", "authenticated", "device_id", "device_label", "auth_method", "expires_at",
                         "idle_expires_at", "csrf_token", "providers"}
    assert restored.headers["cache-control"] == "no-store"


def test_without_a_cookie_session_is_an_unauthenticated_200(harness: Harness) -> None:
    response = harness.client().get("/identity/v1/session")
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "authenticated": False, "providers": []}


def test_session_from_a_foreign_origin_is_refused(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    foreign = client.get("/identity/v1/session", headers={"Origin": "http://evil.example"})
    assert foreign.status_code == 403 and _code(foreign.json()) == "ORIGIN_NOT_ALLOWED"
    cross = client.get("/identity/v1/session", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and "csrf_token" not in cross.text


def test_logout_revokes_only_the_current_session(harness: Harness) -> None:
    one, two = harness.client(), harness.client()
    first, second = harness.paired(one), harness.paired(two)
    stale = _session_cookie(one)
    response = one.post("/identity/v1/logout", headers={"Origin": ORIGIN, "X-PADOS-CSRF": first["csrf_token"]})
    assert response.status_code == 200
    assert "pados_session=" in response.headers["set-cookie"] and "max-age=0" in response.headers["set-cookie"].lower()
    assert one.get("/identity/v1/session").json()["authenticated"] is False
    replay = harness.client()
    replay.cookies.set("pados_session", stale)
    refused = replay.get("/identity/v1/devices")
    assert refused.status_code == 403 and _code(refused.json()) == "SESSION_REVOKED"
    assert two.get("/identity/v1/session").json()["device_id"] == second["device_id"]


def test_logout_requires_csrf_and_origin(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    no_csrf = client.post("/identity/v1/logout", headers={"Origin": ORIGIN})
    assert no_csrf.status_code == 403 and _code(no_csrf.json()) == "CSRF_INVALID"
    no_origin = client.post("/identity/v1/logout", headers={"X-PADOS-CSRF": paired["csrf_token"]})
    assert no_origin.status_code == 403 and _code(no_origin.json()) == "ORIGIN_NOT_ALLOWED"
    assert client.get("/identity/v1/session").json()["authenticated"] is True
    anonymous = harness.client().post("/identity/v1/logout", headers={"Origin": ORIGIN})
    assert anonymous.status_code == 403 and _code(anonymous.json()) == "SESSION_REQUIRED"


def test_logout_with_a_dead_session_clears_the_cookie(harness: Harness) -> None:
    """Review P3: an expired or revoked session is refused, and its cookie is cleared so it stops being sent."""
    client = harness.client()
    paired = harness.paired(client)
    harness.clock.advance(7201)  # past the idle timeout
    response = client.post("/identity/v1/logout", headers={"Origin": ORIGIN, "X-PADOS-CSRF": paired["csrf_token"]})
    assert response.status_code == 403 and _code(response.json()) == "SESSION_EXPIRED"
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("pados_session=") and "max-age=0" in cookie
    # A CSRF or Origin refusal on a live session keeps the cookie (not the cookie's fault).
    live = harness.client()
    harness.paired(live)
    refused = live.post("/identity/v1/logout", headers={"Origin": ORIGIN})
    assert _code(refused.json()) == "CSRF_INVALID" and "set-cookie" not in refused.headers


def test_device_list_by_token_and_by_session(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    by_session = client.get("/identity/v1/devices").json()["devices"]
    assert [d["device_id"] for d in by_session] == [paired["device_id"]] and by_session[0]["current"] is True
    by_token = harness.client().get("/identity/v1/devices", headers=TOKEN_HEADERS).json()["devices"]
    assert by_token[0]["current"] is False
    assert set(by_token[0]) == {"device_id", "device_label", "paired_at", "last_seen_at", "revoked_at", "current"}
    assert harness.client().get("/identity/v1/devices").status_code == 403


def test_an_invalid_token_header_is_refused_even_with_a_valid_cookie(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    response = client.get("/identity/v1/devices", headers={"X-PADOS-Control-Token": "wrong"})
    assert response.status_code == 403 and "invalid control token" in response.text


def test_revoking_a_device_revokes_its_sessions(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    operator = harness.client()
    response = operator.post(f"/identity/v1/devices/{paired['device_id']}/revoke", headers=TOKEN_HEADERS)
    assert response.status_code == 200 and response.json()["revoked_at"]
    refused = client.get("/identity/v1/devices")
    assert refused.status_code == 403 and _code(refused.json()) == "SESSION_REVOKED"
    assert client.get("/identity/v1/session").json()["authenticated"] is False
    listed = operator.get("/identity/v1/devices", headers=TOKEN_HEADERS).json()["devices"]
    assert listed[0]["revoked_at"] is not None


def test_a_session_may_revoke_its_own_device_with_csrf(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    path = f"/identity/v1/devices/{paired['device_id']}/revoke"
    assert client.post(path, headers={"Origin": ORIGIN}).status_code == 403
    response = client.post(path, headers={"Origin": ORIGIN, "X-PADOS-CSRF": paired["csrf_token"]})
    assert response.status_code == 200 and "max-age=0" in response.headers["set-cookie"].lower()


def test_revoking_an_unknown_device(harness: Harness) -> None:
    response = harness.client().post("/identity/v1/devices/dev_0123456789abcdef/revoke", headers=TOKEN_HEADERS)
    assert response.status_code == 404 and _code(response.json()) == "DEVICE_NOT_FOUND"


def test_absolute_expiry(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(PADOS_IDENTITY_SESSION_TTL_SECONDS="600", PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS="300")
    client = harness.client()
    harness.paired(client)
    for step in (200, 200, 150):  # keep it busy so the idle timeout never ends it
        harness.clock.advance(step)
        assert client.get("/identity/v1/devices").status_code == 200
    harness.clock.advance(60)  # 610 s after pairing, 60 s after the last use
    expired = client.get("/identity/v1/devices")
    assert expired.status_code == 403 and _code(expired.json()) == "SESSION_EXPIRED"


def test_idle_timeout_slides_on_use(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS="300")
    client = harness.client()
    harness.paired(client)
    harness.clock.advance(250)
    assert client.get("/identity/v1/devices").status_code == 200  # slides last_seen to now
    harness.clock.advance(250)
    assert client.get("/identity/v1/devices").status_code == 200
    harness.clock.advance(301)
    idle = client.get("/identity/v1/devices")
    assert idle.status_code == 403 and _code(idle.json()) == "SESSION_EXPIRED"


def test_last_seen_is_written_at_most_once_a_minute(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    store = harness.store_dir / "sessions.json"
    harness.clock.advance(30)
    before = store.read_bytes()
    assert client.get("/identity/v1/devices").status_code == 200
    assert store.read_bytes() == before
    harness.clock.advance(31)
    assert client.get("/identity/v1/devices").status_code == 200
    assert store.read_bytes() != before


def test_fixation_a_preset_cookie_is_never_adopted(harness: Harness) -> None:
    client = harness.client()
    planted = secrets.token_urlsafe(32)
    client.cookies.set("pados_session", planted)
    response = harness.pair(client, harness.mint(client))
    issued = response.cookies.get("pados_session")
    assert issued and issued != planted
    attacker = harness.client()
    attacker.cookies.set("pados_session", planted)
    refused = attacker.get("/identity/v1/devices")
    assert refused.status_code == 403 and _code(refused.json()) == "SESSION_REQUIRED"


def test_pairing_with_a_valid_cookie_revokes_it_and_issues_a_new_id(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    old = _session_cookie(client)
    response = harness.pair(client, harness.mint(client))
    new = response.cookies.get("pados_session")
    assert new and new != old
    replay = harness.client()
    replay.cookies.set("pados_session", old)
    refused = replay.get("/identity/v1/devices")
    assert refused.status_code == 403 and _code(refused.json()) == "SESSION_REVOKED"
    assert client.get("/identity/v1/session").json()["authenticated"] is True


def test_forged_and_malformed_cookies_are_refused(harness: Harness) -> None:
    harness.paired(harness.client())
    for forged in (secrets.token_urlsafe(32), "x", "a" * 400, "../../etc", "%00"):
        client = harness.client()
        client.cookies.set("pados_session", forged)
        response = client.get("/identity/v1/devices")
        assert response.status_code == 403 and _code(response.json()) == "SESSION_REQUIRED"
        assert client.get("/identity/v1/session").json()["authenticated"] is False


def test_rotating_the_control_token_ends_every_session(make_harness: Callable[..., Harness]) -> None:
    import hashlib

    harness = make_harness()
    client = harness.client()
    harness.paired(client)
    cookie = _session_cookie(client)
    rotated = make_harness(store_dir=harness.store_dir, clock=harness.clock,
                           PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(b"rotated-value").hexdigest().upper())
    after = rotated.client()
    after.cookies.set("pados_session", cookie)
    refused = after.get("/control/projects")
    assert refused.status_code == 403 and _code(refused.json()) == "SESSION_REVOKED"


def test_the_device_limit_revokes_the_oldest(harness: Harness) -> None:
    clients = []
    for _ in range(17):
        client = harness.client()
        harness.paired(client)
        clients.append(client)
        harness.clock.advance(1)
    devices = harness.client().get("/identity/v1/devices", headers=TOKEN_HEADERS).json()["devices"]
    active = [d for d in devices if d["revoked_at"] is None]
    assert len(active) == 16
    oldest = clients[0].get("/identity/v1/devices")
    assert oldest.status_code == 403 and _code(oldest.json()) == "SESSION_REVOKED"
    assert clients[1].get("/identity/v1/devices").status_code == 200


def test_logout_and_revoke_accept_the_web_client_empty_json_body(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    headers = {"Origin": ORIGIN, "X-PADOS-CSRF": paired["csrf_token"], "Content-Type": "application/json"}
    other = harness.client()
    other_device = harness.paired(other)["device_id"]
    revoked = client.post(f"/identity/v1/devices/{other_device}/revoke", content=b"{}", headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert client.post("/identity/v1/logout", content=b"{}", headers=headers).status_code == 200
    assert client.get("/identity/v1/session").json()["authenticated"] is False


def test_the_token_header_decides_whatever_cookie_comes_with_it(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    stale = _session_cookie(client)
    harness.client().post(f"/identity/v1/devices/{paired['device_id']}/revoke", headers=TOKEN_HEADERS)
    for cookie in (stale, "garbage", secrets.token_urlsafe(32)):
        browser = harness.client()
        browser.cookies.set("pados_session", cookie)
        listed = browser.get("/identity/v1/devices", headers=TOKEN_HEADERS)
        assert listed.status_code == 200 and listed.json()["devices"][0]["current"] is False
