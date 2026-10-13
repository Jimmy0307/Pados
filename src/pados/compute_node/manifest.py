# SPDX-License-Identifier: AGPL-3.0-only
"""Node and model manifests (the node protocol's descriptive contract).

Reuses ``pados.enterprise.compute.ComputeNode`` and the enterprise
classification; node-specific fields (name, architecture, runtime version,
load, model manifests) extend it without replacing it.
"""
from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.enterprise.classification import DataClassification
from pados.enterprise.compute import Accelerator, ComputeNode, NodePlatform, ResourceClass
from pados.enterprise.manifest import CapabilityHealth

NODE_PROTOCOL_VERSION = "pados.compute-node.protocol.v1"
NODE_RUNTIME_VERSION = "pados-lcn-1.0.0"


class NodeCapability(StrEnum):
    LOCAL_INFERENCE = "LOCAL_INFERENCE"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"
    CLASSIFICATION = "CLASSIFICATION"
    DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
    OCR = "OCR"
    EVALUATION = "EVALUATION"


# V1 implements these; the others may be advertised only by future adapters.
IMPLEMENTED_CAPABILITIES = frozenset({NodeCapability.LOCAL_INFERENCE, NodeCapability.EMBEDDING,
                                      NodeCapability.CLASSIFICATION})


class NodeState(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    UNAUTHORIZED = "UNAUTHORIZED"

    @property
    def health(self) -> CapabilityHealth:
        return _HEALTH[self]


_HEALTH = {
    NodeState.ONLINE: CapabilityHealth.READY,
    NodeState.DEGRADED: CapabilityHealth.DEGRADED,
    NodeState.BUSY: CapabilityHealth.DEGRADED,
    NodeState.OFFLINE: CapabilityHealth.UNAVAILABLE,
    NodeState.UNAUTHORIZED: CapabilityHealth.UNAUTHORIZED,
}


class ModelAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    UNVERIFIED = "UNVERIFIED"


_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}/[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}@[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")
# Public, stable form of the canonical model-id rule (consumed by resource-aware planning).
MODEL_ID_PATTERN = _MODEL_ID.pattern
_DIGEST = re.compile(r"^(sha256:)?[a-f0-9]{12,64}$")


class ModelManifest(BaseModel):
    """One advertised model. ``model_id`` is the canonical identity
    (``<backend>/<backend model>@<version|digest>``); aliases are labels only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    backend: str = Field(min_length=1, max_length=40)
    backend_model: str = Field(min_length=1, max_length=160)
    family: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=80)
    quantization: str | None = Field(default=None, max_length=40)
    context_limit_tokens: int = Field(ge=1, le=10_000_000)
    tasks: tuple[NodeCapability, ...] = Field(min_length=1)
    resource_class: ResourceClass
    privacy_class: DataClassification
    digest: str | None = None
    aliases: tuple[str, ...] = ()
    availability: ModelAvailability = ModelAvailability.UNVERIFIED

    @field_validator("model_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        if not _MODEL_ID.fullmatch(value):
            raise ValueError("MODEL_ID_NOT_CANONICAL")
        return value

    @field_validator("digest")
    @classmethod
    def valid_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("MODEL_DIGEST_INVALID")
        return value

    @model_validator(mode="after")
    def identity_is_not_an_alias(self) -> ModelManifest:
        backend, rest = self.model_id.split("/", 1)
        name, _, _version = rest.rpartition("@")
        if backend != self.backend or name != self.backend_model:
            raise ValueError("MODEL_ID_BACKEND_MISMATCH")
        if self.model_id in self.aliases or any("/" in alias and "@" in alias for alias in self.aliases):
            raise ValueError("MODEL_ALIAS_CANNOT_BE_IDENTITY")
        return self

    @staticmethod
    def canonical(backend: str, backend_model: str, version: str) -> str:
        return f"{backend}/{backend_model}@{version}"


class NodeLoad(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    running: int = Field(ge=0)
    queued: int = Field(ge=0)
    max_concurrent_jobs: int = Field(ge=1)
    max_queue_depth: int = Field(ge=0)

    @property
    def saturated(self) -> bool:
        return self.running >= self.max_concurrent_jobs and self.queued >= self.max_queue_depth


class NodeLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_jobs: int = Field(default=1, ge=1, le=64)
    max_queue_depth: int = Field(default=4, ge=0, le=256)
    default_timeout_seconds: float = Field(default=60, gt=0, le=3600)
    max_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_input_chars: int = Field(default=32_000, ge=1, le=2_000_000)
    max_output_tokens: int = Field(default=1024, ge=1, le=65_536)
    max_batch_items: int = Field(default=16, ge=1, le=1024)


class NodeDescriptor(BaseModel):
    """What a node advertises about itself. Descriptive only; never authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: str = NODE_PROTOCOL_VERSION
    node_id: str = Field(pattern=r"^lcn-[a-f0-9]{20}$")
    node_name: str = Field(min_length=1, max_length=80)
    platform: NodePlatform
    architecture: str = Field(min_length=1, max_length=40)
    endpoint: str = Field(min_length=1, max_length=200)
    runtime_version: str = NODE_RUNTIME_VERSION
    health_state: NodeState
    health_reasons: tuple[str, ...] = ()
    capabilities: tuple[NodeCapability, ...]
    models: tuple[ModelManifest, ...]
    privacy_class: DataClassification
    resource_class: ResourceClass
    accelerators: tuple[Accelerator, ...] = (Accelerator.CPU,)
    last_seen: str
    current_load: NodeLoad
    limits: NodeLimits

    def as_compute_node(self) -> ComputeNode:
        """The Enterprise Pack compute-node view of this descriptor."""
        return ComputeNode(
            node_id=self.node_id, endpoint=self.endpoint, platform=self.platform,
            capabilities=tuple(c.value for c in self.capabilities), health=self.health_state.health,
            resource_class=self.resource_class, privacy_class=self.privacy_class,
            models=tuple(m.model_id for m in self.models),
        )

    @property
    def manifest_hash(self) -> str:
        """Hash of the capability/model manifest (volatile load and timestamps excluded)."""
        return manifest_hash(self.model_dump(mode="json"))


def manifest_hash(descriptor: dict[str, Any]) -> str:
    stable = {key: descriptor[key] for key in ("node_id", "capabilities", "privacy_class", "limits")}
    stable["models"] = [{k: v for k, v in model.items() if k != "availability"} for model in descriptor["models"]]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()
