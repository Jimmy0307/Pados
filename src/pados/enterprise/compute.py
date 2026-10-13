# SPDX-License-Identifier: AGPL-3.0-only
"""Provider capability metadata and the minimal compute-node contract.

This is descriptive metadata for the existing PADOS Worker Router; it does not
select providers and does not schedule work. ``LocalInferenceProvider`` is the
contract a future Ollama / MLX / llama.cpp / OpenAI-compatible / vLLM adapter
implements; no platform-specific node is implemented in V1.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.enterprise.classification import DataClassification
from pados.enterprise.manifest import CapabilityHealth
from pados.worker.schemas import WorkerSpec, WorkerType


class Locality(StrEnum):
    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class Accelerator(StrEnum):
    CPU = "CPU"
    CUDA = "CUDA"
    APPLE_SILICON = "APPLE_SILICON"


class CostClass(StrEnum):
    FREE_LOCAL = "FREE_LOCAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class LatencyClass(StrEnum):
    INTERACTIVE = "INTERACTIVE"
    BATCH = "BATCH"
    UNKNOWN = "UNKNOWN"


class ResourceClass(StrEnum):
    NONE = "NONE"
    CPU_LIGHT = "CPU_LIGHT"
    CPU_HEAVY = "CPU_HEAVY"
    GPU = "GPU"


class ProviderCapabilityProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1, max_length=80)
    locality: Locality
    accelerators: tuple[Accelerator, ...] = Field(min_length=1)
    privacy_class: DataClassification
    context_limit_tokens: int = Field(ge=0)
    model_family: str = Field(min_length=1, max_length=80)
    cost_class: CostClass = CostClass.UNKNOWN
    latency_class: LatencyClass = LatencyClass.UNKNOWN
    supported_tasks: tuple[str, ...] = Field(min_length=1)

    def permits(self, classification: DataClassification) -> bool:
        return self.privacy_class.permits(classification)


_REMOTE_WORKERS = {WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI}


def profile_for_worker(worker: WorkerSpec) -> ProviderCapabilityProfile:
    """Describe an existing Worker Router worker; unknown facts stay UNKNOWN.

    Remote model workers are capped at INTERNAL: PADOS has no contractual
    evidence that CONFIDENTIAL/RESTRICTED data may leave the host.
    """
    remote = worker.worker_type in _REMOTE_WORKERS
    return ProviderCapabilityProfile(
        provider_id=worker.worker_id,
        locality=Locality.REMOTE if remote else Locality.LOCAL,
        accelerators=(Accelerator.CPU,),
        privacy_class=DataClassification.INTERNAL if remote else DataClassification.RESTRICTED,
        context_limit_tokens=worker.max_context_tokens,
        model_family=worker.worker_type.value,
        cost_class=CostClass.UNKNOWN if remote else CostClass.FREE_LOCAL,
        latency_class=LatencyClass.UNKNOWN,
        supported_tasks=tuple(sorted(capability.value for capability in worker.capabilities)) or ("NONE",),
    )


class NodePlatform(StrEnum):
    LINUX = "LINUX"
    WINDOWS = "WINDOWS"
    MACOS = "MACOS"


_LOCAL_ENDPOINT = re.compile(r"^(https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?(/.*)?|local://[a-z0-9_.-]+)$")


class ComputeNode(BaseModel):
    """A registered compute node. Registration is description, not authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    endpoint: str = Field(min_length=1, max_length=200)
    platform: NodePlatform
    capabilities: tuple[str, ...] = ()
    health: CapabilityHealth = CapabilityHealth.NOT_CONFIGURED
    resource_class: ResourceClass
    privacy_class: DataClassification
    models: tuple[str, ...] = ()

    @field_validator("endpoint")
    @classmethod
    def no_credentials_in_endpoint(cls, value: str) -> str:
        if "@" in value.split("//", 1)[-1].split("/", 1)[0]:
            raise ValueError("ENDPOINT_MUST_NOT_EMBED_CREDENTIALS")
        return value

    @property
    def is_loopback(self) -> bool:
        return _LOCAL_ENDPOINT.fullmatch(self.endpoint) is not None

    def eligible_for(self, classification: DataClassification, capability: str) -> bool:
        """Descriptive eligibility only; selection remains with the Worker Router."""
        return (self.health is CapabilityHealth.READY and capability in self.capabilities
                and self.privacy_class.permits(classification))


class LocalInferenceProvider(Protocol):
    """Future local inference adapters (not implemented in V1)."""

    @property
    def profile(self) -> ProviderCapabilityProfile: ...

    def health(self) -> CapabilityHealth: ...

    def generate(self, prompt: str, *, max_tokens: int) -> str: ...
