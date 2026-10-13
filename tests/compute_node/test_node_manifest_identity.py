import os

import pytest
from node_support import CREDENTIAL_VALUE, node_config, resolver
from pydantic import ValidationError

from pados.compute_node.config import NodeConfig, is_private_host, validate_bind
from pados.compute_node.identity import identity_proof, load_or_create_identity
from pados.compute_node.manifest import ModelManifest, NodeState
from pados.compute_node.service import NodeRuntime
from pados.enterprise.compute import ComputeNode
from pados.enterprise.manifest import CapabilityHealth


def manifest(**updates):
    values = {"model_id": "ollama/llama3:8b@sha256:0123456789ab", "backend": "ollama", "backend_model": "llama3:8b",
              "family": "llama", "version": "0123456789ab", "context_limit_tokens": 8192,
              "tasks": ["LOCAL_INFERENCE"], "resource_class": "GPU", "privacy_class": "CONFIDENTIAL"}
    values.update(updates)
    return ModelManifest.model_validate(values)


def test_model_manifest_has_canonical_identity_not_alias():
    item = manifest(aliases=("default-chat",), quantization="Q4_K_M", digest="0123456789ab")
    for key in ("model_id", "backend", "family", "version", "quantization", "context_limit_tokens", "tasks",
                "resource_class", "privacy_class", "digest", "availability"):
        assert key in item.model_dump()
    with pytest.raises(ValidationError, match="NOT_CANONICAL"):
        manifest(model_id="default-chat")  # a friendly alias alone is not an identity
    with pytest.raises(ValidationError, match="BACKEND_MISMATCH"):
        manifest(model_id="openai_compatible/llama3:8b@1")
    with pytest.raises(ValidationError, match="ALIAS_CANNOT_BE_IDENTITY"):
        manifest(aliases=("ollama/llama3:8b@sha256:0123456789ab",))


@pytest.mark.parametrize("bad", [{"context_limit_tokens": 0}, {"tasks": []}, {"digest": "not-a-digest"},
                                 {"privacy_class": "SECRET"}, {"tasks": ["TRAINING"]}])
def test_invalid_model_manifest_is_rejected(bad):
    with pytest.raises(ValidationError):
        manifest(**bad)


def test_node_identity_is_stable_and_independent_of_hostname(tmp_path, monkeypatch):
    first = load_or_create_identity(tmp_path, now="t0")
    monkeypatch.setattr("socket.gethostname", lambda: "renamed-host")
    again = load_or_create_identity(tmp_path, now="t1")
    assert first == again and first.node_id.startswith("lcn-")
    assert load_or_create_identity(tmp_path / "other", now="t0").node_id != first.node_id
    if os.name == "posix":  # Windows has no POSIX modes: the state-dir ACL protects the key there
        assert (tmp_path / "node_identity.json").stat().st_mode & 0o077 == 0
    assert identity_proof(CREDENTIAL_VALUE, first.node_id, "n1") != identity_proof(CREDENTIAL_VALUE, first.node_id, "n2")


def test_descriptor_reuses_enterprise_compute_node_contract(tmp_path):
    runtime = NodeRuntime(node_config(tmp_path), resolver=resolver())
    descriptor = runtime.descriptor()
    for key in ("node_id", "node_name", "platform", "architecture", "endpoint", "runtime_version", "health_state",
                "capabilities", "models", "privacy_class", "resource_class", "last_seen", "current_load"):
        assert key in descriptor.model_dump()
    assert isinstance(descriptor.as_compute_node(), ComputeNode)
    assert descriptor.health_state is NodeState.ONLINE and NodeState.OFFLINE.health is CapabilityHealth.UNAVAILABLE
    assert descriptor.models[0].model_id == "deterministic/pados-deterministic-v1@1"
    runtime.jobs.shutdown()


def test_node_refuses_public_or_wildcard_binds(tmp_path):
    for host, allow_lan, ok in [("127.0.0.1", False, True), ("::1", False, True), ("192.168.1.20", False, False),
                                ("192.168.1.20", True, True), ("172.16.5.4", True, True), ("0.0.0.0", True, False),
                                ("8.8.8.8", True, False), ("example.com", True, False)]:
        assert is_private_host(host, allow_lan=allow_lan) is ok, host
    with pytest.raises(PermissionError, match="NOT_PRIVATE"):
        validate_bind(node_config(tmp_path, bind_host="0.0.0.0", allow_lan=True))
    with pytest.raises(ValidationError, match="PRIVATE"):
        node_config(tmp_path, backends=[{"backend_id": "x", "kind": "ollama", "endpoint": "http://8.8.4.4:11434"}])


def test_node_credential_must_be_secret_ref_and_resolvable(tmp_path):
    with pytest.raises(ValidationError, match="SECRET_REFERENCE_REQUIRED"):
        node_config(tmp_path, credential_ref="plain-credential")
    with pytest.raises(PermissionError, match="CREDENTIAL_UNAVAILABLE"):
        NodeRuntime(node_config(tmp_path), resolver=resolver(None))
    with pytest.raises(ValueError, match="NOT_IMPLEMENTED"):
        NodeRuntime(node_config(tmp_path, capabilities=["OCR"]), resolver=resolver())
    assert NodeConfig.model_fields["bind_host"].default == "127.0.0.1"
