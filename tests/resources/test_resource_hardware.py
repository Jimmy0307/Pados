"""Hardware profile validation and the read-only compute-node adapter."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError
from resource_fixtures import (
    ALL_NODES,
    MAC_UNIFIED,
    OBSERVED,
    RT,
    WINDOWS_NVIDIA,
    hardware,
    node,
)

from pados.compute_node.manifest import NodeState
from pados.enterprise.compute import Accelerator, NodePlatform
from pados.machines.profile import MachineRole
from pados.resources.hardware import (
    Architecture,
    GpuInfo,
    GpuVendor,
    HardwareCapacity,
    NodeRole,
    OperatingSystem,
    hardware_from_node,
    parse_architecture,
)

NVIDIA = GpuInfo(vendor=GpuVendor.NVIDIA, label="RTX-class")
AMD = GpuInfo(vendor=GpuVendor.AMD, label="Radeon-class")
APPLE = GpuInfo(vendor=GpuVendor.APPLE, label="Apple GPU")


def test_fixture_nodes_are_valid_and_distinct() -> None:
    assert len({item.resource_id for item in ALL_NODES}) == 4
    assert MAC_UNIFIED.unified and not WINDOWS_NVIDIA.unified
    assert WINDOWS_NVIDIA.machine_ref is not None


def test_node_roles_mirror_machine_roles() -> None:
    assert {role.value for role in NodeRole} == {role.value for role in MachineRole}


@pytest.mark.parametrize(("overrides", "code"), [
    (dict(gpu=AMD, vram_mb=8192, runtimes=(RT.CPU, RT.CUDA)), "HARDWARE_RUNTIME_VENDOR_MISMATCH"),
    (dict(gpu=NVIDIA, vram_mb=8192, runtimes=(RT.ROCM,)), "HARDWARE_RUNTIME_VENDOR_MISMATCH"),
    (dict(gpu=NVIDIA, vram_mb=8192, runtimes=(RT.DIRECTML,)), "HARDWARE_RUNTIME_OS_MISMATCH"),
    (dict(gpu=AMD, vram_mb=8192, runtimes=(RT.METAL,)), "HARDWARE_RUNTIME_OS_MISMATCH"),
    (dict(vram_mb=8192), "HARDWARE_VRAM_WITHOUT_GPU"),
    (dict(runtimes=(RT.CUDA,)), "HARDWARE_GPU_RUNTIME_WITHOUT_GPU_MEMORY"),
    (dict(gpu=NVIDIA, runtimes=(RT.CUDA,)), "HARDWARE_GPU_RUNTIME_WITHOUT_GPU_MEMORY"),
    (dict(gpu=AMD, unified_memory_mb=32_768, vram_mb=512), "HARDWARE_UNIFIED_AND_DEDICATED_VRAM_EXCLUSIVE"),
    (dict(unified_memory_mb=16_384), "HARDWARE_UNIFIED_POOL_REPORTED_TWICE"),
    (dict(gpu=APPLE, unified_memory_mb=32_768), "HARDWARE_APPLE_GPU_REQUIRES_MACOS_UNIFIED_MEMORY"),
    (dict(runtimes=(RT.CPU, RT.CPU)), "HARDWARE_RUNTIME_DUPLICATE"),
    (dict(observed_at=datetime(2026, 10, 1, 12, 0)), "HARDWARE_OBSERVED_AT_NOT_AWARE"),
    (dict(schema_version="pados.resources.hardware.v0"), "HARDWARE_SCHEMA_UNSUPPORTED"),
])
def test_inconsistent_hardware_fails_closed(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(ValidationError, match=code):
        hardware(**overrides)


def test_amd_unified_apu_with_vulkan_is_valid() -> None:
    apu = hardware(gpu=AMD, unified_memory_mb=32_768, runtimes=(RT.CPU, RT.VULKAN, RT.ROCM))
    assert apu.unified


@pytest.mark.parametrize("resource_id", ["Upper-case", "ab", "http://host", "node/../x", "a..b", "-x" + "y" * 5,
                                         "x" * 65, "node id"])
def test_resource_id_is_a_closed_identifier(resource_id: str) -> None:
    with pytest.raises(ValidationError):
        hardware(resource_id=resource_id)


@pytest.mark.parametrize("label", ["see https://vendor.example", "/dev/nvidia0", "..\\gpu", "rtx; rm -rf",
                                   "gpu $(id)", "C:\\gpu", "~/gpu", "x" * 81, "", "tab\tname"])
def test_gpu_label_cannot_carry_urls_paths_or_commands(label: str) -> None:
    with pytest.raises(ValidationError):
        GpuInfo(vendor=GpuVendor.NVIDIA, label=label)


@pytest.mark.parametrize("field", ["endpoint", "command", "path", "url", "driver_path"])
def test_profiles_reject_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="extra"):
        hardware(**{field: "anything"})


@pytest.mark.parametrize("machine_ref", ["mach-short", "lcn-" + "0" * 20, "mach-" + "G" * 20])
def test_machine_ref_pattern(machine_ref: str) -> None:
    with pytest.raises(ValidationError):
        hardware(machine_ref=machine_ref)


def test_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        hardware(cpu_cores=0)
    with pytest.raises(ValidationError):
        hardware(ram_mb=-1)
    with pytest.raises(ValidationError):
        hardware(runtimes=())


def test_hardware_from_node_is_read_only_translation() -> None:
    descriptor = node()
    profile = hardware_from_node(descriptor, HardwareCapacity(cpu_cores=16, ram_mb=65_536, gpu=NVIDIA, vram_mb=12_288),
                                 machine_ref="mach-" + "b2" * 10)
    assert profile.resource_id == descriptor.node_id
    assert profile.node_role is NodeRole.LOCAL_COMPUTE_NODE
    assert profile.os is OperatingSystem.WINDOWS and profile.architecture is Architecture.X86_64
    assert profile.runtimes == (RT.CPU, RT.CUDA)
    assert profile.observed_at == OBSERVED and not profile.stale
    assert node() == descriptor  # unchanged


def test_hardware_from_node_maps_apple_silicon_and_extra_runtimes() -> None:
    descriptor = node(platform=NodePlatform.MACOS, architecture="arm64",
                      accelerators=(Accelerator.APPLE_SILICON, Accelerator.CPU))
    profile = hardware_from_node(descriptor, HardwareCapacity(cpu_cores=10, ram_mb=32_768, unified_memory_mb=32_768,
                                                              gpu=APPLE))
    assert profile.runtimes == (RT.CPU, RT.METAL) and profile.unified


@pytest.mark.parametrize("state", [NodeState.OFFLINE, NodeState.UNAUTHORIZED])
def test_offline_or_unauthorized_node_is_stale(state: NodeState) -> None:
    profile = hardware_from_node(node(health_state=state), HardwareCapacity(cpu_cores=4, ram_mb=8192, gpu=NVIDIA,
                                                                             vram_mb=8192))
    assert profile.stale


def test_hardware_from_node_fails_closed_on_inconsistency() -> None:
    with pytest.raises(ValidationError, match="HARDWARE_GPU_RUNTIME_WITHOUT_GPU_MEMORY"):
        hardware_from_node(node(), HardwareCapacity(cpu_cores=4, ram_mb=8192))  # CUDA advertised, no GPU declared
    with pytest.raises(ValueError, match="HARDWARE_ARCHITECTURE_UNKNOWN"):
        parse_architecture("riscv128")
    with pytest.raises(ValueError, match="HARDWARE_OBSERVED_AT_INVALID"):
        hardware_from_node(node(last_seen="yesterday"), HardwareCapacity(cpu_cores=4, ram_mb=8192, gpu=NVIDIA,
                                                                          vram_mb=8192))
    with pytest.raises(ValidationError, match="HARDWARE_OBSERVED_AT_NOT_AWARE"):
        hardware_from_node(node(last_seen="2026-10-01T12:00:00"), HardwareCapacity(cpu_cores=4, ram_mb=8192,
                                                                                    gpu=NVIDIA, vram_mb=8192))


def test_hardware_document_round_trip_and_fail_closed() -> None:
    from pados.lifecycle.minimization import DataMinimizationViolation
    from pados.resources.hardware import HARDWARE_INVENTORY_SCHEMA, hardware_from_document

    document = {"schema_version": HARDWARE_INVENTORY_SCHEMA,
                "resources": [item.model_dump(mode="json") for item in ALL_NODES]}
    assert hardware_from_document(document) == ALL_NODES
    with pytest.raises(ValueError, match="HARDWARE_INVENTORY_SCHEMA_UNSUPPORTED"):
        hardware_from_document({"schema_version": "x", "resources": []})
    with pytest.raises(ValueError, match="HARDWARE_INVENTORY_RESOURCES_INVALID"):
        hardware_from_document({"schema_version": HARDWARE_INVENTORY_SCHEMA, "resources": "x"})
    with pytest.raises(DataMinimizationViolation, match="FIELD_NOT_IN_SCHEMA"):
        hardware_from_document({**document, "probe_command": "x"})
    leaked = dict(ALL_NODES[0].model_dump(mode="json"), **{"api" + "_key": "x"})
    with pytest.raises(DataMinimizationViolation):
        hardware_from_document({"schema_version": HARDWARE_INVENTORY_SCHEMA, "resources": [leaked]})


def test_documented_hardware_example_is_valid() -> None:
    import json
    import re
    from pathlib import Path

    from pados.resources.hardware import hardware_from_document

    doc = Path(__file__).resolve().parents[2] / "docs" / "resources" / "RESOURCE_AWARE_PLANNING_V1.md"
    block = re.search(r"```json\s*(\{\"schema_version\": \"pados.resources.hardware-inventory.v1\".*?)\s*```",
                      doc.read_text(encoding="utf-8"), re.S)
    assert block is not None
    (profile,) = hardware_from_document(json.loads(block.group(1)))
    assert profile.resource_id == "home-workstation" and profile.runtimes == (RT.CPU, RT.CUDA)
