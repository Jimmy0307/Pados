# SPDX-License-Identifier: AGPL-3.0-only
"""Gateway contract: schema, closed defaults, secret refusal and node configuration wiring."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pados.compute_node.config import BackendConfig
from pados.compute_node.gateway_contract import (
    ContractError,
    GatewayPolicy,
    fill_template,
    json_path_one,
    json_path_values,
    load_gateway_contract,
    parse_gateway_contract,
)
from pados.enterprise.classification import DataClassification
from tests.compute_node.gateway_support import sim_contract


def _code(document: dict[str, Any]) -> str:
    with pytest.raises(ContractError) as caught:
        parse_gateway_contract(document)
    return str(caught.value)


def test_simulated_contract_parses_with_closed_defaults() -> None:
    contract = parse_gateway_contract(sim_contract("http://127.0.0.1:8999"))
    assert contract.base_url == "http://127.0.0.1:8999"
    assert contract.is_loopback
    default = GatewayPolicy()
    assert default.max_classification is DataClassification.PUBLIC and default.execution_enabled is False


@pytest.mark.parametrize("url", ["http://gateway.example.invalid", "https://user:pw@example.invalid",
                                 "https://gateway.example.invalid/?q=1", "ftp://gateway.example.invalid"])
def test_base_url_requires_https_off_loopback(url: str) -> None:
    assert _code(sim_contract(url)) == "GATEWAY_BASE_URL_MUST_BE_HTTPS"


def test_https_hostname_is_accepted() -> None:
    assert parse_gateway_contract(sim_contract("https://gateway.example.invalid")).is_loopback is False


def test_restricted_is_never_released() -> None:
    document = sim_contract("http://127.0.0.1:1", policy={"max_classification": "RESTRICTED"})
    assert _code(document) == "GATEWAY_RESTRICTED_NEVER_RELEASED"


def test_secret_values_refuse_the_whole_contract() -> None:
    document = sim_contract("http://127.0.0.1:1")
    document["display_name"] = "Bear" + "er " + "q" * 20  # built at run time: no credential-shaped literal
    assert _code(document) == "GATEWAY_CONTRACT_CONTAINS_SECRET"


def test_auth_needs_a_reference_not_a_value() -> None:
    document = sim_contract("http://127.0.0.1:1", auth={"scheme": "bearer", "auth_ref": "PLAIN_VALUE"})
    assert _code(document) == "SECRET_REFERENCE_REQUIRED"
    document = sim_contract("http://127.0.0.1:1", auth={"scheme": "header", "auth_ref": "secretref://env/X",
                                                         "header_name": "Host"})
    assert _code(document) == "GATEWAY_AUTH_HEADER_INVALID"


def test_template_placeholders_are_closed_and_required() -> None:
    document = sim_contract("http://127.0.0.1:1")
    document["chat"]["request_template"]["extra"] = "{{api_key}}"
    assert _code(document) == "GATEWAY_TEMPLATE_PLACEHOLDER_UNKNOWN"
    document = sim_contract("http://127.0.0.1:1")
    document["chat"]["request_template"] = {"model_name": "{{model}}"}
    assert _code(document) == "GATEWAY_TEMPLATE_REQUIRES_MODEL_INPUT_AND_BUDGET"


@pytest.mark.parametrize("path", ["gw/health", "/gw/../x", "/gw?x=1", "https://other.example.invalid/x"])
def test_route_paths_stay_relative(path: str) -> None:
    document = sim_contract("http://127.0.0.1:1")
    document["health"]["route"]["path"] = path
    assert _code(document) == "GATEWAY_ROUTE_PATH_INVALID"


def test_unknown_fields_and_idempotency_without_header_are_refused() -> None:
    assert _code({**sim_contract("http://127.0.0.1:1"), "verify_tls": False}) == "GATEWAY_CONTRACT_INVALID"
    document = sim_contract("http://127.0.0.1:1", idempotency={"honoured": True})
    assert _code(document) == "GATEWAY_IDEMPOTENCY_HEADER_REQUIRED"


def test_json_paths_and_template_fill() -> None:
    document = {"data": [{"id": "a"}, {"id": "b"}], "choices": [{"message": {"content": "hi"}}]}
    assert json_path_values(document, "data[*].id") == ["a", "b"]
    assert json_path_one(document, "choices[0].message.content") == "hi"
    assert json_path_one(document, "data[*].id") is None  # ambiguous
    assert json_path_one(document, "missing.path") is None
    filled = fill_template({"m": "{{model}}", "n": ["{{max_output_tokens}}"], "s": "literal"},
                           {"model": "x", "max_output_tokens": 5})
    assert filled == {"m": "x", "n": [5], "s": "literal"}


def test_load_reads_a_bounded_json_file(tmp_path: Path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(sim_contract("http://127.0.0.1:1")), encoding="utf-8")
    assert load_gateway_contract(path).provider_id == "sim-gateway"
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(ContractError, match="GATEWAY_CONTRACT_UNREADABLE"):
        load_gateway_contract(tmp_path / "bad.json")


def test_node_backend_config_for_the_gateway(tmp_path: Path) -> None:
    contract = str(tmp_path / "gateway.json")
    assert BackendConfig(backend_id="gw", kind="enterprise_gateway", contract_path=contract).contract_path == contract
    with pytest.raises(ValueError, match="GATEWAY_CONTRACT_PATH_MUST_BE_ABSOLUTE"):
        BackendConfig(backend_id="gw", kind="enterprise_gateway", contract_path="relative.json")
    with pytest.raises(ValueError, match="GATEWAY_ENDPOINT_AND_AUTH_BELONG_TO_THE_CONTRACT"):
        BackendConfig(backend_id="gw", kind="enterprise_gateway", contract_path=contract,
                      endpoint="http://127.0.0.1:9")
    with pytest.raises(ValueError, match="CONTRACT_PATH_ONLY_FOR_ENTERPRISE_GATEWAY"):
        BackendConfig(backend_id="o", kind="ollama", endpoint="http://127.0.0.1:9", contract_path=contract)
