# SPDX-License-Identifier: AGPL-3.0-only
"""Capability graph: a validated DAG of stages, and declarative decomposition templates.

A stage names a capability id (never an adapter, engine or provider), where its inputs come from
(named task inputs or earlier stages' outputs) and bounded scalar parameters. V1 decomposition is
data only: ``TemplateRegistry`` maps a task type to an operator-registered template; no language
model decomposes tasks, so a task cannot invent stages, capabilities or edges.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from pados.capability.task import Scalar, TaskContract, checked_parameters
from pados.execution.context import canonical_hash

GRAPH_SCHEMA = "pados.capability.graph.v1"
TEMPLATE_SCHEMA = "pados.capability.template.v1"
MAX_STAGES = 64
MAX_STAGE_INPUTS = 16
MAX_TEMPLATES = 256
_STAGE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_KIND = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_TASK_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")


class GraphError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InputSource(StrEnum):
    TASK_INPUT = "TASK_INPUT"  # a named, project-checked TaskContract input
    STAGE_OUTPUT = "STAGE_OUTPUT"  # every output (optionally of one artifact kind) of an earlier stage


class InputBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: InputSource
    name: str  # task input name, or upstream stage id
    artifact_kind: str | None = None  # STAGE_OUTPUT only: filter upstream outputs by artifact kind

    @model_validator(mode="after")
    def well_formed(self) -> InputBinding:
        pattern = _NAME if self.source is InputSource.TASK_INPUT else _STAGE_ID
        if not pattern.fullmatch(self.name):
            raise ValueError("GRAPH_INPUT_NAME_INVALID")
        if self.artifact_kind is not None and (self.source is InputSource.TASK_INPUT
                                               or not _KIND.fullmatch(self.artifact_kind)):
            raise ValueError("GRAPH_INPUT_KIND_INVALID")
        return self


class GraphStage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage_id: str
    capability_id: str
    inputs: tuple[InputBinding, ...] = Field(default=(), max_length=MAX_STAGE_INPUTS)
    parameters: Mapping[str, Scalar] = Field(default_factory=dict, validate_default=True)
    task_parameters: tuple[str, ...] = Field(default=(), max_length=16)  # names copied from the task
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)

    @field_validator("stage_id")
    @classmethod
    def stage_id_pattern(cls, value: str) -> str:
        if not _STAGE_ID.fullmatch(value):
            raise ValueError("GRAPH_STAGE_ID_INVALID")
        return value

    @field_validator("capability_id")
    @classmethod
    def capability_id_pattern(cls, value: str) -> str:
        if not _CAPABILITY_ID.fullmatch(value):
            raise ValueError("GRAPH_CAPABILITY_ID_INVALID")
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def bounded_parameters(cls, value: Any) -> Mapping[str, Scalar]:
        return checked_parameters(value, code="GRAPH_PARAMETERS")

    @field_validator("parameters", mode="after")
    @classmethod
    def immutable_parameters(cls, value: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def plain_parameters(self, value: Mapping[str, Scalar]) -> dict[str, Scalar]:
        return dict(value)

    @field_validator("task_parameters")
    @classmethod
    def parameter_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _NAME.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("GRAPH_TASK_PARAMETER_INVALID")
        return value

    def upstream(self) -> tuple[str, ...]:
        return tuple(sorted({item.name for item in self.inputs if item.source is InputSource.STAGE_OUTPUT}))


class CapabilityGraph(BaseModel):
    """Stages in declaration order; edges are the STAGE_OUTPUT input bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = GRAPH_SCHEMA
    stages: tuple[GraphStage, ...] = Field(min_length=1, max_length=MAX_STAGES)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != GRAPH_SCHEMA:
            raise ValueError("GRAPH_SCHEMA_UNSUPPORTED")
        return value

    @model_validator(mode="after")
    def acyclic(self) -> CapabilityGraph:
        ids = [stage.stage_id for stage in self.stages]
        if len(set(ids)) != len(ids):
            raise ValueError("GRAPH_STAGE_ID_DUPLICATE")
        known = set(ids)
        for stage in self.stages:
            if any(name not in known for name in stage.upstream()):
                raise ValueError("GRAPH_UPSTREAM_STAGE_UNKNOWN")
        topological_order(self.stages)  # raises GraphError("GRAPH_CYCLE")
        return self

    def stage(self, stage_id: str) -> GraphStage:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise GraphError("GRAPH_STAGE_UNKNOWN")

    def ordered(self) -> tuple[GraphStage, ...]:
        return topological_order(self.stages)

    def terminal_stage_ids(self) -> tuple[str, ...]:
        consumed = {name for stage in self.stages for name in stage.upstream()}
        return tuple(stage.stage_id for stage in self.stages if stage.stage_id not in consumed)

    @property
    def graph_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def topological_order(stages: tuple[GraphStage, ...]) -> tuple[GraphStage, ...]:
    """Kahn's algorithm; ready stages are taken in declaration order (deterministic). Cycles fail closed."""
    position = {stage.stage_id: index for index, stage in enumerate(stages)}
    pending = {stage.stage_id: set(stage.upstream()) for stage in stages}
    by_id = {stage.stage_id: stage for stage in stages}
    ordered: list[GraphStage] = []
    while pending:
        ready = sorted((sid for sid, deps in pending.items() if not deps), key=position.__getitem__)
        if not ready:
            raise GraphError("GRAPH_CYCLE")
        for sid in ready:
            ordered.append(by_id[sid])
            del pending[sid]
        for deps in pending.values():
            deps.difference_update(ready)
    return tuple(ordered)


class DecompositionTemplate(BaseModel):
    """Operator-registered data mapping one task type to one capability graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = TEMPLATE_SCHEMA
    template_id: str
    version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
    task_type: str
    graph: CapabilityGraph

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != TEMPLATE_SCHEMA:
            raise ValueError("TEMPLATE_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("template_id")
    @classmethod
    def template_id_pattern(cls, value: str) -> str:
        if not _TEMPLATE_ID.fullmatch(value):
            raise ValueError("TEMPLATE_ID_INVALID")
        return value

    @field_validator("task_type")
    @classmethod
    def task_type_pattern(cls, value: str) -> str:
        if not _TASK_TYPE.fullmatch(value):
            raise ValueError("TASK_TYPE_INVALID")
        return value

    @property
    def template_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class TemplateRegistry:
    """task_type -> template. Registration re-validates; duplicates are refused (no silent override)."""

    def __init__(self) -> None:
        self._by_type: dict[str, DecompositionTemplate] = {}

    def register(self, template: DecompositionTemplate | Mapping[str, Any]) -> DecompositionTemplate:
        try:
            raw = template.model_dump(mode="json") if isinstance(template, DecompositionTemplate) else dict(template)
            validated = DecompositionTemplate.model_validate(raw)
        except (ValueError, TypeError) as exc:
            raise GraphError("TEMPLATE_INVALID") from exc
        if validated.task_type in self._by_type:
            raise GraphError("TEMPLATE_TASK_TYPE_ALREADY_REGISTERED")
        if len(self._by_type) >= MAX_TEMPLATES:
            raise GraphError("TEMPLATE_REGISTRY_FULL")
        self._by_type[validated.task_type] = validated
        return validated

    def template_for(self, task_type: str) -> DecompositionTemplate:
        template = self._by_type.get(task_type)
        if template is None:
            raise GraphError("TEMPLATE_NOT_FOUND")
        return template

    def decompose(self, task: TaskContract) -> tuple[DecompositionTemplate, CapabilityGraph]:
        """Instantiate the template's graph for one task (task parameters copied only where named)."""
        template = self.template_for(task.task_type)
        names = {item.name for item in task.inputs}
        stages: list[GraphStage] = []
        for stage in template.graph.stages:
            for binding in stage.inputs:
                if binding.source is InputSource.TASK_INPUT and binding.name not in names:
                    raise GraphError("GRAPH_TASK_INPUT_MISSING")
            merged = dict(stage.parameters)
            for name in stage.task_parameters:
                if name in task.parameters:
                    merged[name] = task.parameters[name]
            stages.append(stage.model_copy(update={"parameters": checked_parameters(merged,
                                                                                    code="GRAPH_PARAMETERS")}))
        return template, CapabilityGraph(stages=tuple(stages))
