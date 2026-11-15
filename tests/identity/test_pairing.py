# SPDX-License-Identifier: AGPL-3.0-only
"""Pairing codes and the pair exchange (design §3.2, §3.4, §3.5)."""
from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from tests.identity.identity_support import CONTROL_VALUE, ORIGIN, TOKEN_HEADERS, Harness

CODE_SHAPE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$")


def _code(response_json: dict[str, object]) -> str:
    return str(response_json["error"]["code"])  # type: ignore[index]


def test_minting_requires_the_control_token_header(harness: Harness) -> None:
    client = harness.client()
    missing = client.post("/identity/v1/pairing-codes")
    assert missing.status_code == 403
    wrong = client.post("/identity/v1/pairing-codes", headers={"X-PADOS-Control-Token": "not-the-value"})
    assert wrong.status_code == 403
    minted = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS)
    assert minted.status_code == 200
    body = minted.json()
    assert CODE_SHAPE.fullmatch(body["pairing_code"]) and set(body) == {"pairing_code", "expires_at"}
    assert minted.headers["cache-control"] == "no-store"


def test_a_session_cookie_cannot_mint_pairing_codes(harness: Harness) -> None:
    client = harness.client()
    paired = harness.paired(client)
    refused = client.post("/identity/v1/pairing-codes",
                          headers={"Origin": ORIGIN, "X-PADOS-CSRF": paired["csrf_token"]})
    assert refused.status_code == 403
    assert "pairing_code" not in refused.text


def test_pair_on_loopback_http_sets_the_plain_httponly_strict_cookie(harness: Harness) -> None:
    client = harness.client()
    response = harness.pair(client, harness.mint(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"device_id", "device_label", "csrf_token", "expires_at", "idle_timeout_seconds"}
    assert re.fullmatch(r"dev_[0-9a-f]{16}", body["device_id"]) and body["device_label"] == "Test browser"
    assert body["idle_timeout_seconds"] == 7200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("pados_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "secure" not in lowered and "domain" not in lowered


def test_pair_over_https_sets_the_host_prefixed_secure_cookie(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(base_url="https://testserver")
    client = harness.client()
    response = harness.pair(client, harness.mint(client))
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-pados_session=")
    lowered = cookie.lower()
    assert "secure" in lowered and "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "domain" not in lowered
    session = client.get("/identity/v1/session")
    assert session.json()["authenticated"] is True


def test_cookie_policy_never_is_refused_over_https(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(base_url="https://testserver", PADOS_IDENTITY_COOKIE_SECURE="never")
    client = harness.client()
    cookie = harness.pair(client, harness.mint(client)).headers["set-cookie"]
    assert cookie.startswith("__Host-pados_session=") and "secure" in cookie.lower()


def test_forwarded_proto_never_selects_the_cookie_variant(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    response = client.post("/identity/v1/pair", json={"pairing_code": code},
                           headers={"Origin": ORIGIN, "X-Forwarded-Proto": "https", "Forwarded": "proto=https"})
    assert response.headers["set-cookie"].startswith("pados_session=")


def test_a_code_works_once(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    assert harness.pair(client, code).status_code == 200
    again = harness.pair(harness.client(), code)
    assert again.status_code == 403 and _code(again.json()) == "PAIRING_CODE_INVALID"


def test_operator_input_is_normalised(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    typed = code.replace("-", " ").lower().replace("1", "l").replace("0", "o")
    assert harness.pair(client, typed).status_code == 200


def test_the_web_client_form_and_other_separators_are_normalised(harness: Harness) -> None:
    """The Control Center sends the code upper-case with its dash; any non-alphanumeric separator is dropped."""
    client = harness.client()
    assert harness.pair(client, harness.mint(client), label="Edge browser").status_code == 200
    code = harness.mint(client)
    assert harness.pair(harness.client(), f" {code[:2]}.{code[2:5].lower()}_{code[6:]} ").status_code == 200


def test_an_expired_code_is_refused_with_expired(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    harness.clock.advance(301)
    response = harness.pair(client, code)
    assert response.status_code == 403 and _code(response.json()) == "PAIRING_CODE_EXPIRED"
    assert "set-cookie" not in response.headers


def test_wrong_and_unknown_codes_are_indistinguishable(harness: Harness) -> None:
    client = harness.client()
    harness.mint(client)
    wrong = harness.pair(client, "ZZZZZ-ZZZZZ")
    malformed = harness.pair(client, "not a code")
    for response in (wrong, malformed):
        assert response.status_code == 403 and _code(response.json()) == "PAIRING_CODE_INVALID"
    assert wrong.json()["detail"] == malformed.json()["detail"] == "PAIRING_CODE_INVALID"


def test_five_failed_attempts_burn_the_code(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    for _ in range(5):
        assert harness.pair(client, "ZZZZZ-ZZZZZ").status_code == 403
    burned = harness.pair(client, code)
    assert burned.status_code == 403 and _code(burned.json()) == "PAIRING_CODE_INVALID"


def test_four_failures_leave_the_code_usable(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    for _ in range(4):
        harness.pair(client, "ZZZZZ-ZZZZZ")
    assert harness.pair(client, code).status_code == 200


def test_the_global_failure_limit_pauses_pairing(harness: Harness) -> None:
    client = harness.client()
    for _ in range(20):
        assert harness.pair(client, "ZZZZZ-ZZZZZ").status_code == 403
    code = harness.mint(client)
    paused = harness.pair(client, code)
    assert paused.status_code == 429 and _code(paused.json()) == "PAIRING_RATE_LIMITED"
    harness.clock.advance(601)
    fresh = harness.mint(client)
    assert harness.pair(client, fresh).status_code == 200


def test_at_most_three_codes_are_outstanding(harness: Harness) -> None:
    client = harness.client()
    codes = []
    for _ in range(4):
        codes.append(harness.mint(client))
        harness.clock.advance(1)
    first, second, third, fourth = codes
    dropped = harness.pair(client, first)
    assert dropped.status_code == 403 and _code(dropped.json()) == "PAIRING_CODE_INVALID"
    for code in (second, third, fourth):
        assert harness.pair(harness.client(), code).status_code == 200


@pytest.mark.parametrize("origin", [None, "http://evil.example", "null", "http://127.0.0.1:5174"])
def test_pair_requires_an_allowlisted_origin(harness: Harness, origin: str | None) -> None:
    client = harness.client()
    code = harness.mint(client)
    response = harness.pair(client, code, origin=origin)
    assert response.status_code == 403 and _code(response.json()) == "ORIGIN_NOT_ALLOWED"
    assert harness.pair(client, code).status_code == 200  # the refused request never consumed the code


def test_pair_refuses_a_cross_site_fetch(harness: Harness) -> None:
    client = harness.client()
    code = harness.mint(client)
    response = client.post("/identity/v1/pair", json={"pairing_code": code},
                           headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403 and _code(response.json()) == "ORIGIN_NOT_ALLOWED"


def test_the_label_is_sanitised_and_the_cli_label_is_the_default(harness: Harness) -> None:
    client = harness.client()
    response = harness.pair(client, harness.mint(client), label="  Office\x00 PC‮\n laptop " + "x" * 80)
    label = response.json()["device_label"]
    assert label.startswith("Office PC laptop") and len(label) <= 64 and "\x00" not in label and "‮" not in label
    hinted = harness.pair(harness.client(), harness.mint(client, label="Front desk"), label=None)
    assert hinted.json()["device_label"] == "Front desk"
    default = harness.pair(harness.client(), harness.mint(client), label="\x00\x01")
    assert default.json()["device_label"] == "Browser"


def test_disabled_identity_refuses_every_route_but_session(make_harness: Callable[..., Harness]) -> None:
    harness = make_harness(enabled=False)
    client = harness.client()
    for method, path in (("POST", "/identity/v1/pairing-codes"), ("POST", "/identity/v1/pair"),
                         ("POST", "/identity/v1/logout"), ("GET", "/identity/v1/devices"),
                         ("POST", "/identity/v1/devices/dev_0000000000000000/revoke")):
        response = client.request(method, path, headers={**TOKEN_HEADERS, "Origin": ORIGIN},
                                  json={"pairing_code": "ZZZZZ-ZZZZZ"} if path.endswith("/pair") else None)
        assert response.status_code == 404 and _code(response.json()) == "IDENTITY_DISABLED", (method, path)
    session = client.get("/identity/v1/session")
    assert session.status_code == 200
    assert session.json() == {"enabled": False, "authenticated": False, "providers": []}
    assert not harness.store_dir.exists()  # nothing was written while disabled


def test_the_default_control_hash_refuses_pairing(make_harness: Callable[..., Harness]) -> None:
    from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH

    harness = make_harness(PADOS_CONTROL_API_TOKEN_HASH=DEFAULT_CONTROL_TOKEN_HASH)
    client = harness.client()
    response = client.post("/identity/v1/pairing-codes", headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    assert response.status_code == 403 and "DEFAULT_CONTROL_TOKEN_REFUSED" in response.text
