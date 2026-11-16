# SPDX-License-Identifier: AGPL-3.0-only
"""No raw control token, pairing code, session id or CSRF token in logs, responses or the store (design §2.5)."""
from __future__ import annotations

from typing import Any

import pytest
import structlog

from tests.identity.identity_support import CONTROL_VALUE, ORIGIN, TOKEN_HEADERS, Harness


def _flow(harness: Harness) -> dict[str, Any]:
    client = harness.client()
    responses = []
    minted = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS, json={"label": "Desk"})
    code = minted.json()["pairing_code"]
    responses.append(harness.pair(client, "ZZZZZ-ZZZZZ"))
    paired = harness.pair(client, code.lower())
    responses.append(paired)
    session_id = str(client.cookies.get("pados_session"))
    csrf = paired.json()["csrf_token"]
    responses.append(client.get("/identity/v1/session"))
    responses.append(client.get("/identity/v1/devices"))
    responses.append(client.get("/identity/v1/devices", headers=TOKEN_HEADERS))
    responses.append(client.get("/control/projects"))
    responses.append(client.post("/control/work-requests", json={}, headers={"Origin": ORIGIN}))
    responses.append(client.post("/identity/v1/logout", headers={"Origin": ORIGIN, "X-PADOS-CSRF": csrf}))
    responses.append(client.get("/control/projects"))  # revoked now
    return {"minted": minted, "responses": responses, "code": code, "session_id": session_id, "csrf": csrf}


def test_secrets_never_reach_logs_or_other_responses(harness: Harness, capsys: pytest.CaptureFixture[str],
                                                     caplog: pytest.LogCaptureFixture) -> None:
    with structlog.testing.capture_logs() as events:
        flow = _flow(harness)
    captured = capsys.readouterr()
    logs = repr(events) + captured.out + captured.err + caplog.text
    event_names = {event.get("event") for event in events}
    assert {"identity.pair.succeeded", "identity.pair.refused", "http.request.finished"} <= event_names

    code = flow["code"]
    secrets_ = {"control token": CONTROL_VALUE, "pairing code": code, "pairing code (compact)": code.replace("-", ""),
                "session id": flow["session_id"], "csrf token": flow["csrf"]}
    for name, value in secrets_.items():
        assert value not in logs, f"{name} reached a log"

    bodies = [r.text for r in flow["responses"]]
    headers = [repr(dict(r.headers)) for r in flow["responses"]]
    for value in (CONTROL_VALUE, code, code.replace("-", ""), flow["session_id"]):
        assert not any(value in body for body in bodies)
    # The session id travels only in the Set-Cookie header of /pair; the control token in no header at all.
    assert sum(flow["session_id"] in h for h in headers) == 1
    assert not any(CONTROL_VALUE in h for h in headers)
    assert code in flow["minted"].text  # the one response that carries the code


def test_the_store_holds_only_hashes(harness: Harness) -> None:
    flow = _flow(harness)
    stored = "".join(path.read_bytes().decode("utf-8", "replace") for path in harness.store_dir.glob("*.json"))
    assert stored
    for value in (CONTROL_VALUE, flow["code"], flow["code"].replace("-", ""), flow["session_id"], flow["csrf"]):
        assert value not in stored
