"""Synthetic example hardware and model profiles (never the user's real machines or chosen model)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pados.capability.model import (
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Distribution,
    LicenseInfo,
    Modality,
    ResourceRequirement,
    VerificationMethod,
)
from pados.compute_node.manifest import (
    ModelAvailability,
    ModelManifest,
    NodeCapability,
    NodeDescriptor,
    NodeLimits,
    NodeLoad,
    NodeState,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.compute import Accelerator, NodePlatform, ResourceClass
from pados.resources.hardware import (
    AcceleratorRuntime,
    Architecture,
    GpuInfo,
    GpuVendor,
    HardwareProfile,
    NodeRole,
    OperatingSystem,
)
from pados.resources.models import ModelRole, ModelRuntimeProfile

OBSERVED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
AS_OF = OBSERVED + timedelta(minutes=5)
RT = AcceleratorRuntime


def hardware(**overrides: Any) -> HardwareProfile:
    values: dict[str, Any] = dict(resource_id="linux-cpu-node", node_role=NodeRole.LOCAL_COMPUTE_NODE,
                                  os=OperatingSystem.LINUX, architecture=Architecture.X86_64, cpu_cores=8,
                                  ram_mb=32_768, runtimes=(RT.CPU,), observed_at=OBSERVED)
    values.update(overrides)
    return HardwareProfile(**values)


WINDOWS_NVIDIA = hardware(resource_id="win-nvidia-node", os=OperatingSystem.WINDOWS, cpu_cores=16, ram_mb=65_536,
                          gpu=GpuInfo(vendor=GpuVendor.NVIDIA, label="RTX-class 16 GB"), vram_mb=16_384,
                          runtimes=(RT.CPU, RT.CUDA, RT.DIRECTML), machine_ref="mach-" + "a1" * 10)
MAC_UNIFIED = hardware(resource_id="mac-unified-node", node_role=NodeRole.HOME_BASE, os=OperatingSystem.MACOS,
                       architecture=Architecture.ARM64, cpu_cores=12, ram_mb=65_536, unified_memory_mb=65_536,
                       gpu=GpuInfo(vendor=GpuVendor.APPLE, label="Apple M-series GPU"), runtimes=(RT.CPU, RT.METAL))
LINUX_CPU = hardware()
FUTURE_NPU = hardware(resource_id="future-npu-node", architecture=Architecture.ARM64, cpu_cores=8, ram_mb=16_384,
                      runtimes=(RT.CPU, RT.NPU))
ALL_NODES = (WINDOWS_NVIDIA, MAC_UNIFIED, LINUX_CPU, FUTURE_NPU)

OPEN_LICENSE = LicenseInfo(spdx="Apache-2.0", engine_version="1.0.0", distribution=Distribution.RUNTIME_OPTIONAL)


def model(**overrides: Any) -> ModelRuntimeProfile:
    values: dict[str, Any] = dict(model_id="llama-cpp/example-text-7b-q4@v1", backend="llama-cpp",
                                  family="example-text", roles=(ModelRole.TEXT,), modalities=(Modality.TEXT,),
                                  context_length=8192, quantization="Q4_K_M", runtimes=(RT.CPU, RT.CUDA, RT.METAL),
                                  ram_mb=8192, vram_mb=6144, unified_memory_mb=7168, license=OPEN_LICENSE,
                                  max_classification=DataClassification.CONFIDENTIAL, structured_output=True)
    values.update(overrides)
    return ModelRuntimeProfile(**values)


TEXT_7B = model()
TEXT_70B = model(model_id="llama-cpp/example-text-70b-q4@v1", context_length=32_768, ram_mb=49_152,
                 vram_mb=40_960, unified_memory_mb=45_056, tool_use=True)
EMBED_SMALL = model(model_id="onnx/example-embed-small@v1", backend="onnx", family="example-embed",
                    roles=(ModelRole.EMBEDDING,), modalities=(Modality.TEXT, Modality.EMBEDDING), context_length=512,
                    quantization="int8", runtimes=(RT.CPU, RT.NPU), ram_mb=1024, vram_mb=0, unified_memory_mb=0,
                    structured_output=False)
CLOUD_TEXT = model(model_id="remote-api/example-hosted@v1", backend="remote-api", runtimes=(RT.CPU,), ram_mb=1,
                   vram_mb=0, unified_memory_mb=0, network_requirement=NetworkRequirement.WAN_REQUIRED,
                   license=LicenseInfo(spdx="LicenseRef-Proprietary", distribution=Distribution.EXTERNAL_SERVICE),
                   max_classification=DataClassification.INTERNAL)
ALL_MODELS = (TEXT_7B, TEXT_70B, EMBED_SMALL, CLOUD_TEXT)


def capability(**overrides: Any) -> CapabilityDescriptor:
    values: dict[str, Any] = dict(capability_id="MODEL_INFERENCE", version="1", family=CapabilityFamily.MODEL_RUNTIME,
                                  operation="GENERATE_TEXT", input_contract="model.prompt/v1",
                                  output_contract="model.text/v1", determinism=Determinism.PROBABILISTIC,
                                  modalities=(Modality.TEXT,), max_classification=DataClassification.CONFIDENTIAL,
                                  network_requirement=NetworkRequirement.LOCAL_ONLY,
                                  verification=(VerificationMethod.HUMAN_REVIEW,))
    values.update(overrides)
    return CapabilityDescriptor(**values)


INFERENCE = capability()
OCR = capability(capability_id="DOCUMENT_OCR", family=CapabilityFamily.PERCEPTION, operation="RECOGNIZE_TEXT",
                 input_contract="ocr.request/v1", output_contract="ocr.document/v1",
                 determinism=Determinism.DETERMINISTIC, modalities=(Modality.PDF, Modality.IMAGE),
                 resources=ResourceRequirement(cpu_cores=4, ram_mb=4096), verification=(VerificationMethod.SCHEMA,))

LOOPBACK_ENDPOINT = "http://" + ".".join(("127", "0", "0", "1")) + ":8765"
OVERLAY_ENDPOINT = "http://" + ".".join(("10", "20", "30", "40")) + ":8765"


def manifest(**overrides: Any) -> ModelManifest:
    values: dict[str, Any] = dict(model_id="ollama/example-text:7b@v1", backend="ollama",
                                  backend_model="example-text:7b", family="example-text", version="v1",
                                  quantization="Q4_K_M", context_limit_tokens=8192,
                                  tasks=(NodeCapability.LOCAL_INFERENCE, NodeCapability.CLASSIFICATION),
                                  resource_class=ResourceClass.GPU, privacy_class=DataClassification.CONFIDENTIAL,
                                  availability=ModelAvailability.AVAILABLE)
    values.update(overrides)
    return ModelManifest(**values)


def node(**overrides: Any) -> NodeDescriptor:
    values: dict[str, Any] = dict(node_id="lcn-" + "0f" * 10, node_name="example node", platform=NodePlatform.WINDOWS,
                                  architecture="AMD64", endpoint=LOOPBACK_ENDPOINT, health_state=NodeState.ONLINE,
                                  capabilities=(NodeCapability.LOCAL_INFERENCE,), models=(manifest(),),
                                  privacy_class=DataClassification.CONFIDENTIAL, resource_class=ResourceClass.GPU,
                                  accelerators=(Accelerator.CPU, Accelerator.CUDA), last_seen=OBSERVED.isoformat(),
                                  current_load=NodeLoad(running=0, queued=0, max_concurrent_jobs=1, max_queue_depth=4),
                                  limits=NodeLimits())
    values.update(overrides)
    return NodeDescriptor(**values)
