# SPDX-License-Identifier: AGPL-3.0-only
"""``POST /identity/v1/actor/logout`` ends only the presented actor session (design §4.2, §3.10).

The Control Center's SSO section needs it: with a paired device session in the same browser, ``POST /logout`` ends
the device session, so the actor session needs its own exit. It takes authority away only, and it obeys every rule
of an actor write (cookie, CSRF, Origin).
"""
from __future__ import annotations

from tests.identity.identity_support import ORIGIN
from tests.identity.oidc.oidc_harness import OidcHarness, write_headers


def test_actor_logout_ends_the_actor_session_and_leaves_the_device_session(oidc: OidcHarness) -> None:
    client, csrf = oidc.signed_in()
    device = oidc.harness.pair(client, oidc.harness.mint(client))
    assert device.status_code == 200
    ended = client.post("/identity/v1/actor/logout", json={}, headers=write_headers(csrf))
    assert ended.status_code == 200 and ended.json() == {"status": "ok", "authenticated": False}
    assert ended.headers["cache-control"] == "no-store"
    assert "pados_actor=" in ended.headers["set-cookie"] and "max-age=0" in ended.headers["set-cookie"].lower()
    assert client.get("/identity/v1/me").status_code == 403
    session = client.get("/identity/v1/session").json()
    assert session["authenticated"] is True and "actor" not in session  # the paired device session is untouched


def test_actor_logout_needs_csrf_and_origin(oidc: OidcHarness) -> None:
    client, csrf = oidc.signed_in()
    for headers in ({"Origin": ORIGIN}, {"X-PADOS-CSRF": csrf}, {"Origin": "http://127.0.0.1:9999", "X-PADOS-CSRF": csrf},
                    {"Origin": ORIGIN, "X-PADOS-CSRF": "forged"}):
        refused = client.post("/identity/v1/actor/logout", json={}, headers=headers)
        assert refused.status_code == 403, headers
    assert client.get("/identity/v1/me").status_code == 200  # still signed in


def test_actor_logout_without_an_actor_session_is_refused(oidc: OidcHarness) -> None:
    client = oidc.client()
    refused = client.post("/identity/v1/actor/logout", json={}, headers={"Origin": ORIGIN, "X-PADOS-CSRF": "x"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "SESSION_REQUIRED"
