# SPDX-License-Identifier: AGPL-3.0-only
"""No authorization code, state, nonce, PKCE verifier, ID token, client credential, actor session id or CSRF token in
logs, in redirect URLs to PADOS, in the store, or in any response body (design §2.5, §4)."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import structlog

from tests.identity.oidc.oidc_harness import OidcHarness, write_headers
from tests.identity.oidc.oidc_support import ENTRA_CLIENT_CREDENTIAL, GOOGLE_CLIENT_CREDENTIAL


def _flows(oidc: OidcHarness) -> dict[str, Any]:
    """A happy sign-in, refusals of several kinds, /me, a write and a logout; everything sensitive recorded."""
    sensitive: list[str] = [ENTRA_CLIENT_CREDENTIAL, GOOGLE_CLIENT_CREDENTIAL]
    responses = []
    client = oidc.client()
    started = oidc.start(client)
    query = {k: v[0] for k, v in parse_qs(urlsplit(started.headers["location"]).query).items()}
    sensitive += [query["state"], query["nonce"], str(client.cookies.get("pados_oidc_preauth"))]
    params = oidc.entra.authorize(started.headers["location"], subject="entra-sub-alice")
    sensitive.append(params["code"])
    done = oidc.callback(client, params)
    responses += [started, done]
    sensitive.append(oidc.entra.token_requests[-1]["code_verifier"])
    sensitive.append(oidc.entra.issued_tokens[-1])
    sensitive.append(str(client.cookies.get("pados_actor")))
    me = client.get("/identity/v1/me")
    sensitive.append(me.json()["csrf_token"])
    responses += [me, client.get("/identity/v1/session"), oidc.callback(client, params)]  # replay refused
    responses.append(client.post("/identity/v1/projects/p-1/work-requests", json={"title": "t", "body": "b",
                                 "idempotency_key": "k"}, headers=write_headers(me.json()["csrf_token"])))
    for subject, claims in (("entra-sub-stranger", {}), ("entra-sub-alice", {"nonce": "wrong"})):
        other = oidc.client()
        other_start = oidc.start(other)
        other_params = oidc.entra.authorize(other_start.headers["location"], subject=subject, claims=claims)
        sensitive.append(other_params["code"])
        responses += [other_start, oidc.callback(other, other_params)]
        sensitive.append(oidc.entra.issued_tokens[-1])
    responses.append(client.post("/identity/v1/logout", json={}, headers=write_headers(me.json()["csrf_token"])))
    return {"sensitive": sensitive, "responses": responses, "done": done, "started": started}


def test_no_oidc_secret_reaches_logs_or_responses(oidc: OidcHarness, capsys: pytest.CaptureFixture[str],
                                                  caplog: pytest.LogCaptureFixture) -> None:
    with structlog.testing.capture_logs() as events:
        flow = _flows(oidc)
    captured = capsys.readouterr()
    logs = repr(events) + captured.out + captured.err + caplog.text
    names = {event.get("event") for event in events}
    assert {"identity.oidc.started", "identity.actor_session.issued", "identity.oidc.refused",
            "identity.oidc.identity_not_bound"} <= names
    for value in flow["sensitive"]:
        assert value and value not in logs, "a sensitive OIDC value reached a log"
    started, done = flow["started"], flow["done"]
    for response in flow["responses"]:
        location = response.headers.get("location", "")
        if response is not started and not location.startswith("https://"):
            # Redirects back into PADOS carry a closed code only.
            assert location in ("", "/?sso=ok") or location.startswith("/?sso_error="), location
            assert all(value not in location for value in flow["sensitive"])
        for value in flow["sensitive"][:2]:  # client credentials: in no response at all
            assert value not in location and value not in response.text and value not in repr(response.headers)
    assert flow["sensitive"][2] in started.headers["location"]  # the state goes to the provider, by design
    assert done.headers["location"] == "/?sso=ok"


def test_the_store_holds_no_oidc_secret(oidc: OidcHarness) -> None:
    flow = _flows(oidc)
    stored = "".join(path.read_bytes().decode("utf-8", "replace")
                     for path in oidc.harness.store_dir.glob("*.json"))
    assert "actor_sessions" in stored
    for value in flow["sensitive"]:
        assert value not in stored


def test_phase_one_documents_stay_free_of_phase_two_keys(oidc: OidcHarness) -> None:
    oidc.harness.paired(oidc.client())
    stored = (oidc.harness.store_dir / "sessions.json").read_text(encoding="utf-8")
    assert "actor_sessions" not in stored and "oidc_states" not in stored
