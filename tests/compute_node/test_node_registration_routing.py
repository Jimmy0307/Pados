"""reachable != registered != authorized != authorized capability; privacy-aware routing."""
import httpx
import pytest
from node_support import PROJECT, make_node, resolver

from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import LocalInferenceProvider, RouteIntent, RoutingDenied
from pados.compute_node.registry import NodeProber, NodeRegistration, NodeRegistry
from pados.enterprise.classification import DataClassification


def _provider(node, tmp_path, *registrations, resolvable=True):
    registry = node.registry(tmp_path, *registrations)
    return LocalInferenceProvider(registry, resolver=resolver() if resolvable else resolver(None), http=node.http)


def _intent(classification=DataClassification.INTERNAL, capability=NodeCapability.LOCAL_INFERENCE, project=PROJECT):
    return RouteIntent(capability, classification, project)


def test_authorized_registered_probed_node_is_routed_through_worker_router(node, tmp_path, monkeypatch, events):
    import pados.compute_node.provider as provider_module

    calls = []
    real = provider_module.select_worker
    monkeypatch.setattr(provider_module, "select_worker", lambda *a, **k: calls.append(k) or real(*a, **k))
    route = _provider(node, tmp_path).route(_intent())
    assert route.descriptor.node_id == node.node_id and route.model.model_id.startswith("deterministic/")
    assert calls and calls[0]["allowed_worker_types"] == frozenset({provider_module.LOCAL_MODEL_WORKER})
    assert route.profile.locality.value == "LOCAL"
    assert any(e["event"] == "pados.enterprise.node.selected" for e in events)


def test_reachable_node_is_not_registered(node, tmp_path):
    registry = NodeRegistry(state_dir=tmp_path / "s", nodes=())
    with pytest.raises(RoutingDenied, match="NO_ELIGIBLE_LOCAL_NODE"):
        LocalInferenceProvider(registry, resolver=resolver(), http=node.http).route(_intent())
    with pytest.raises(PermissionError, match="NOT_REGISTERED"):
        registry.get(node.node_id)


def test_registered_node_is_not_authorized(node, tmp_path):
    registration = node.registration(authorized=False)
    with pytest.raises(RoutingDenied) as info:
        _provider(node, tmp_path, registration).route(_intent())
    assert info.value.considered[0]["reason"] == "NODE_NOT_AUTHORIZED"
    evidence = NodeProber(node.registry(tmp_path, registration), resolver=resolver(), http=node.http).probe(registration)
    assert evidence["reachable"] and evidence["identity_verified"] and evidence["state"] == "UNAUTHORIZED"


def test_authorized_node_is_not_an_authorized_capability(node, tmp_path):
    registration = node.registration(allowed_capabilities=["EMBEDDING"])
    with pytest.raises(RoutingDenied) as info:
        _provider(node, tmp_path, registration).route(_intent())
    assert info.value.considered[0]["reason"] == "NODE_CAPABILITY_NOT_AUTHORIZED"
    assert _provider(node, tmp_path, registration).route(_intent(capability=NodeCapability.EMBEDDING))


def test_project_scope_is_enforced(node, tmp_path):
    with pytest.raises(RoutingDenied) as info:
        _provider(node, tmp_path).route(_intent(project="project-other"))
    assert info.value.considered[0]["reason"] == "NODE_PROJECT_SCOPE_DENIED"
    assert _provider(node, tmp_path, node.registration(project_ids=["*"])).route(_intent(project="any"))
    with pytest.raises(RoutingDenied):
        _provider(node, tmp_path, node.registration(project_ids=[])).route(_intent())  # empty scope = none


def test_classification_policy_blocks_invalid_routing(node, tmp_path, events):
    with pytest.raises(RoutingDenied) as info:
        _provider(node, tmp_path).route(_intent(DataClassification.RESTRICTED))  # ceiling CONFIDENTIAL
    assert info.value.considered[0]["reason"] == "NODE_CLASSIFICATION_CEILING"
    restricted = node.registration(max_classification="RESTRICTED")
    assert _provider(node, tmp_path, restricted).route(_intent(DataClassification.RESTRICTED))
    internal_node = make_node(tmp_path / "n2", privacy_class="INTERNAL")
    with pytest.raises(RoutingDenied) as info:
        _provider(internal_node, tmp_path, internal_node.registration(max_classification="RESTRICTED")).route(
            _intent(DataClassification.CONFIDENTIAL))
    assert info.value.considered[0]["reason"] == "NODE_CAPABILITY_OR_CLASSIFICATION_INELIGIBLE"
    internal_node.runtime.jobs.shutdown()
    assert any(e.get("pados.decision") == "DENY" for e in events)


def test_offline_node_fails_cleanly(node, tmp_path):
    registration = node.registration(endpoint="http://127.0.0.1:9")
    provider = LocalInferenceProvider(node.registry(tmp_path, registration), resolver=resolver(),
                                      http=lambda endpoint: httpx.Client(base_url=endpoint))
    with pytest.raises(RoutingDenied) as info:
        provider.route(_intent())
    assert info.value.considered[0]["state"] == "OFFLINE"


def test_identity_mismatch_wrong_credential_and_invalid_secret_ref_are_unauthorized(node, tmp_path):
    impostor = node.registration(node_id="lcn-" + "0" * 20)
    evidence = NodeProber(node.registry(tmp_path, impostor), resolver=resolver(), http=node.http).probe(impostor)
    assert (evidence["state"], evidence["reasons"]) == ("UNAUTHORIZED", ["NODE_IDENTITY_MISMATCH"])
    wrong = NodeProber(node.registry(tmp_path), resolver=resolver("wrong-credential-value"), http=node.http)
    evidence = wrong.probe(node.registration())
    assert evidence["state"] == "UNAUTHORIZED" and evidence["reasons"] == ["NODE_AUTHENTICATION_FAILED"]
    missing = NodeProber(node.registry(tmp_path), resolver=resolver(None), http=node.http).probe(node.registration())
    assert missing["state"] == "UNAUTHORIZED" and missing["reasons"] == ["SECRET_REFERENCE_UNAVAILABLE"]
    with pytest.raises(ValueError, match="SECRET_REFERENCE_REQUIRED"):
        NodeRegistration.model_validate({**node.registration().model_dump(mode="json"), "auth_ref": "plain"})
    with pytest.raises(ValueError, match="PRIVATE"):
        NodeRegistration.model_validate({**node.registration().model_dump(mode="json"),
                                         "endpoint": "http://u" + ":p" + "@127.0.0.1:1"})


def test_invalid_manifest_from_node_is_not_routable(node, tmp_path):
    from node_support import json_response

    def corrupt(method, path, response):
        if path == "/capabilities":
            return json_response(response, lambda body: body["descriptor"]["models"][0].update(context_limit_tokens=0))
        return response

    node.rewrite = corrupt
    evidence = NodeProber(node.registry(tmp_path), resolver=resolver(), http=node.http).probe(node.registration())
    assert evidence["state"] == "DEGRADED" and evidence["reasons"] == ["NODE_MANIFEST_INVALID"]
    with pytest.raises(RoutingDenied):
        _provider(node, tmp_path).route(_intent())


def test_busy_and_degraded_nodes_are_not_selected(tmp_path):
    degraded = make_node(tmp_path, models=[
        {"backend_id": "deterministic", "backend_model": "absent", "family": "f", "version": "1",
         "context_limit_tokens": 10, "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT",
         "privacy_class": "INTERNAL"}], capabilities=["LOCAL_INFERENCE"])
    with pytest.raises(RoutingDenied) as info:
        _provider(degraded, tmp_path).route(_intent())
    assert info.value.considered[0]["state"] == "DEGRADED"
    degraded.runtime.jobs.shutdown()


def test_model_missing_for_requested_model(node, tmp_path):
    with pytest.raises(RoutingDenied) as info:
        _provider(node, tmp_path).route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL,
                                                    PROJECT, model_id="deterministic/other@1"))
    assert info.value.considered[0]["reason"] == "NO_AVAILABLE_MODEL"
