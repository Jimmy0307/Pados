# SPDX-License-Identifier: AGPL-3.0-only
"""The OIDC relying party end to end against the fake provider: Authorization Code + PKCE S256 + state + nonce,
ID-token validation, and the actor binding gate (design §4)."""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.identity.oidc.oidc_harness import (
    ENTRA_PROVIDER,
    OIDC_CONFIG,
    PROJECT_A,
    PROJECT_B,
    OidcHarness,
    write_headers,
)
from tests.identity.oidc.oidc_support import (
    ENTRA_CLIENT_ID,
    ENTRA_ISSUER,
    ENTRA_TENANT,
    GOOGLE_ISSUER,
    OTHER_TENANT,
    FakeIssuer,
    FakeNetwork,
    b64u,
    compact_jws,
    entra_issuer,
    google_issuer,
    test_rsa_key,
)

SSO_OK = "/?sso=ok"


def _error(response: Any) -> str:
    location = response.headers["location"]
    assert location.startswith("/?sso_error="), location
    return str(location.split("=", 1)[1])


def _actor_cookie(client: Any) -> str | None:
    return client.cookies.get("pados_actor")


# --- happy paths --------------------------------------------------------------------------------------------------
def test_entra_pinned_tenant_sign_in_issues_an_oidc_actor_session(oidc: OidcHarness) -> None:
    client = oidc.client()
    started = oidc.start(client)
    assert started.status_code == 302
    location = urlsplit(started.headers["location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == oidc.entra.authorization_endpoint
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["redirect_uri"] == ENTRA_PROVIDER["redirect_uri"] and query["scope"] == "openid profile email"
    assert len(query["state"]) >= 43 and len(query["nonce"]) >= 43 and query["state"] != query["nonce"]
    assert started.headers["cache-control"] == "no-store" and started.headers["referrer-policy"] == "no-referrer"
    preauth = started.headers["set-cookie"]
    assert preauth.startswith("pados_oidc_preauth=") and "HttpOnly" in preauth and "SameSite=lax" in preauth
    assert "Max-Age=600" in preauth

    params = oidc.entra.authorize(started.headers["location"], subject="entra-sub-alice")
    done = oidc.callback(client, params)
    assert done.status_code == 302 and done.headers["location"] == SSO_OK
    assert _actor_cookie(client) and client.cookies.get("pados_oidc_preauth") is None
    assert client.cookies.get("pados_session") is None  # never the device session cookie

    session = client.get("/identity/v1/session").json()
    # ``authenticated`` stays the device session (the control plane's); the actor session is reported apart.
    assert session["authenticated"] is False and "csrf_token" not in session and "device_id" not in session
    actor = session["actor"]
    assert actor["auth_method"] == "oidc" and actor["provider_id"] == "entra"
    assert (actor["actor_id"], actor["issuer"], actor["subject"]) == ("alice", ENTRA_ISSUER, "entra-sub-alice")
    assert [p["id"] for p in session["providers"]] == ["entra", "google"]
    me = client.get("/identity/v1/me").json()
    assert me["actor_id"] == "alice" and me["display_name"] == "Alice" and me["auth_method"] == "oidc"
    assert me["projects"] == [{"project_id": PROJECT_A, "roles": ["viewer", "contributor"]},
                              {"project_id": PROJECT_B, "roles": ["viewer"]}]
    assert me["csrf_token"] and me["expires_at"] == actor["expires_at"]
    token_form = oidc.entra.token_requests[-1]
    assert token_form["redirect_uri"] == ENTRA_PROVIDER["redirect_uri"] and len(token_form["code_verifier"]) == 43


def test_google_preset_sign_in(oidc: OidcHarness) -> None:
    client = oidc.client()
    assert oidc.sign_in(client, "google-sub-bob", "google").headers["location"] == SSO_OK
    me = client.get("/identity/v1/me").json()
    assert (me["actor_id"], me["issuer"], me["provider_id"]) == ("bob", GOOGLE_ISSUER, "google")


def test_google_bare_issuer_form_is_the_same_identity(oidc: OidcHarness) -> None:
    client = oidc.client()
    done = oidc.sign_in(client, "google-sub-bob", "google", claims={"iss": "accounts.google.com"})
    assert done.headers["location"] == SSO_OK
    assert client.get("/identity/v1/me").json()["issuer"] == GOOGLE_ISSUER


def test_entra_multi_tenant_accepts_only_pinned_tids(make_oidc: Callable[..., OidcHarness]) -> None:
    provider = {**ENTRA_PROVIDER, "tenant": "organizations", "allowed_tenant_ids": [ENTRA_TENANT]}
    harness = make_oidc(oidc_config={**OIDC_CONFIG, "providers": [provider]},
                        network=lambda clock: FakeNetwork(*multi_tenant_fakes(clock)))
    fake = harness.network.issuers[0]
    client = harness.client()
    assert harness.sign_in(client, "entra-sub-alice").headers["location"] == SSO_OK
    foreign = harness.client()
    started = harness.start(foreign)
    params = fake.authorize(started.headers["location"], subject="entra-sub-alice",
                            claims={"iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0",
                                    "tid": OTHER_TENANT})
    assert _error(harness.callback(foreign, params)) == "OIDC_TOKEN_INVALID"


def multi_tenant_fakes(clock: Any) -> list[FakeIssuer]:
    return [entra_issuer(clock, authority_tenant="organizations"), google_issuer(clock)]


# --- state, PKCE, code replay -------------------------------------------------------------------------------------
def test_pkce_verifier_mismatch_is_refused(oidc: OidcHarness) -> None:
    """A code issued for another authorization request (another challenge) fails PKCE at the token endpoint."""
    victim, attacker = oidc.client(), oidc.client()
    victim_start = oidc.start(victim)
    attacker_start = oidc.start(attacker)
    attacker_code = oidc.entra.authorize(attacker_start.headers["location"], subject="entra-sub-alice")["code"]
    victim_state = oidc.entra.authorize(victim_start.headers["location"], subject="entra-sub-alice")["state"]
    done = oidc.callback(victim, {"code": attacker_code, "state": victim_state})
    assert _error(done) == "OIDC_PROVIDER_ERROR" and _actor_cookie(victim) is None


def test_missing_and_unknown_state_are_refused(oidc: OidcHarness) -> None:
    client = oidc.client()
    started = oidc.start(client)
    params = oidc.entra.authorize(started.headers["location"], subject="entra-sub-alice")
    assert _error(oidc.callback(client, {"code": params["code"]})) == "OIDC_STATE_INVALID"
    assert _error(oidc.callback(client, {**params, "state": "A" * 43})) == "OIDC_STATE_INVALID"
    repeated = client.get(f"/identity/v1/oidc/entra/callback?code={params['code']}&state={params['state']}"
                          f"&state={params['state']}", follow_redirects=False)
    assert _error(repeated) == "OIDC_STATE_INVALID"
    assert oidc.entra.token_requests == []
    assert oidc.callback(client, params).headers["location"] == SSO_OK  # the real state is still usable


def test_state_from_another_browser_is_refused_and_not_consumed(oidc: OidcHarness) -> None:
    starter, other = oidc.client(), oidc.client()
    params = oidc.entra.authorize(oidc.start(starter).headers["location"], subject="entra-sub-alice")
    assert _error(oidc.callback(other, params)) == "OIDC_STATE_INVALID"  # no pre-auth cookie at all
    other.cookies.set("pados_oidc_preauth", "x" * 43)
    assert _error(oidc.callback(other, params)) == "OIDC_STATE_INVALID"  # the wrong pre-auth cookie
    assert _actor_cookie(other) is None and oidc.entra.token_requests == []
    assert oidc.callback(starter, params).headers["location"] == SSO_OK


def test_code_and_state_replay_is_refused(oidc: OidcHarness) -> None:
    client = oidc.client()
    started = oidc.start(client)
    preauth = client.cookies.get("pados_oidc_preauth")
    params = oidc.entra.authorize(started.headers["location"], subject="entra-sub-alice")
    assert oidc.callback(client, params).headers["location"] == SSO_OK
    client.cookies.set("pados_oidc_preauth", str(preauth))  # even with the original pre-auth cookie replayed
    assert _error(oidc.callback(client, params)) == "OIDC_STATE_INVALID"
    assert len(oidc.entra.token_requests) == 1


def test_expired_state_is_refused(oidc: OidcHarness) -> None:
    client = oidc.client()
    params = oidc.entra.authorize(oidc.start(client).headers["location"], subject="entra-sub-alice")
    oidc.clock.advance(601)
    assert _error(oidc.callback(client, params)) == "OIDC_STATE_INVALID"
    assert oidc.entra.token_requests == []


def test_state_of_another_provider_is_refused(oidc: OidcHarness) -> None:
    client = oidc.client()
    params = oidc.entra.authorize(oidc.start(client).headers["location"], subject="entra-sub-alice")
    assert _error(oidc.callback(client, params, "google")) == "OIDC_STATE_INVALID"


def test_provider_error_redirects_with_a_closed_code_only(oidc: OidcHarness) -> None:
    client = oidc.client()
    params = oidc.entra.authorize(oidc.start(client).headers["location"], subject="entra-sub-alice")
    done = oidc.callback(client, {"state": params["state"], "error": "access_denied",
                                  "error_description": "<script>provider text</script>"})
    assert done.headers["location"] == "/?sso_error=OIDC_PROVIDER_ERROR"
    assert "provider text" not in done.text and "script" not in done.headers["location"]


def test_missing_client_credential_environment(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(environ={})
    client = harness.client()
    assert _error(harness.sign_in(client, "entra-sub-alice")) == "OIDC_CLIENT_CREDENTIAL_UNAVAILABLE"
    assert harness.entra.token_requests == []


# --- ID token validation ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("claims", "reason"), [
    ({"nonce": "not-the-nonce"}, "nonce"),
    ({"exp": 1_700_000_000}, "expired"),
    ({"iss": "https://login.microsoftonline.com/" + OTHER_TENANT + "/v2.0"}, "issuer"),
    ({"iss": ENTRA_ISSUER + "/"}, "issuer"),
    ({"aud": "another-client"}, "audience"),
    ({"aud": [ENTRA_CLIENT_ID, "another-client"]}, "authorized party missing"),
    ({"aud": [ENTRA_CLIENT_ID, "another-client"], "azp": "another-client"}, "authorized party"),
    ({"tid": OTHER_TENANT}, "tenant"),
    ({"nbf": 1_900_000_000}, "not yet valid"),
    ({"iat": 1_900_000_000}, "issued in the future"),
    ({"sub": None}, "subject"),
])
def test_invalid_claims_are_refused(oidc: OidcHarness, claims: dict[str, Any], reason: str) -> None:
    client = oidc.client()
    done = oidc.sign_in(client, "entra-sub-alice", claims=claims)
    assert _error(done) == "OIDC_TOKEN_INVALID", reason
    assert _actor_cookie(client) is None


def test_skew_of_up_to_120_seconds_is_tolerated(oidc: OidcHarness) -> None:
    now = int(oidc.clock.now.timestamp())
    ok = oidc.client()
    assert oidc.sign_in(ok, "entra-sub-alice", claims={"exp": now - 119, "iat": now + 119}).headers["location"] \
        == SSO_OK
    late = oidc.client()
    assert _error(oidc.sign_in(late, "entra-sub-alice", claims={"exp": now - 120})) == "OIDC_TOKEN_INVALID"


def _unsigned(alg: str) -> Callable[[FakeIssuer, dict[str, Any], dict[str, Any]], str]:
    def factory(_fake: FakeIssuer, header: dict[str, Any], claims: dict[str, Any]) -> str:
        return compact_jws({**header, "alg": alg}, claims, None)
    return factory


def _hs256_with_public_key(fake: FakeIssuer, header: dict[str, Any], claims: dict[str, Any]) -> str:
    """Key confusion: an HMAC 'signature' keyed with the provider's published public key."""
    signing_input = f"{b64u(json.dumps({**header, 'alg': 'HS256'}).encode())}.{b64u(json.dumps(claims).encode())}"
    mac = hmac.new(fake.signing_key.public_bytes(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64u(mac)}"


def _forged(fake: FakeIssuer, header: dict[str, Any], claims: dict[str, Any]) -> str:
    """Signed by an attacker key while naming the provider's kid."""
    return compact_jws(header, claims, test_rsa_key(9, header["kid"]))


def _tampered(fake: FakeIssuer, header: dict[str, Any], claims: dict[str, Any]) -> str:
    """A genuine signature over other claims (the subject swapped after signing)."""
    genuine = compact_jws(header, claims, fake.signing_key).split(".")
    swapped = b64u(json.dumps({**claims, "sub": "entra-sub-carol"}).encode())
    return f"{genuine[0]}.{swapped}.{genuine[2]}"


@pytest.mark.parametrize("factory", [_unsigned("none"), _unsigned("None"), _unsigned("RS512"), _unsigned("ES256"),
                                     _hs256_with_public_key, _forged, _tampered])
def test_signature_attacks_are_refused(oidc: OidcHarness,
                                       factory: Callable[[FakeIssuer, dict[str, Any], dict[str, Any]], str]) -> None:
    client = oidc.client()
    assert _error(oidc.sign_in(client, "entra-sub-alice", factory=factory)) == "OIDC_TOKEN_INVALID"
    assert _actor_cookie(client) is None


def test_unknown_kid_refetches_the_jwks_once_then_refuses(oidc: OidcHarness) -> None:
    first = oidc.client()
    assert oidc.sign_in(first, "entra-sub-alice").headers["location"] == SSO_OK
    assert oidc.entra.jwks_fetches == 1
    second = oidc.client()
    assert oidc.sign_in(second, "entra-sub-alice").headers["location"] == SSO_OK
    assert oidc.entra.jwks_fetches == 1  # cached

    rogue = test_rsa_key(9, "kid-unknown")

    def unknown(_fake: FakeIssuer, header: dict[str, Any], claims: dict[str, Any]) -> str:
        return compact_jws({**header, "kid": rogue.kid}, claims, rogue)

    third = oidc.client()
    assert _error(oidc.sign_in(third, "entra-sub-alice", factory=unknown)) == "OIDC_TOKEN_INVALID"
    assert oidc.entra.jwks_fetches == 2  # exactly one refetch


def test_key_rotation_is_picked_up_by_the_refetch(oidc: OidcHarness) -> None:
    assert oidc.sign_in(oidc.client(), "entra-sub-alice").headers["location"] == SSO_OK
    rotated = test_rsa_key(3, "kid-rotated")
    oidc.entra.published_keys = [oidc.entra.signing_key, rotated]
    oidc.entra.signing_key = rotated
    assert oidc.sign_in(oidc.client(), "entra-sub-alice").headers["location"] == SSO_OK
    assert oidc.entra.jwks_fetches == 2


def test_jwks_fetch_failure_is_refused(oidc: OidcHarness) -> None:
    oidc.entra.fail_jwks = True
    client = oidc.client()
    assert _error(oidc.sign_in(client, "entra-sub-alice")) == "OIDC_PROVIDER_UNAVAILABLE"
    assert _actor_cookie(client) is None


@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                                     {"Origin": "http://evil.example"}])
def test_a_foreign_page_cannot_start_a_sign_in(oidc: OidcHarness, headers: dict[str, str]) -> None:
    response = oidc.client().get("/identity/v1/oidc/entra/start", headers=headers, follow_redirects=False)
    assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert oidc.entra.discovery_fetches == 0


def test_a_huge_jwk_exponent_is_never_used() -> None:
    from pados.identity.oidc.errors import OidcFailure
    from pados.identity.oidc.jws import rsa_key_from_jwk

    jwk = test_rsa_key(1, "kid-primary").jwk()
    with pytest.raises(OidcFailure):
        rsa_key_from_jwk({**jwk, "e": b64u((2**32 + 1).to_bytes(5, "big"))})


def test_discovery_failure_refuses_the_start(oidc: OidcHarness) -> None:
    oidc.entra.fail_discovery = True
    response = oidc.start(oidc.client())
    assert response.status_code == 503 and response.json()["error"]["code"] == "OIDC_PROVIDER_UNAVAILABLE"


def test_discovery_with_another_issuer_is_refused(oidc: OidcHarness) -> None:
    oidc.entra.discovery_issuer = "https://login.microsoftonline.com/" + OTHER_TENANT + "/v2.0"
    response = oidc.start(oidc.client())
    assert response.status_code == 503 and response.json()["error"]["code"] == "OIDC_PROVIDER_UNAVAILABLE"


def test_discovery_naming_http_endpoints_is_refused(oidc: OidcHarness) -> None:
    oidc.entra.token_endpoint = "http://login.microsoftonline.com/token"
    response = oidc.start(oidc.client())
    assert response.status_code == 503 and response.json()["error"]["code"] == "OIDC_PROVIDER_UNAVAILABLE"


# --- binding --------------------------------------------------------------------------------------------------------
def test_unbound_identity_gets_no_session(oidc: OidcHarness) -> None:
    client = oidc.client()
    assert _error(oidc.sign_in(client, "entra-sub-stranger")) == "SSO_IDENTITY_NOT_BOUND"
    assert _actor_cookie(client) is None
    assert "actor" not in client.get("/identity/v1/session").json()
    assert client.get("/identity/v1/me").json()["error"]["code"] == "SESSION_REQUIRED"


def test_same_email_with_another_subject_is_not_bound(oidc: OidcHarness) -> None:
    """Email is display data: alice's email under another subject (or another issuer) binds nothing."""
    client = oidc.client()
    done = oidc.sign_in(client, "entra-sub-mallory", email="alice@example.com")
    assert _error(done) == "SSO_IDENTITY_NOT_BOUND"
    google = oidc.client()
    assert _error(oidc.sign_in(google, "entra-sub-alice", "google", email="alice@example.com")) \
        == "SSO_IDENTITY_NOT_BOUND"  # alice's subject under another issuer


def test_no_actor_directory_binds_nobody(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(actors_config=None)
    assert _error(harness.sign_in(harness.client(), "entra-sub-alice")) == "SSO_IDENTITY_NOT_BOUND"


def test_a_new_sign_in_revokes_the_presented_actor_session(oidc: OidcHarness) -> None:
    client, _ = oidc.signed_in()
    first = str(_actor_cookie(client))
    assert oidc.sign_in(client, "entra-sub-alice").headers["location"] == SSO_OK
    second = str(_actor_cookie(client))
    assert first != second
    stale = oidc.client()
    stale.cookies.set("pados_actor", first)
    assert stale.get("/identity/v1/me").json()["error"]["code"] == "SESSION_REVOKED"


def test_actor_session_logout_and_lifetimes(oidc: OidcHarness) -> None:
    client, csrf = oidc.signed_in()
    assert client.post("/identity/v1/logout", json={}, headers={"Origin": "http://127.0.0.1:5173"}).status_code == 403
    ended = client.post("/identity/v1/logout", json={}, headers=write_headers(csrf))
    assert ended.status_code == 200 and ended.json() == {"status": "ok", "authenticated": False}
    assert _actor_cookie(client) is None
    idle, _ = oidc.signed_in()
    oidc.clock.advance(7201)
    assert idle.get("/identity/v1/me").json()["error"]["code"] == "SESSION_EXPIRED"


def test_rotating_the_control_token_ends_actor_sessions(oidc: OidcHarness,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    client, _ = oidc.signed_in()
    settings = oidc.harness.app.state.pados_identity.settings
    rotated = settings.model_copy(update={"control_api_token_hash": hashlib.sha256(b"rotated").hexdigest().upper()})
    monkeypatch.setattr(oidc.harness.app.state.pados_identity, "settings", rotated)
    assert client.get("/identity/v1/me").json()["error"]["code"] == "SESSION_REVOKED"
