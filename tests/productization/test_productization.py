"""Productization V1: public examples, identity/security profiles, public naming, first-run CLI."""
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from pados.compute_node.config import NodeConfig
from pados.compute_node.identity_providers import (
    AsymmetricIdentity,
    HmacSharedSecretIdentity,
    IdentityProfile,
    SecurityProfile,
    assert_profile_allowed,
)
from pados.compute_node.public import PublicBackend, public_backend, public_worker_view
from pados.compute_node.registry import NodeProber, NodeRegistration, NodeRegistry
from pados.enterprise.status import load_registry
from pados.worker.schemas import WorkerType

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "config"
FORBIDDEN = re.compile(r"(/home/|/Users/|C:\\\\Users|C:\\\\PADOS|\b10\.\d+\.\d+\.\d+\b|\b192\.168\.|\b172\.(1[6-9]|2\d|3[01])\.)")


NODE_CONFIGS = sorted([*EXAMPLES.glob("node-*.json"), *(ROOT / "deploy" / "mac-mini").glob("node.*.json")])


@pytest.mark.parametrize("path", NODE_CONFIGS, ids=lambda p: p.name)
def test_example_node_configs_validate(path):
    NodeConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("compute-nodes-*.json")))
def test_example_registries_validate_and_are_not_authorized_by_default(name):
    registry = NodeRegistry.load(EXAMPLES / name)
    assert all(node.authorized is False and node.project_ids == () for node in registry.nodes)


def test_example_enterprise_config_validates():
    registry, _meta = load_registry(str(EXAMPLES / "enterprise-synthetic.json"))
    assert [m.capability_id for m in registry.manifests()] == ["knowledge.local", "ocr.local"]


def test_examples_contain_no_real_paths_addresses_or_credentials():
    for path in [*EXAMPLES.iterdir(), *(ROOT / "examples" / "demo").rglob("*.*"), *(ROOT / "deploy").rglob("*.*")]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not FORBIDDEN.search(text), path.name
            assert "secretref://" in text or "PASSWORD" not in text or "CHANGE_ME" in text, path.name


def test_hmac_is_trusted_lan_only_and_asymmetric_fails_closed(tmp_path):
    hmac_identity = HmacSharedSecretIdentity("c" * 32)
    proof = hmac_identity.prove("lcn-" + "a" * 20, "n")
    assert hmac_identity.verify("lcn-" + "a" * 20, "n", proof) and not hmac_identity.verify("lcn-" + "a" * 20, "m", proof)
    assert_profile_allowed(SecurityProfile.TRUSTED_LAN, IdentityProfile.HMAC_SHARED_SECRET)
    with pytest.raises(PermissionError, match="NOT_ALLOWED_FOR_HARDENED_ENTERPRISE"):
        assert_profile_allowed(SecurityProfile.HARDENED_ENTERPRISE, IdentityProfile.HMAC_SHARED_SECRET)
    assert AsymmetricIdentity().available() is False
    base = {"node_id": "lcn-" + "a" * 20, "endpoint": "http://127.0.0.1:8765", "auth_ref": "secretref://env/X",
            "authorized": True, "allowed_capabilities": ["LOCAL_INFERENCE"], "project_ids": ["*"]}
    with pytest.raises(ValidationError, match="NOT_ALLOWED"):
        NodeRegistration.model_validate({**base, "security_profile": "HARDENED_ENTERPRISE"})
    with pytest.raises(ValidationError, match="REQUIRES_LOOPBACK"):
        NodeRegistration.model_validate({**base, "endpoint": "http://192.0.2.10:8765", "security_profile": "LOCAL_SINGLE_USER"})
    hardened = NodeRegistration.model_validate({**base, "security_profile": "HARDENED_ENTERPRISE",
                                                "identity_profile": "ASYMMETRIC_IDENTITY"})
    evidence = NodeProber(NodeRegistry(state_dir=tmp_path, nodes=(hardened,))).probe(hardened)
    assert (evidence["state"], evidence["reasons"]) == ("UNAUTHORIZED", ["ASYMMETRIC_IDENTITY_NOT_AVAILABLE"])


def test_public_names_never_expose_the_internal_router_class():
    from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec

    worker = WorkerSpec(worker_id="lcn:lcn-" + "a" * 20 + ":ollama/m@1", worker_type=WorkerType.OLLAMA, display_name="x",
                        availability=WorkerAvailability.AVAILABLE, capabilities=[CapabilityId.SUMMARIZE],
                        risk_ceiling=RiskLevel.LOW, supports_structured_output=True, supports_file_changes=False,
                        supports_tool_calls=False, supports_streaming=False, max_context_tokens=1, default_timeout_seconds=1)
    view = public_worker_view(worker)
    assert view == {"executor_kind": "COMPUTE_NODE", "provider_id": "PADOS_LOCAL_INFERENCE", "node_id": "lcn-" + "a" * 20,
                    "model_id": "ollama/m@1", "capability": "LOCAL_INFERENCE"}
    assert "OLLAMA" not in json.dumps(view).replace("ollama/", "")
    assert public_backend("llama_cpp") is PublicBackend.LLAMA_CPP_BACKEND
    assert public_backend("mlx_openai_compatible") is PublicBackend.MLX_BACKEND
    for surface in ("src/pados/compute_node/visibility.py", "src/pados/compute_node/status.py",
                    "src/pados/compute_node/receipts.py", "web/src/compute-nodes-panel.tsx"):
        assert "WorkerType" not in (ROOT / surface).read_text(encoding="utf-8"), surface


def test_version_init_and_security_posture(tmp_path, monkeypatch):
    from pados.cli import main as cli
    from pados.config import get_settings

    version = CliRunner().invoke(cli.app, ["version", "--json"])
    report = json.loads(version.output)
    assert report["product"] == {"version": "0.1.0a1", "channel": "alpha"}
    assert report["protocols"]["compute_node"] == "pados.compute-node.protocol.v1"
    monkeypatch.delenv("PADOS_DB_PASSWORD", raising=False)
    created = CliRunner().invoke(cli.app, ["init", "--dir", str(tmp_path)])
    assert created.exit_code == 0, created.output
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    control_value = (tmp_path / "runtime/local/control.token").read_text(encoding="utf-8").strip()
    assert control_value not in env and control_value not in created.output and "PADOS_DB_PASSWORD=CHANGE_ME" in env
    assert (tmp_path / "runtime/local/control.token").stat().st_mode & 0o077 == 0 or not hasattr(__import__("os"), "getuid")
    from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH, security_posture

    assert DEFAULT_CONTROL_TOKEN_HASH not in env
    assert CliRunner().invoke(cli.app, ["init", "--dir", str(tmp_path)]).exit_code == 1  # never silently overwrite
    get_settings.cache_clear()
    posture = security_posture(get_settings())
    assert posture["readiness_affected"] is False and posture["internet_facing_supported"] is False
    assert any(w.startswith("DEFAULT_CONTROL_TOKEN_HASH_IN_USE") for w in posture["warnings"])
    get_settings.cache_clear()
