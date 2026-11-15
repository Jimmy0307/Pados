# SPDX-License-Identifier: AGPL-3.0-only
"""Review hardening of the phase 2 relying party: no eviction of pending sign-ins, a start rate limit, a discovery
failure cooldown, and closed codes (never a 500) for malformed provider URLs or deeply nested token headers."""
from __future__ import annotations

import json

import pytest

from tests.identity.oidc.oidc_harness import OidcHarness
from tests.identity.oidc.oidc_support import b64u


def _start_code(response: object) -> str:
    return str(response.json()["error"]["code"])  # type: ignore[attr-defined]


def test_a_flood_of_starts_cannot_cancel_a_sign_in_in_progress(oidc: OidcHarness) -> None:
    """Before: each /start evicted the oldest pending state past 31. Now the table is never evicted, and a burst of
    starts is refused with OIDC_START_RATE_LIMITED while the user's own sign-in still completes."""
    user = oidc.client()
    params = oidc.entra.authorize(oidc.start(user).headers["location"], subject="entra-sub-alice")
    flooder = oidc.client()
    statuses = [oidc.start(flooder).status_code for _ in range(40)]
    assert statuses.count(302) == 9 and statuses.count(429) == 31  # 10 per minute, the user's start included
    refused = oidc.start(flooder)
    assert refused.status_code == 429 and _start_code(refused) == "OIDC_START_RATE_LIMITED"
    done = oidc.callback(user, params)
    assert done.status_code == 302 and done.headers["location"] == "/?sso=ok"


def test_the_start_limit_clears_after_a_minute(oidc: OidcHarness) -> None:
    client = oidc.client()
    for _ in range(10):
        assert oidc.start(client).status_code == 302
    assert oidc.start(client).status_code == 429
    oidc.clock.advance(61)
    assert oidc.start(client).status_code == 302


def test_a_full_pending_table_refuses_new_starts_without_evicting(oidc: OidcHarness) -> None:
    first = oidc.client()
    params = oidc.entra.authorize(oidc.start(first).headers["location"], subject="entra-sub-alice")
    other = oidc.client()
    for _ in range(31):  # 32 pending in total, paced under the per-minute limit
        oidc.clock.advance(7)
        assert oidc.start(other).status_code == 302
    oidc.clock.advance(7)
    full = oidc.start(other)
    assert full.status_code == 429 and _start_code(full) == "OIDC_START_RATE_LIMITED"
    assert oidc.callback(first, params).headers["location"] == "/?sso=ok"  # still valid (inside its 10 minutes)


def test_discovery_failures_are_not_retried_during_the_cooldown(oidc: OidcHarness) -> None:
    oidc.entra.fail_discovery = True
    client = oidc.client()
    assert oidc.start(client).status_code == 503
    assert oidc.entra.discovery_fetches == 1
    assert oidc.start(client).status_code == 503
    assert oidc.entra.discovery_fetches == 1  # no outbound request while cooling down
    oidc.entra.fail_discovery = False
    oidc.clock.advance(31)
    assert oidc.start(client).status_code == 302
    assert oidc.entra.discovery_fetches == 2


def test_a_malformed_provider_url_is_a_closed_code_not_a_server_error(oidc: OidcHarness) -> None:
    oidc.entra.token_endpoint = "https://[bad"
    response = oidc.start(oidc.client())
    assert response.status_code == 503 and _start_code(response) == "OIDC_PROVIDER_UNAVAILABLE"


def test_a_deeply_nested_token_header_fails_closed() -> None:
    from pados.identity.oidc.errors import OidcFailure
    from pados.identity.oidc.jws import parse_compact

    header = "[" * 5000 + "]" * 5000
    compact = f"{b64u(header.encode())}.{b64u(json.dumps({'sub': 'x'}).encode())}.{b64u(b'sig')}"
    with pytest.raises(OidcFailure):
        parse_compact(compact)
