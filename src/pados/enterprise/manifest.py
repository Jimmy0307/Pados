# SPDX-License-Identifier: AGPL-3.0-only
"""Enterprise capability manifest.

A manifest describes an installed capability. It never authorizes one: use of
a capability still needs a project-bound PADOS grant
(``pados.execution.authorization``). Provider routing stays with the PADOS
Worker Router; manifests carry no routing policy.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import is_secret_ref, looks_like_secret

MANIFEST_SCHEMA_VERSION = "pados.enterprise.capability-manifest.v1"


class CapabilityType(StrEnum):
    KNOWLEDGE_RETRIEVAL = "KNOWLEDGE_RETRIEVAL"
    OCR = "OCR"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"
    DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    LOCAL_INFERENCE = "LOCAL_INFERENCE"
    ENTERPRISE_TOOL = "ENTERPRISE_TOOL"
    MCP_TOOL = "MCP_TOOL"


class ExecutionMode(StrEnum):
    IN_PROCESS = "IN_PROCESS"
    LOCAL_SUBPROCESS = "LOCAL_SUBPROCESS"
    LOCAL_HTTP = "LOCAL_HTTP"
    REMOTE_HTTP = "REMOTE_HTTP"
    MCP = "MCP"

    @property
    def leaves_host(self) -> bool:
        return self is ExecutionMode.REMOTE_HTTP


class CapabilityHealth(StrEnum):
    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    UNAUTHORIZED = "UNAUTHORIZED"
    DEGRADED = "DEGRADED"


class ArtifactContract(BaseModel):
    """What the capability returns, and that it is never semantic truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_kind: str = Field(min_length=1, max_length=80)
    schema_ref: str = Field(min_length=1, max_length=160)
    content_hashed: bool = True
    semantic_authority: str = "NONE"

    @field_validator("semantic_authority")
    @classmethod
    def no_semantic_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("ENTERPRISE_CAPABILITY_CANNOT_OWN_SEMANTIC_TRUTH")
        return value


_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


class EnterpriseCapabilityManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = MANIFEST_SCHEMA_VERSION
    capability_id: str
    capability_type: CapabilityType
    version: str = Field(min_length=1, max_length=40)
    supported_inputs: tuple[str, ...] = Field(min_length=1)
    supported_outputs: tuple[str, ...] = Field(min_length=1)
    privacy_classes: tuple[DataClassification, ...] = Field(min_length=1)
    required_permissions: tuple[str, ...] = ()
    execution_mode: ExecutionMode
    health: CapabilityHealth = CapabilityHealth.NOT_CONFIGURED
    provenance_support: bool
    artifact_contract: ArtifactContract
    secret_refs: tuple[str, ...] = ()
    implementation: str | None = Field(default=None, max_length=160)

    @field_validator("capability_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("INVALID_CAPABILITY_ID")
        return value

    @field_validator("secret_refs")
    @classmethod
    def secret_refs_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not is_secret_ref(item) for item in value):
            raise ValueError("SECRET_REFERENCE_REQUIRED")
        return value

    @model_validator(mode="after")
    def no_inline_secrets(self) -> EnterpriseCapabilityManifest:
        if not self.provenance_support and self.capability_type in _EVIDENCE_TYPES:
            raise ValueError("EVIDENCE_CAPABILITY_REQUIRES_PROVENANCE")
        if _contains_secret(self.model_dump(mode="json", exclude={"secret_refs"})):
            raise ValueError("MANIFEST_CONTAINS_SECRET_MATERIAL")
        return self

    @property
    def max_classification(self) -> DataClassification:
        return max(self.privacy_classes, key=lambda item: item.rank)

    @property
    def grant_capability(self) -> str:
        """The capability name a PADOS project grant must contain."""
        return f"ENTERPRISE:{self.capability_id}"


_EVIDENCE_TYPES = {
    CapabilityType.KNOWLEDGE_RETRIEVAL, CapabilityType.OCR, CapabilityType.DOCUMENT_EXTRACTION,
    CapabilityType.RERANK,
}


def _contains_secret(value: Any) -> bool:
    if isinstance(value, str):
        return looks_like_secret(value)
    if isinstance(value, dict):
        return any(_contains_secret(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False
