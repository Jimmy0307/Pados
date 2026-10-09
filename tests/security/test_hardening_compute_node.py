"""Security Hardening V1: Local Compute Node (SSRF, identity spoofing, capability bypass,
stale telemetry, credential leakage through node-controlled text)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from pados.compute_node.config import BackendConfig, NodeConfig, is_private_host
from pados.compute_node.identity import identity_proof
from pados.compute_node.jobs import ExecuteRequest, NodeRejection
from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import LocalInferenceProvider, RouteIntent, RoutingDenied
from pados.compute_node.registry import NodeProber, NodeRegistration, NodeRegistry
from pados.compute_node.visibility import compute_node_visibility
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver
from tests.compute_node.node_support import (
    AUTH_REF,
    CREDENTIAL_VALUE,
    ENDPOINT,
    PROJECT,
    Node,
    json_response,
    make_node,
    resolver,
)

NODE_ID = "lcn-" + "a" * 20


@pytest.fixture
def node(tmp_path: Path) -> Any:
    created = make_node(tmp_path)
    yield created
    created.runtime.jobs.shutdown()


# -- arbitrary probe target / SSRF ------------------------------------------------------------

TRANSITION_OR_RESERVED = [
    "2002:808:808::1",  # 6to4: embeds public 8.8.8.8
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo: embeds a public relay/client
    "64:ff9b:1::808:808",  # NAT64 local-use prefix
    "192.0.0.8",  # IETF protocol assignments
    "198.18.0.1",  # benchmarking
    "240.0.0.1", "255.255.255.255", "0.1.2.3",  # reserved / broadcast / "this network"
]


@pytest.mark.parametrize("host", TRANSITION_OR_RESERVED)
def test_private_host_check_is_an_explicit_allowlist(host: str) -> None:
    """FIXED: ``ipaddress.is_private`` admitted transition/reserved ranges (CPython-version dependent)."""
    assert is_private_host(host, allow_lan=True) is False
    with pytest.raises(ValidationError, match="NODE_ENDPOINT_MUST_BE_PRIVATE"):
        NodeRegistration(node_id=NODE_ID, endpoint=f"http://[{host}]:8765" if ":" in host else f"http://{host}:8765",
                         auth_ref=AUTH_REF)


@pytest.mark.parametrize(("host", "allow_lan"), [
    ("127.0.0.1", False), ("::1", False), ("localhost", False), ("10.1.2.3", True), ("172.16.0.9", True),
    ("192.168.1.20", True), ("fd12:3456::1", True), ("fe80::1", True), ("169.254.10.10", True),
    ("::ffff:10.0.0.7", True), ("::ffff:c0a8:114", True), ("::ffff:127.0.0.1", True),
    ("192.0.2.10", True), ("2001:db8::10", True),  # non-routable documentation ranges used by examples
])
def test_documented_private_ranges_remain_accepted(host: str, allow_lan: bool) -> None:
    assert is_private_host(host, allow_lan=allow_lan) is True
    if allow_lan:
        assert is_private_host(host, allow_lan=False) is False  # LAN ranges stay opt-in


@pytest.mark.parametrize("host", [
    "::ffff:127.0.0.1", "::ffff:127.1.2.3", "::ffff:127.255.255.254",
    "::ffff:7f00:1", "0:0:0:0:0:ffff:7f00:102",
])
def test_mapped_loopback_node_endpoint_requires_lan_opt_in(host: str) -> None:
    """The node endpoint allow-list keeps mapped IPv4 opt-in; probe targets deny loopback outright."""
    assert is_private_host(host, allow_lan=False) is False
    assert is_private_host(host, allow_lan=True) is True


@pytest.mark.parametrize("endpoint", [
    "http://user:" + "pw" + "@10.0.0.5:8765", "http://10.0.0.5:8765@8.8.8.8", "ftp://10.0.0.5", "file:///etc/passwd",
    "http://8.8.8.8:8765", "http://0.0.0.0:8765", "http://node.example:8765", "http://[::ffff:8.8.8.8]:80",
    "http://10.0.0.5#@8.8.8.8", "http://10.0.0.5?x=@8.8.8.8",
])
def test_node_registration_refuses_public_credentialed_or_non_http_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        NodeRegistration(node_id=NODE_ID, endpoint=endpoint, auth_ref=AUTH_REF)
    with pytest.raises(ValidationError):
        BackendConfig(backend_id="b", kind="ollama", endpoint=endpoint)


# -- machine identity spoofing by hostname/IP ------------------------------------------------

def _probe(node: Node, tmp_path: Path, rewrite: Any, **registration: Any) -> dict[str, Any]:
    node.rewrite = rewrite
    registry = node.registry(tmp_path, node.registration(**registration))
    return NodeProber(registry, resolver=resolver(), http=node.http).probe(registry.nodes[0])


def test_impostor_at_the_registered_address_cannot_prove_identity(node: Node, tmp_path: Path) -> None:
    def forged_proof(method: str, path: str, response: httpx.Response) -> httpx.Response:
        def mutate(body: dict[str, Any]) -> None:
            nonce = "unknown-to-impostor"
            body["identity_proof"] = identity_proof("guessed-credential", node.node_id, nonce)

        return json_response(response, mutate)

    evidence = _probe(node, tmp_path, forged_proof)
    assert evidence["state"] == "UNAUTHORIZED" and evidence["reasons"] == ["NODE_IDENTITY_PROOF_INVALID"]
    assert evidence["identity_verified"] is False and evidence["descriptor"] is None


def test_same_endpoint_with_another_node_id_is_not_that_node(node: Node, tmp_path: Path) -> None:
    evidence = _probe(node, tmp_path, None, node_id="lcn-" + "b" * 20)
    assert evidence["state"] == "UNAUTHORIZED" and evidence["reasons"] == ["NODE_IDENTITY_MISMATCH"]


def test_proof_without_the_credential_is_never_accepted(node: Node, tmp_path: Path) -> None:
    def strip(method: str, path: str, response: httpx.Response) -> httpx.Response:
        return json_response(response, lambda body: body.pop("identity_proof"))

    assert _probe(node, tmp_path, strip)["reasons"] == ["NODE_IDENTITY_PROOF_INVALID"]


def test_node_service_rejects_requests_without_the_credential(node: Node) -> None:
    for headers in ({}, {"authorization": "Bea" + "rer wrong"}, {"authorization": CREDENTIAL_VALUE},
                    {"authorization": f"Basic {CREDENTIAL_VALUE}"}):
        for method, path in (("GET", "/health"), ("GET", "/capabilities"), ("GET", "/models"),
                             ("POST", "/execute"), ("GET", "/jobs/job-x"), ("GET", f"/artifacts/sha256:{'0' * 64}")):
            assert node.client.request(method, path, headers=headers).status_code == 401, (method, path)


# -- capability bypass ---------------------------------------------------------------------

@pytest.mark.parametrize(("registration", "reason"), [
    ({"authorized": False}, "NODE_NOT_AUTHORIZED"),
    ({"allowed_capabilities": ["EMBEDDING"]}, "NODE_CAPABILITY_NOT_AUTHORIZED"),
    ({"project_ids": ["project-beta"]}, "NODE_PROJECT_SCOPE_DENIED"),
    ({"max_classification": "INTERNAL"}, "NODE_CLASSIFICATION_CEILING"),
])
def test_unauthorized_registration_never_routes(node: Node, tmp_path: Path, registration: dict[str, Any],
                                                reason: str) -> None:
    registry = node.registry(tmp_path, node.registration(**registration))
    provider = LocalInferenceProvider(registry, resolver=resolver(), http=node.http)
    with pytest.raises(RoutingDenied) as denied:
        provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.CONFIDENTIAL, PROJECT))
    assert denied.value.considered[0]["reason"] == reason


def test_unregistered_node_is_unknown_to_pados(tmp_path: Path) -> None:
    registry = NodeRegistry(state_dir=tmp_path)
    with pytest.raises(PermissionError, match="NODE_NOT_REGISTERED"):
        registry.get(NODE_ID)


def test_node_refuses_capabilities_and_classifications_it_does_not_advertise(tmp_path: Path) -> None:
    node = make_node(tmp_path, capabilities=["EMBEDDING"], privacy_class="INTERNAL")
    try:
        model_id = node.runtime.models()[0][0].model_id
        base = {"request_id": "r-1", "work_package_id": "wp", "run_id": "run", "model_id": model_id,
                "inputs": [{"text": "x"}], "output_path": "out.txt"}
        with pytest.raises(NodeRejection, match="CAPABILITY_NOT_ENABLED"):
            node.runtime.jobs.submit(ExecuteRequest.model_validate(
                {**base, "capability": "LOCAL_INFERENCE", "classification": "PUBLIC"}))
        with pytest.raises(NodeRejection, match="CLASSIFICATION_ABOVE_NODE_CEILING"):
            node.runtime.jobs.submit(ExecuteRequest.model_validate(
                {**base, "capability": "EMBEDDING", "classification": "RESTRICTED"}))
    finally:
        node.runtime.jobs.shutdown()


# -- stale telemetry mistaken for live state -------------------------------------------------

def _visibility_with_probe(tmp_path: Path, probed_at: str) -> dict[str, Any]:
    state = tmp_path / "state"
    (state / "probes").mkdir(parents=True)
    (state / "probes" / f"{NODE_ID}.json").write_text(json.dumps(
        {"node_id": NODE_ID, "state": "ONLINE", "probed_at": probed_at, "reasons": [], "descriptor": None}))
    config = tmp_path / "nodes.json"
    config.write_text(json.dumps({"schema_version": "pados.compute-nodes.registry.v1", "state_dir": str(state),
                                  "probe_max_age_seconds": 300,
                                  "nodes": [{"node_id": NODE_ID, "endpoint": ENDPOINT, "auth_ref": AUTH_REF}]}))
    return compute_node_visibility(str(config))["nodes"][0]


def test_old_probe_is_flagged_stale(tmp_path: Path) -> None:
    view = _visibility_with_probe(tmp_path, (datetime.now(UTC) - timedelta(hours=2)).isoformat())
    assert view["stale"] is True and view["reason"] == "PROBE_STALE"


@pytest.mark.parametrize("probed_at", [
    (datetime.now(UTC) + timedelta(days=365)).isoformat(),  # future-dated (clock skew / tampered state)
    datetime.now().replace(tzinfo=None).isoformat(),  # naive timestamp
    "not-a-timestamp",
])
def test_unverifiable_probe_time_is_never_treated_as_fresh(tmp_path: Path, probed_at: str) -> None:
    """FIXED: a future timestamp produced a negative age (fresh forever); a naive one crashed."""
    view = _visibility_with_probe(tmp_path, probed_at)
    assert view["stale"] is True and view["reason"] == "PROBE_STALE"


def test_routing_reprobes_and_ignores_persisted_online_state(node: Node, tmp_path: Path) -> None:
    registry = node.registry(tmp_path)
    probes = registry.state_dir / "probes"
    probes.mkdir(parents=True)
    (probes / f"{node.node_id}.json").write_text(json.dumps(
        {"node_id": node.node_id, "state": "ONLINE", "probed_at": datetime.now(UTC).isoformat()}))

    def offline(endpoint: str) -> Any:
        return httpx.Client(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("down", request=request))), base_url=endpoint)

    provider = LocalInferenceProvider(registry, resolver=resolver(), http=offline)
    with pytest.raises(RoutingDenied, match="NO_ELIGIBLE_LOCAL_NODE"):
        provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))


# -- credential leakage through node-controlled text -----------------------------------------

UPPER_CREDENTIAL = "NODE_CREDENTIAL_4F1C9A7E2B6D8053"


class _ErrorNode:
    def __init__(self, detail: str) -> None:
        self.detail = detail

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(403, json={"detail": self.detail})

    def close(self) -> None:
        pass


@pytest.mark.parametrize("detail", [UPPER_CREDENTIAL, f"DENIED_{UPPER_CREDENTIAL}", "X" * 300])
def test_node_error_detail_cannot_plant_credentials_or_break_probes(tmp_path: Path, detail: str) -> None:
    """FIXED: a node-supplied error detail was persisted verbatim (credential echo) and an
    over-long one crashed the probe through the observability length guard."""
    registry = NodeRegistry(state_dir=tmp_path, nodes=(NodeRegistration(
        node_id=NODE_ID, endpoint=ENDPOINT, auth_ref=AUTH_REF, authorized=True),))
    prober = NodeProber(registry, resolver=EnvironmentSecretResolver(
        {"PADOS_LCN_TEST_CREDENTIAL": UPPER_CREDENTIAL}), http=lambda _e: _ErrorNode(detail))
    evidence = prober.probe(registry.nodes[0])
    assert evidence["state"] == "UNAUTHORIZED" and evidence["reasons"] == ["NODE_ERROR"]
    persisted = (tmp_path / "probes" / f"{NODE_ID}.json").read_text(encoding="utf-8")
    assert UPPER_CREDENTIAL not in persisted


def test_long_node_health_reason_does_not_crash_probe_or_routing(node: Node, tmp_path: Path) -> None:
    def noisy(method: str, path: str, response: httpx.Response) -> httpx.Response:
        def mutate(body: dict[str, Any]) -> None:
            body["descriptor"]["health_reasons"] = ["R" * 400]
            body["descriptor"]["health_state"] = "DEGRADED"

        return json_response(response, mutate) if path == "/capabilities" else response

    evidence = _probe(node, tmp_path, noisy)
    assert evidence["state"] == "DEGRADED"


def test_probe_evidence_and_receipts_carry_refs_not_credentials(node: Node, tmp_path: Path) -> None:
    registry = node.registry(tmp_path)
    provider = LocalInferenceProvider(registry, resolver=resolver(), http=node.http, poll_interval=0.01)
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    execution = provider.execute(route, run_id="run-1", work_package_id="wp-1",
                                 inputs=[{"text": "summarize"}], output_path="out.txt",
                                 allowed_paths=("out.txt",), timeout_seconds=5)
    blobs = [p.read_text(encoding="utf-8") for p in registry.state_dir.rglob("*.json")]
    blobs.append(json.dumps(execution.receipt))
    assert all(CREDENTIAL_VALUE not in blob for blob in blobs)
    assert execution.receipt["credential_ref"] == AUTH_REF and execution.receipt["semantic_authority"] is False


def test_node_config_refuses_plaintext_credentials(tmp_path: Path) -> None:
    base = {"node_name": "n", "state_dir": str(tmp_path), "capabilities": ["LOCAL_INFERENCE"],
            "backends": [{"backend_id": "d", "kind": "deterministic"}],
            "models": [{"backend_id": "d", "backend_model": "m", "family": "f", "version": "1",
                        "context_limit_tokens": 1, "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT",
                        "privacy_class": "INTERNAL"}]}
    with pytest.raises(ValidationError, match="SECRET_REFERENCE_REQUIRED"):
        NodeConfig.model_validate({**base, "credential_ref": "plaintext-node-password"})
    with pytest.raises(ValidationError, match="extra"):
        NodeConfig.model_validate({**base, "credential_ref": AUTH_REF, "password": "x"})
    with pytest.raises(ValidationError, match="extra"):
        NodeRegistration.model_validate({"node_id": NODE_ID, "endpoint": ENDPOINT, "auth_ref": AUTH_REF,
                                         "password": "x"})
    with pytest.raises(ValidationError, match="SECRET_REFERENCE_REQUIRED"):
        BackendConfig(backend_id="b", kind="ollama", endpoint="http://127.0.0.1:11434", credential_ref="sk-" + "plain")
