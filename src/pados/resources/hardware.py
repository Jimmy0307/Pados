# SPDX-License-Identifier: AGPL-3.0-only
"""Hardware profiles: what one machine offers (descriptive telemetry, never authority).

A profile is declared by the operator or derived read-only from an existing compute-node descriptor; this
module never probes the host, the GPU or the network. Identifiers and accelerator/runtime names are
closed vocabularies and the only free text (the GPU label) is bounded display text, so a profile cannot
carry URLs, paths or commands. Internal consistency is enforced (e.g. CUDA needs an NVIDIA GPU, METAL a
macOS GPU, unified memory is one pool reported once) and contradictions fail closed.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.model import checked_display_text
from pados.compute_node.manifest import NodeDescriptor, NodeState
from pados.enterprise.compute import Accelerator, NodePlatform
from pados.lifecycle.minimization import assert_minimized

HARDWARE_SCHEMA = "pados.resources.hardware.v1"
HARDWARE_INVENTORY_SCHEMA = "pados.resources.hardware-inventory.v1"
MAX_DOCUMENT_RESOURCES = 256
RESOURCE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{2,63}$"
MACHINE_REF_PATTERN = r"^mach-[0-9a-f]{20}$"  # pados.machines.identity.MACHINE_ID_PATTERN
_RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)


class NodeRole(StrEnum):
    """Mirrors ``pados.machines.profile.MachineRole`` (a role is a description, not a permission)."""

    HOME_BASE = "HOME_BASE"
    LOCAL_COMPUTE_NODE = "LOCAL_COMPUTE_NODE"
    HERDR_MACHINE = "HERDR_MACHINE"
    SANDBOX_HOST = "SANDBOX_HOST"


class OperatingSystem(StrEnum):
    LINUX = "LINUX"
    WINDOWS = "WINDOWS"
    MACOS = "MACOS"


class Architecture(StrEnum):
    X86_64 = "X86_64"
    ARM64 = "ARM64"


class AcceleratorRuntime(StrEnum):
    """Closed accelerator/runtime tokens (declaration order is the deterministic listing order)."""

    CPU = "CPU"
    CUDA = "CUDA"
    ROCM = "ROCM"
    METAL = "METAL"
    DIRECTML = "DIRECTML"
    VULKAN = "VULKAN"
    ONEAPI = "ONEAPI"
    NPU = "NPU"


# GPU runtimes that use device memory (dedicated VRAM, or the shared pool on a unified-memory machine).
GPU_RUNTIMES = frozenset({AcceleratorRuntime.CUDA, AcceleratorRuntime.ROCM, AcceleratorRuntime.METAL,
                          AcceleratorRuntime.DIRECTML, AcceleratorRuntime.VULKAN, AcceleratorRuntime.ONEAPI})
RUNTIME_ORDER: dict[AcceleratorRuntime, int] = {item: index for index, item in enumerate(AcceleratorRuntime)}


class GpuVendor(StrEnum):
    NVIDIA = "NVIDIA"
    AMD = "AMD"
    APPLE = "APPLE"
    INTEL = "INTEL"
    OTHER = "OTHER"


_RUNTIME_VENDORS: dict[AcceleratorRuntime, frozenset[GpuVendor]] = {
    AcceleratorRuntime.CUDA: frozenset({GpuVendor.NVIDIA}),
    AcceleratorRuntime.ROCM: frozenset({GpuVendor.AMD}),
    AcceleratorRuntime.ONEAPI: frozenset({GpuVendor.INTEL}),
}
_RUNTIME_OS: dict[AcceleratorRuntime, OperatingSystem] = {
    AcceleratorRuntime.METAL: OperatingSystem.MACOS,
    AcceleratorRuntime.DIRECTML: OperatingSystem.WINDOWS,
}


class GpuInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor: GpuVendor
    label: str = Field(min_length=1, max_length=80)  # display only, e.g. "RTX-class 16 GB"; never parsed

    @field_validator("label")
    @classmethod
    def display_label(cls, value: str) -> str:
        return checked_display_text(value)


def aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(code)
    return value


class HardwareProfile(BaseModel):
    """One machine's resources. ``stale`` marks telemetry the reporter itself distrusts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = HARDWARE_SCHEMA
    resource_id: str
    node_role: NodeRole
    os: OperatingSystem
    architecture: Architecture
    cpu_cores: int = Field(ge=1, le=512)
    ram_mb: int = Field(ge=0, le=4_194_304)
    gpu: GpuInfo | None = None
    vram_mb: int = Field(default=0, ge=0, le=1_048_576)  # dedicated device memory
    unified_memory_mb: int = Field(default=0, ge=0, le=4_194_304)  # one CPU/GPU pool (e.g. Apple silicon)
    runtimes: tuple[AcceleratorRuntime, ...] = Field(min_length=1, max_length=len(AcceleratorRuntime))
    machine_ref: str | None = Field(default=None, pattern=MACHINE_REF_PATTERN)
    observed_at: datetime
    stale: bool = False

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != HARDWARE_SCHEMA:
            raise ValueError("HARDWARE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("resource_id")
    @classmethod
    def resource_id_pattern(cls, value: str) -> str:
        if not _RESOURCE_ID.fullmatch(value) or ".." in value:
            raise ValueError("RESOURCE_ID_INVALID")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_at_aware(cls, value: datetime) -> datetime:
        return aware(value, "HARDWARE_OBSERVED_AT_NOT_AWARE")

    @model_validator(mode="after")
    def consistent(self) -> HardwareProfile:
        if len(set(self.runtimes)) != len(self.runtimes):
            raise ValueError("HARDWARE_RUNTIME_DUPLICATE")
        if self.unified_memory_mb and self.vram_mb:
            raise ValueError("HARDWARE_UNIFIED_AND_DEDICATED_VRAM_EXCLUSIVE")
        if self.unified_memory_mb and self.ram_mb != self.unified_memory_mb:
            raise ValueError("HARDWARE_UNIFIED_POOL_REPORTED_TWICE")  # ram_mb must equal the one pool
        if self.vram_mb and self.gpu is None:
            raise ValueError("HARDWARE_VRAM_WITHOUT_GPU")
        if self.gpu is not None and self.gpu.vendor is GpuVendor.APPLE and not (
                self.os is OperatingSystem.MACOS and self.unified_memory_mb):
            raise ValueError("HARDWARE_APPLE_GPU_REQUIRES_MACOS_UNIFIED_MEMORY")
        for runtime in self.runtimes:
            if runtime in GPU_RUNTIMES:
                if self.gpu is None or not (self.vram_mb or self.unified_memory_mb):
                    raise ValueError("HARDWARE_GPU_RUNTIME_WITHOUT_GPU_MEMORY")
                vendors = _RUNTIME_VENDORS.get(runtime)
                if vendors is not None and self.gpu.vendor not in vendors:
                    raise ValueError("HARDWARE_RUNTIME_VENDOR_MISMATCH")
            required_os = _RUNTIME_OS.get(runtime)
            if required_os is not None and self.os is not required_os:
                raise ValueError("HARDWARE_RUNTIME_OS_MISMATCH")
        return self

    @property
    def unified(self) -> bool:
        return self.unified_memory_mb > 0


class HardwareCapacity(BaseModel):
    """Operator-declared capacities a compute-node descriptor does not advertise."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_cores: int = Field(ge=1, le=512)
    ram_mb: int = Field(ge=0, le=4_194_304)
    gpu: GpuInfo | None = None
    vram_mb: int = Field(default=0, ge=0, le=1_048_576)
    unified_memory_mb: int = Field(default=0, ge=0, le=4_194_304)
    extra_runtimes: tuple[AcceleratorRuntime, ...] = Field(default=(), max_length=len(AcceleratorRuntime))


_OS_FROM_PLATFORM = {NodePlatform.LINUX: OperatingSystem.LINUX, NodePlatform.WINDOWS: OperatingSystem.WINDOWS,
                     NodePlatform.MACOS: OperatingSystem.MACOS}
_RUNTIME_FROM_ACCELERATOR = {Accelerator.CPU: AcceleratorRuntime.CPU, Accelerator.CUDA: AcceleratorRuntime.CUDA,
                             Accelerator.APPLE_SILICON: AcceleratorRuntime.METAL}
_ARCHITECTURES = {"x86_64": Architecture.X86_64, "amd64": Architecture.X86_64, "x64": Architecture.X86_64,
                  "arm64": Architecture.ARM64, "aarch64": Architecture.ARM64}
_STALE_NODE_STATES = frozenset({NodeState.OFFLINE, NodeState.UNAUTHORIZED})


def parse_architecture(value: str) -> Architecture:
    try:
        return _ARCHITECTURES[value.strip().lower()]
    except KeyError:
        raise ValueError("HARDWARE_ARCHITECTURE_UNKNOWN") from None


def hardware_from_node(node: NodeDescriptor, capacity: HardwareCapacity, *,
                       machine_ref: str | None = None) -> HardwareProfile:
    """Read-only view of a compute-node descriptor plus operator-declared capacities.

    The descriptor contributes identity, platform, architecture, advertised accelerators and ``last_seen``;
    an OFFLINE or UNAUTHORIZED node is marked stale. Nothing is probed and nothing is registered.
    """
    try:
        observed = datetime.fromisoformat(node.last_seen)
    except ValueError:
        raise ValueError("HARDWARE_OBSERVED_AT_INVALID") from None
    runtimes: list[AcceleratorRuntime] = []
    for runtime in (*(_RUNTIME_FROM_ACCELERATOR[item] for item in node.accelerators), *capacity.extra_runtimes):
        if runtime not in runtimes:
            runtimes.append(runtime)
    return HardwareProfile(
        resource_id=node.node_id, node_role=NodeRole.LOCAL_COMPUTE_NODE, os=_OS_FROM_PLATFORM[node.platform],
        architecture=parse_architecture(node.architecture), cpu_cores=capacity.cpu_cores, ram_mb=capacity.ram_mb,
        gpu=capacity.gpu, vram_mb=capacity.vram_mb, unified_memory_mb=capacity.unified_memory_mb,
        runtimes=tuple(sorted(runtimes, key=RUNTIME_ORDER.__getitem__)), machine_ref=machine_ref,
        observed_at=observed, stale=node.health_state in _STALE_NODE_STATES,
    )


def hardware_from_document(document: Mapping[str, Any]) -> tuple[HardwareProfile, ...]:
    """Parse an operator hardware document (``{"schema_version", "resources": [...]}``); fails closed."""
    assert_minimized(document, allowed_keys=("schema_version", "resources"), max_string=256,
                     max_items=MAX_DOCUMENT_RESOURCES, max_total_bytes=1024 * 1024)
    if document.get("schema_version") != HARDWARE_INVENTORY_SCHEMA:
        raise ValueError("HARDWARE_INVENTORY_SCHEMA_UNSUPPORTED")
    resources = document.get("resources")
    if not isinstance(resources, list):
        raise ValueError("HARDWARE_INVENTORY_RESOURCES_INVALID")
    return tuple(HardwareProfile.model_validate(item) for item in resources)
