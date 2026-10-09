import pytest
from pydantic import ValidationError

from pados.enterprise.classification import DataClassification
from pados.enterprise.compute import (
    Accelerator,
    ComputeNode,
    Locality,
    NodePlatform,
    ProviderCapabilityProfile,
    ResourceClass,
    profile_for_worker,
)
from pados.enterprise.manifest import CapabilityHealth
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType


def worker(worker_type):
    return WorkerSpec(worker_id=f"w-{worker_type.value.lower()}", worker_type=worker_type, display_name="w",
                      version="1", availability=WorkerAvailability.AVAILABLE,
                      capabilities=[CapabilityId.SUMMARIZE], risk_ceiling=RiskLevel.MEDIUM,
                      supports_structured_output=True, supports_file_changes=False, supports_tool_calls=False,
                      supports_streaming=False, max_context_tokens=8000, default_timeout_seconds=30)


def test_provider_profile_declares_locality_accelerator_privacy_limits_and_tasks():
    profile = ProviderCapabilityProfile(
        provider_id="local-llm", locality=Locality.LOCAL, accelerators=(Accelerator.CUDA, Accelerator.CPU),
        privacy_class=DataClassification.RESTRICTED, context_limit_tokens=32768, model_family="llama",
        supported_tasks=("SUMMARIZE",))
    assert profile.permits(DataClassification.RESTRICTED)
    assert {a.value for a in Accelerator} == {"CPU", "CUDA", "APPLE_SILICON"}


def test_existing_worker_router_workers_are_described_not_routed():
    remote = profile_for_worker(worker(WorkerType.CODEX_CLI))
    local = profile_for_worker(worker(WorkerType.OLLAMA))
    assert remote.locality is Locality.REMOTE and not remote.permits(DataClassification.CONFIDENTIAL)
    assert local.locality is Locality.LOCAL and local.permits(DataClassification.RESTRICTED)
    assert remote.cost_class.value == "UNKNOWN"  # unknown facts are not invented


def test_compute_node_model_is_minimal_and_rejects_embedded_credentials():
    node = ComputeNode(node_id="node-a", endpoint="http://127.0.0.1:11434", platform=NodePlatform.LINUX,
                       capabilities=("LOCAL_INFERENCE",), health=CapabilityHealth.READY,
                       resource_class=ResourceClass.GPU, privacy_class=DataClassification.CONFIDENTIAL,
                       models=("synthetic-model",))
    assert set(ComputeNode.model_fields) == {"node_id", "endpoint", "platform", "capabilities", "health",
                                             "resource_class", "privacy_class", "models"}
    assert node.is_loopback and node.eligible_for(DataClassification.INTERNAL, "LOCAL_INFERENCE")
    assert not node.eligible_for(DataClassification.RESTRICTED, "LOCAL_INFERENCE")
    down = node.model_copy(update={"health": CapabilityHealth.UNAVAILABLE})
    assert not down.eligible_for(DataClassification.PUBLIC, "LOCAL_INFERENCE")
    with pytest.raises(ValidationError, match="CREDENTIALS"):
        ComputeNode(node_id="node-b", endpoint="http://user" + ":pw" + "@" + "node.example.invalid:8000", platform=NodePlatform.LINUX,
                    resource_class=ResourceClass.CPU_LIGHT, privacy_class=DataClassification.PUBLIC)


def test_no_scheduler_or_router_is_introduced():
    from pados.enterprise import compute

    names = {name.lower() for name in dir(compute)}
    assert not any(word in name for name in names for word in ("schedule", "dispatch", "route"))
