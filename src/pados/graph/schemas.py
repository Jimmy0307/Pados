# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NodeType(StrEnum):
    IDEA = "IDEA"
    CONCEPT = "CONCEPT"
    ACTION = "ACTION"
    EXPERT = "EXPERT"
    RESOURCE = "RESOURCE"
    OUTPUT = "OUTPUT"


class Provenance(StrEnum):
    HUMAN_CREATED = "HUMAN_CREATED"
    AI_PROPOSED = "AI_PROPOSED"
    SYSTEM_PROPOSED = "SYSTEM_PROPOSED"
    IMPORTED = "IMPORTED"


class AssuranceStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    VALIDATED = "VALIDATED"
    PROMOTED = "PROMOTED"


class IdeaState(StrEnum):
    ACTIVE = "ACTIVE"
    INCUBATING = "INCUBATING"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class ReuseMode(StrEnum):
    REFERENCE = "REFERENCE"
    FORK = "FORK"
    SNAPSHOT_COPY = "SNAPSHOT_COPY"


class CognitiveOperation(StrEnum):
    THINK = "THINK"
    EXPAND = "EXPAND"
    CONNECT = "CONNECT"
    CRITIQUE = "CRITIQUE"
    INCUBATE = "INCUBATE"


class ModelSlot(StrEnum):
    THINKING = "thinking"
    FAST = "fast"
    EMBEDDING = "embedding"
    EXECUTOR = "executor"


GRAPH_RELATION_TYPES = frozenset(
    {
        "RELATED_TO",
        "RELATES_TO",
        "SUPPORTS",
        "CONTRADICTS",
        "REQUIRES",
        "DERIVED_FROM",
        "DERIVES_FROM",
        "EXPANDS",
        "PRODUCED",
        "USES_SOURCE",
    }
)

_RELATION_ALIASES = {
    "DECOMPOSES": "DERIVED_FROM",
    "INFORMS": "PRODUCED",
    "REFINES": "EXPANDS",
}


class AutoRefreshPolicy(StrEnum):
    OFF = "AUTO_REFRESH_OFF"
    PROMPT = "AUTO_REFRESH_PROMPT"
    ON = "AUTO_REFRESH_ON"


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    project_id: str | None = None


class NodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_type: NodeType
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(default="", max_length=20_000)
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    group_id: str | None = None


class SystemNodeCreate(NodeCreate):
    """Server-owned graph fields that ordinary node creation cannot assert."""

    provenance: Provenance = Provenance.HUMAN_CREATED
    assurance_status: AssuranceStatus = AssuranceStatus.PROVISIONAL
    idea_state: IdeaState | None = None
    source_capability_id: str | None = Field(default=None, max_length=160)
    reuse_mode: ReuseMode | None = None
    source_version: str | None = Field(default=None, max_length=80)
    parent_capability_id: str | None = Field(default=None, max_length=160)
    internal_ref_type: str | None = Field(default=None, max_length=80)
    internal_ref_id: str | None = Field(default=None, max_length=160)
    lineage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_idea_state(self) -> SystemNodeCreate:
        if self.semantic_type == NodeType.IDEA and self.idea_state is None:
            self.idea_state = IdeaState.ACTIVE
        if self.semantic_type != NodeType.IDEA and self.idea_state is not None:
            raise ValueError("idea_state is valid only for IDEA nodes")
        return self


class NodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=240)
    content: str | None = Field(default=None, max_length=20_000)
    x: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    y: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    group_id: str | None = None
    clear_group: bool = False
    version: int = Field(gt=0)


class SystemNodeUpdate(NodeUpdate):
    """Governed lifecycle fields available only to server-side operations."""

    assurance_status: AssuranceStatus | None = None
    idea_state: IdeaState | None = None
    metadata: dict[str, Any] | None = None


class EdgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_node_id: str
    target_node_id: str
    relationship_type: str = Field(default="RELATED_TO", min_length=1, max_length=80)
    label: str = Field(default="", max_length=240)

    @field_validator("relationship_type")
    @classmethod
    def bounded_relationship_type(cls, value: str) -> str:
        normalized = _RELATION_ALIASES.get(value.strip().upper(), value.strip().upper())
        if normalized not in GRAPH_RELATION_TYPES:
            raise ValueError("relationship_type is outside the bounded graph relation vocabulary")
        return normalized

    @model_validator(mode="after")
    def distinct_nodes(self) -> EdgeCreate:
        if self.source_node_id == self.target_node_id:
            raise ValueError("edge endpoints must differ")
        return self


class SystemEdgeCreate(EdgeCreate):
    """Server-owned edge provenance and metadata."""

    provenance: Provenance = Provenance.HUMAN_CREATED
    metadata: dict[str, Any] = Field(default_factory=dict)


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    x: float
    y: float
    width: float = Field(gt=0, le=1_000_000)
    height: float = Field(gt=0, le=1_000_000)
    node_ids: list[str] = Field(default_factory=list, max_length=500)


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: list[str] = Field(min_length=1, max_length=200)

    @field_validator("node_ids")
    @classmethod
    def unique_nodes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("selection contains duplicate node ids")
        return values


class CognitiveRequest(SelectionRequest):
    operation: CognitiveOperation
    model_selection: ModelSelection | None = None


class SynthesisRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_selection: ModelSelection | None = None


class SynthesisPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: AutoRefreshPolicy


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: ModelSlot
    provider: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    model: str = Field(default="", max_length=240, pattern=r"^[^\r\n]*$")


class CapabilityReuseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reuse_mode: ReuseMode
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)


class UrlIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4096)
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20_000)
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)


class ActionDisabledReason(BaseModel):
    code: str
    message: str


class SelectionAction(BaseModel):
    id: str
    label: str
    allowed: bool
    disabled_reasons: list[ActionDisabledReason] = Field(default_factory=list)
    intent_class: str | None = None
    lane: str | None = None
    primary_message: str | None = None
