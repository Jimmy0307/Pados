# SPDX-License-Identifier: AGPL-3.0-only
"""/control/providers over the real app (control_auth) with the simulated gateway: list, owner setup (atomic, no key
values, revision-guarded), enable/disable and diagnostics with the two separate status levels."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pados.compute_node.circuit import reset_circuits
from tests.compute_node.gateway_support import KEY_ENV, running_sim, sim_contract, sim_control, sim_key

OPERATOR = "provider-routes-operator-value"
HEADERS = {"X-PADOS-Control-Token": OPERATOR}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, int, Path, str]]:
    from pados.api import app as api_app
    from pados.config import Settings

    reset_circuits()
    key = sim_key()
    monkeypatch.setenv(KEY_ENV, key)
    setup = tmp_path / "local" / "providers.json"
    settings = Settings(PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(OPERATOR.encode()).hexdigest().upper(),
                        PADOS_PROVIDERS_SETUP=str(setup), OLLAMA_BASE_URL="http://127.0.0.1:9")
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    with running_sim(key) as port:
        contract = tmp_path / "private" / "gateway.json"
        contract.parent.mkdir()
        contract.write_text(json.dumps(sim_contract(f"http://127.0.0.1:{port}")), encoding="utf-8")
        yield TestClient(api_app.create_app()), port, contract, key
    reset_circuits()


def _gateway_body(contract: Path, revision: int, **extra: Any) -> dict[str, Any]:
    return {"expected_revision": revision, "kind": "enterprise_gateway", "display_name": "Sim gateway",
            "contract_path": str(contract), "timeout_seconds": 5, "max_classification": "INTERNAL",
            "project_ids": ["*"], "capability_requirement": "TEXT_SUMMARY", **extra}


def test_reads_need_control_auth(world: Any) -> None:
    client, *_ = world
    assert client.get("/control/providers").status_code in (401, 403)


def test_list_shows_the_detected_runtime_and_the_governed_use_rule(world: Any) -> None:
    client, *_ = world
    body = client.get("/control/providers", headers=HEADERS).json()
    (detected,) = body["providers"]
    assert detected["provider_id"] == "ollama-local" and detected["source"] == "settings"
    assert detected["reachability"]["state"] == "NOT_CHECKED"
    assert body["catalog"]["status"] == "NOT_CONFIGURED" and body["authorizations"]["status"] == "NOT_CONFIGURED"
    assert "not automatically usable" in body["governed_use"]


def test_gateway_setup_diagnostics_and_toggle(world: Any) -> None:
    client, port, contract, key = world
    created = client.put("/control/providers/sim-gw", headers=HEADERS, json=_gateway_body(contract, 0))
    assert created.status_code == 200, created.text
    provider = created.json()["provider"]
    assert provider["enabled"] is False and provider["reachability"]["state"] == "DISABLED"
    assert provider["gateway"]["auth_env"] == KEY_ENV and provider["gateway"]["auth_env_present"] is True
    stale = client.put("/control/providers/sim-gw", headers=HEADERS, json=_gateway_body(contract, 0))
    assert stale.status_code == 409 and "PROVIDERS_SETUP_REVISION_CONFLICT" in stale.text
    disabled = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS, json={})
    assert disabled.json()["provider"]["reachability"]["state"] == "DISABLED"

    enabled = client.post("/control/providers/sim-gw/enable", headers=HEADERS, json={"expected_revision": 1})
    assert enabled.status_code == 200 and enabled.json()["setup_revision"] == 2
    view = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS,
                       json={"probe_chat": True}).json()["provider"]
    assert view["reachability"]["state"] == "REACHABLE"
    models = {m["model"]: m for m in view["models"]}
    assert models["sim-small"]["governance"] == "NOT_MEASURED" and models["sim-large"]["loaded"] is False
    probe = next(c for c in view["checks"] if c["check"] == "probe_chat")
    assert probe["status"] == "PASS" and probe["authority"] == "NON_AUTHORITATIVE_OBSERVATION"
    assert len(probe["answer_sha256"]) == 64 and "SIMULATED" not in json.dumps(view)
    assert view["circuit"]["state"] == "CLOSED" and view["recent_attempts"]

    sim_control(port, "unavailable")
    down = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS, json={}).json()["provider"]
    assert down["reachability"]["state"] == "UNREACHABLE"
    assert down["reachability"]["code"] == "GATEWAY_UNAVAILABLE" and down["reachability"]["explanation"]

    off = client.post("/control/providers/sim-gw/disable", headers=HEADERS, json={"expected_revision": 2})
    assert off.json()["provider"]["enabled"] is False
    assert off.json()["provider"]["reachability"]["state"] == "DISABLED"  # never the earlier REACHABLE observation
    document = (contract.parent.parent / "local" / "providers.json").read_text(encoding="utf-8")
    everything = document + created.text + view.__repr__() + down.__repr__()
    assert key not in everything  # no key value is ever stored or returned
    assert json.loads(document)["revision"] == 3


def test_setup_refuses_key_values_and_invalid_endpoints(world: Any) -> None:
    client, _, contract, _ = world
    leaked = _gateway_body(contract, 0, display_name="sk-" + "z" * 24)
    refused = client.put("/control/providers/sim-gw", headers=HEADERS, json=leaked)
    assert refused.status_code == 422 and "PROVIDER_SETTINGS_CONTAIN_KEY_VALUE" in refused.text
    public = {"expected_revision": 0, "kind": "openai_compatible", "endpoint": "https://api.example.invalid"}
    assert "PROVIDER_ENDPOINT_MUST_BE_PRIVATE" in client.put("/control/providers/x", headers=HEADERS, json=public).text
    unknown = client.post("/control/providers/nope/diagnostics", headers=HEADERS, json={})
    assert unknown.status_code == 404 and "PROVIDER_NOT_CONFIGURED" in unknown.text


def test_detected_provider_becomes_a_setup_entry_when_toggled(world: Any) -> None:
    client, *_ = world
    response = client.post("/control/providers/ollama-local/disable", headers=HEADERS, json={"expected_revision": 0})
    assert response.status_code == 200
    provider = response.json()["provider"]
    assert provider["source"] == "setup" and provider["enabled"] is False


def test_chat_probes_are_rate_limited_with_a_closed_code(world: Any) -> None:
    """MEDIUM-2: a probe spends inference on the gateway; past the probe budget the route answers 429 with a closed code
    and makes no request, while a plain connection check still has budget."""
    from pados.providers.diagnostics import PROBE_BURST, RATE_LIMITED

    client, _port, contract, _key = world
    assert client.put("/control/providers/sim-gw", headers=HEADERS, json=_gateway_body(contract, 0)).status_code == 200
    assert client.post("/control/providers/sim-gw/enable", headers=HEADERS, json={"expected_revision": 1}).status_code == 200
    for _ in range(PROBE_BURST):
        ok = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS, json={"probe_chat": True})
        assert ok.status_code == 200, ok.text
    attempts = len(ok.json()["provider"]["recent_attempts"])
    refused = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS, json={"probe_chat": True})
    assert refused.status_code == 429 and RATE_LIMITED in refused.text
    still = client.post("/control/providers/sim-gw/diagnostics", headers=HEADERS, json={})
    assert still.status_code == 200
    assert len(still.json()["provider"]["recent_attempts"]) >= attempts  # the refused probe added no attempt
