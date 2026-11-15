# SPDX-License-Identifier: AGPL-3.0-only
"""Transport limits and the cross-port localhost cookie risk (design §3.9).

Cookies are scoped by host, not by port: a page served by any other program on ``http://127.0.0.1:<other port>``
is *same-site* with the Control Center, so ``SameSite=Strict`` does not keep the session cookie off its requests.
What does: state-changing requests need an allowlisted ``Origin`` plus the CSRF header (which such a page cannot
read), and no CORS header is ever sent, so a cross-port read is opaque to the page that made it.

Sessions off loopback need the ``__Host-`` Secure cookie, and the control plane's loopback-binding rule still refuses
a non-loopback bind: every combination fails closed.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS, Harness

OTHER_PORT = "http://127.0.0.1:9999"
WORK_REQUEST = {"project_id": "00000000-0000-0000-0000-000000000001", "title": "Cross port",
                "body": "Must never be created.", "idempotency_key": "cross-port-1"}


def _code(response_json: dict[str, Any]) -> str:
    return str(response_json["error"]["code"])


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, no_database: None) -> list[dict[str, Any]]:
    from pados.control import service as control_service

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(control_service, "create_work_request",
                        lambda _s, payload: calls.append(payload) or {"id": "wr-1", **payload})
    monkeypatch.setattr(control_service, "list_projects", lambda _session: [{"id": "p-1"}])
    return calls


def test_a_state_changing_request_from_another_localhost_port_is_refused(harness: Harness,
                                                                         created: list[dict[str, Any]]) -> None:
    client = harness.client()
    csrf = str(harness.paired(client)["csrf_token"])
    # Even with the CSRF value (which a cross-port page cannot read), another port's Origin is refused ...
    foreign = client.post("/control/work-requests", json=WORK_REQUEST,
                          headers={"Origin": OTHER_PORT, "X-PADOS-CSRF": csrf, "Sec-Fetch-Site": "same-site"})
    assert foreign.status_code == 403 and _code(foreign.json()) == "ORIGIN_NOT_ALLOWED"
    # ... and so is a same-site (other port) fetch that claims an allowlisted Origin.
    same_site = client.post("/control/work-requests", json=WORK_REQUEST,
                            headers={"Origin": ORIGIN, "X-PADOS-CSRF": csrf, "Sec-Fetch-Site": "same-site"})
    assert same_site.status_code == 403 and _code(same_site.json()) == "ORIGIN_NOT_ALLOWED"
    # A simple form post carries no CSRF header.
    form = client.post("/control/work-requests", data={"title": "x"}, headers={"Origin": OTHER_PORT})
    assert form.status_code == 403
    assert created == []


def test_cross_port_reads_are_refused_with_an_origin_and_opaque_without_one(harness: Harness,
                                                                            created: list[dict[str, Any]]) -> None:
    client = harness.client()
    harness.paired(client)
    cors_read = client.get("/control/projects", headers={"Origin": OTHER_PORT, "Sec-Fetch-Site": "same-site"})
    assert cors_read.status_code == 403 and _code(cors_read.json()) == "ORIGIN_NOT_ALLOWED"
    # A no-cors read (no Origin) is answered, but with no CORS header the page that sent it cannot read the body.
    # Residual (documented): such a read is side-effect free; its only signal to the other page is timing.
    no_cors = client.get("/control/projects", headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"})
    assert no_cors.status_code == 200
    for response in (cors_read, no_cors):
        assert not any(name.lower().startswith("access-control-") for name in response.headers)


def test_the_session_cookie_is_host_only_strict_and_httponly(harness: Harness) -> None:
    client = harness.client()
    response = harness.pair(client, harness.mint(client))
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "domain=" not in cookie  # host-only; browsers ignore ports for cookies, hence the Origin and CSRF rules


@pytest.mark.parametrize(("secure", "detail_prefix"), [
    ("auto", "IDENTITY_SECURE_COOKIE_REQUIRED"),
    ("never", "IDENTITY_SECURE_COOKIE_REQUIRED"),
    ("always", "control api must bind to localhost"),
])
def test_sessions_off_loopback_fail_closed_in_every_cookie_variant(make_harness: Callable[..., Harness],
                                                                   secure: str, detail_prefix: str) -> None:
    harness = make_harness(PADOS_API_HOST="0.0.0.0", PADOS_IDENTITY_COOKIE_SECURE=secure)  # noqa: S104
    client = harness.client()
    paired = client.post("/identity/v1/pair", json={"pairing_code": "ABCDE-FGHJK"}, headers={"Origin": ORIGIN})
    assert paired.status_code == 403 and str(paired.json()["detail"]).startswith(detail_prefix)
    assert "set-cookie" not in paired.headers
    client.cookies.set("pados_session", "any")
    assert client.get("/control/projects").status_code == 403
    assert client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS).status_code == 403


@pytest.mark.parametrize("secure", ["auto", "never", "always"])
def test_serve_refuses_to_start_sessions_off_loopback(monkeypatch: pytest.MonkeyPatch, secure: str) -> None:
    import uvicorn

    from pados.cli import main as cli
    from pados.config import Settings

    started: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(kwargs))
    settings = Settings(PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(b"serve-off-loopback").hexdigest().upper(),
                        PADOS_IDENTITY_SESSIONS_ENABLED=True, PADOS_API_HOST="0.0.0.0",  # noqa: S104
                        PADOS_IDENTITY_COOKIE_SECURE=secure)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    result = CliRunner().invoke(cli.app, ["serve"])
    assert result.exit_code != 0 and started == []
    expected = "control api must bind to localhost" if secure == "always" else "IDENTITY_SECURE_COOKIE_REQUIRED"
    assert expected in result.output  # the CLI reports the refusal and exits non-zero
