# SPDX-License-Identifier: AGPL-3.0-only
"""TaskContract: the closed, bounded description of one unit of capability work.

A task names WHAT is wanted (task type, required output contracts, quality, human gate) and WHICH
project-owned artifacts it may read; it never names an adapter, engine, provider, command or URL.
The planner turns it into a capability graph; nothing in a task authorizes anything — authorization
comes only from the project-bound PADOS grant for ``work_contract_id``.

The per-input ``project_id`` / ``classification`` are caller-supplied LABELS. The schema rejects labels
that are inconsistent with the task, but labels are never trusted: the planner and executor resolve
every input by content hash through the trusted ``InputProvenance`` seam (``pados.capability.provenance``)
and refuse unknown artifacts and any label that differs from the resolved value.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from pados.capability.artifacts import ArtifactRef
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_no_credentials

TASK_SCHEMA = "pados.capability.task.v1"
MAX_TASK_INPUTS = 64
MAX_PARAMETERS = 32
MAX_PARAMETER_STRING = 256
_TASK_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CONTRACT_REF = re.compile(r"^[a-z][a-z0-9_.-]{2,79}/v[0-9]{1,3}$")

Scalar = str | int | float | bool | None


class QualityRequirement(StrEnum):
    CANDIDATE = "CANDIDATE"  # outputs may stay NOT_VERIFIED candidates
    VERIFIED = "VERIFIED"  # every stage must pass an automatic verification method


class TaskRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def checked_parameters(value: Mapping[str, Any], *, code: str) -> Mapping[str, Scalar]:
    """Flat, bounded scalar parameters without credential-shaped material (immutable copy)."""
    if not isinstance(value, Mapping) or len(value) > MAX_PARAMETERS:
        raise ValueError(f"{code}_TOO_MANY")
    cleaned: dict[str, Scalar] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key):
            raise ValueError(f"{code}_KEY_INVALID")
        if not (item is None or isinstance(item, (bool, int, float, str))):
            raise ValueError(f"{code}_NOT_FLAT")
        if isinstance(item, str) and len(item) > MAX_PARAMETER_STRING:
            raise ValueError(f"{code}_VALUE_TOO_LONG")
        if isinstance(item, float) and item != item:  # NaN cannot be hashed canonically
            raise ValueError(f"{code}_VALUE_INVALID")
        cleaned[key] = item
    assert_no_credentials(dict(cleaned))
    return MappingProxyType(cleaned)


class TaskInput(BaseModel):
    """One input artifact the task may read (by content hash). ``project_id`` and ``classification`` are
    labels that must match the trusted ``InputProvenance`` resolution; they authorize nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ref: ArtifactRef
    project_id: str
    classification: DataClassification

    @field_validator("name")
    @classmethod
    def name_pattern(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("TASK_INPUT_NAME_INVALID")
        return value


class TaskContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = TASK_SCHEMA
    task_id: str
    task_type: str
    project_id: str
    work_contract_id: str
    classification: DataClassification
    network_policy: NetworkRequirement = NetworkRequirement.LOCAL_ONLY
    required_outputs: tuple[str, ...] = Field(min_length=1, max_length=16)
    quality: QualityRequirement = QualityRequirement.CANDIDATE
    risk_level: TaskRisk = TaskRisk.LOW
    human_gate: bool = False
    inputs: tuple[TaskInput, ...] = Field(default=(), max_length=MAX_TASK_INPUTS)
    parameters: Mapping[str, Scalar] = Field(default_factory=dict, validate_default=True)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != TASK_SCHEMA:
            raise ValueError("TASK_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("task_type")
    @classmethod
    def task_type_pattern(cls, value: str) -> str:
        if not _TASK_TYPE.fullmatch(value):
            raise ValueError("TASK_TYPE_INVALID")
        return value

    @field_validator("task_id", "project_id", "work_contract_id")
    @classmethod
    def identifiers(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("TASK_IDENTIFIER_INVALID")
        return value

    @field_validator("required_outputs")
    @classmethod
    def output_contracts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CONTRACT_REF.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("TASK_REQUIRED_OUTPUT_INVALID")
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def bounded_parameters(cls, value: Any) -> Mapping[str, Scalar]:
        return checked_parameters(value, code="TASK_PARAMETERS")

    @field_validator("parameters", mode="after")
    @classmethod
    def immutable_parameters(cls, value: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def plain_parameters(self, value: Mapping[str, Scalar]) -> dict[str, Scalar]:
        return dict(value)

    @model_validator(mode="after")
    def inputs_belong_to_the_task(self) -> TaskContract:
        """Schema consistency of the LABELS only; trusted provenance is checked by planner and executor."""
        names = [item.name for item in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("TASK_INPUT_NAME_DUPLICATE")
        for item in self.inputs:
            if item.project_id != self.project_id:
                raise ValueError("TASK_INPUT_PROJECT_MISMATCH")
            if not self.classification.permits(item.classification):
                raise ValueError("TASK_INPUT_CLASSIFICATION_EXCEEDS_TASK")
        return self

    def input_ref(self, name: str) -> ArtifactRef:
        for item in self.inputs:
            if item.name == name:
                return item.ref
        raise KeyError("TASK_INPUT_UNKNOWN")

    @property
    def task_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def work_contract(self, capability_grants: tuple[str, ...]) -> dict[str, Any]:
        """The work-contract document handed to the existing grant validator for these capabilities."""
        return {"work_contract_id": self.work_contract_id, "project_id": self.project_id,
                "required_capabilities": sorted(capability_grants)}


def validated_task(task: TaskContract) -> TaskContract:
    """Re-validate (a ``model_construct``-forged task cannot skip the schema)."""
    data = task.model_dump(mode="python")
    data["parameters"] = dict(task.parameters) if isinstance(task.parameters, Mapping) else task.parameters
    return TaskContract.model_validate(data)
