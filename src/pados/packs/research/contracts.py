# SPDX-License-Identifier: AGPL-3.0-only
"""Contracts of the research pack (RESEARCH_CAMPAIGN over an external AI-Scientist-v3-shaped runtime).

* ``CampaignRequest`` is the task-owned request: an allowlisted agent, the requested phases, a budget profile, an
  optional resume of a named run. No commands, host paths, URLs, flags or credential values can be expressed.
* ``ResearchRunOutput`` is what an UNTRUSTED runtime returns: exit code, run id, phase states, relative files,
  claims and the egress it reports. Nothing in it is believed until ``build_run_manifest`` admits it.
* ``ResearchRunManifest`` is the bounded record PADOS derives: phases, digest-addressed artifacts, the quarantine
  count, the final-evaluation metrics read from the artifact itself, and every claim marked EVIDENCED only when
  that artifact supports it. It is a candidate: ``semantic_authority = NONE`` and no acceptance field.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA = "pados.research.run-manifest.v1"
REQUEST_SCHEMA = "pados.research.campaign-request.v1"
MAX_FILES = 256
MAX_CLAIMS = 64

_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class ResearchError(ValueError):
    """Closed-code refusal; never carries runtime output, paths or credential-shaped text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResearchPhase(StrEnum):
    """The upstream campaign phases, in their only legal order."""

    TASK_DRAFT = "task_draft"
    INVESTIGATION = "investigation"
    ABLATION = "ablation"
    FINAL_EVALUATION = "final_evaluation"
    POSTHOC = "posthoc"
    PAPER = "paper"
    REVIEW = "review"


PHASE_ORDER = tuple(ResearchPhase)


class PhaseState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    PENDING = "PENDING"


class ArtifactKind(StrEnum):
    TASK_SNAPSHOT = "task_snapshot"
    INVESTIGATION = "investigation"
    ABLATION = "ablation"
    FINAL_EVALUATION = "final_evaluation"
    POSTHOC = "posthoc"
    PAPER = "paper"
    REVIEW = "review"
    TELEMETRY = "telemetry"


class RunStatus(StrEnum):
    COMPLETED = "COMPLETED"  # every requested phase completed
    PARTIAL = "PARTIAL"  # some phases completed, none failed, the rest pending or skipped
    FAILED = "FAILED"  # a phase failed


class ClaimStatus(StrEnum):
    EVIDENCED = "EVIDENCED"  # the final-evaluation artifact states exactly this metric value
    NOT_EVIDENCED = "NOT_EVIDENCED"  # recorded, never presented


class ResumeSpec(_Frozen):
    upstream_run_id: str
    completed_phases: tuple[ResearchPhase, ...] = Field(max_length=len(PHASE_ORDER))

    @field_validator("upstream_run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("RESEARCH_REQUEST_INVALID")
        return value


class CampaignRequest(_Frozen):
    schema_version: Literal["pados.research.campaign-request.v1"] = "pados.research.campaign-request.v1"
    agent: str
    model: str | None = None
    preset: str | None = None
    phases: tuple[ResearchPhase, ...] = Field(min_length=1, max_length=len(PHASE_ORDER))
    budget_profile: str
    require_final_evaluation: bool = True
    resume: ResumeSpec | None = None

    @field_validator("agent", "budget_profile")
    @classmethod
    def _tokens(cls, value: str) -> str:
        if not _TOKEN.fullmatch(value):
            raise ValueError("RESEARCH_REQUEST_INVALID")
        return value

    @field_validator("model", "preset")
    @classmethod
    def _optional_tokens(cls, value: str | None) -> str | None:
        if value is not None and not _TOKEN.fullmatch(value):
            raise ValueError("RESEARCH_REQUEST_INVALID")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> CampaignRequest:
        if len(set(self.phases)) != len(self.phases) or list(self.phases) != sorted(self.phases, key=PHASE_ORDER.index):
            raise ValueError("RESEARCH_REQUEST_INVALID")
        if self.require_final_evaluation and ResearchPhase.FINAL_EVALUATION not in self.phases:
            raise ValueError("RESEARCH_REQUEST_INVALID")
        return self


class ResearchRunOutput(_Frozen):
    """Raw runtime output. Shape only; every semantic check happens when PADOS builds the manifest."""

    exit_code: int
    upstream_run_id: str
    phases: Mapping[str, str] = Field(max_length=16)
    files: Mapping[str, bytes] = Field(max_length=MAX_FILES * 2)
    claims: tuple[Mapping[str, object], ...] = Field(default=(), max_length=MAX_CLAIMS)
    egress_hosts_contacted: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("upstream_run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise ValueError("RESEARCH_MANIFEST_MALFORMED")
        return value


class PhaseRecord(_Frozen):
    phase: ResearchPhase
    state: PhaseState


class ArtifactEntry(_Frozen):
    kind: ArtifactKind
    relative_path: str
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("RESEARCH_MANIFEST_MALFORMED")
        return value


class ClaimRecord(_Frozen):
    metric: str
    value: float
    status: ClaimStatus

    @field_validator("metric")
    @classmethod
    def _metric(cls, value: str) -> str:
        if not _TOKEN.fullmatch(value):
            raise ValueError("RESEARCH_MANIFEST_MALFORMED")
        return value


class ResearchRunManifest(_Frozen):
    schema_version: Literal["pados.research.run-manifest.v1"] = "pados.research.run-manifest.v1"
    profile_id: str
    upstream_repository: str
    upstream_revision: str
    image_digest: str
    upstream_run_id: str
    request_digest: str
    task_snapshot_digest: str
    phases: tuple[PhaseRecord, ...]
    artifacts: tuple[ArtifactEntry, ...] = Field(max_length=MAX_FILES)
    quarantined_count: int = Field(ge=0)
    final_evaluation: dict[str, float] | None
    claims: tuple[ClaimRecord, ...] = Field(max_length=MAX_CLAIMS)
    status: RunStatus
    review_required: bool
    semantic_authority: Literal["NONE"] = "NONE"


class ResearchRuntimeUnavailable(RuntimeError):
    """The runtime (daemon, image, credentials) cannot run right now: retryable, carries no content."""


class ResearchRuntimeTimeout(RuntimeError):
    """The runtime exceeded its bound: failed, never retried with a weaker network or model class."""


class ResearchRuntimeProfile(BaseModel):
    """An operator profile: what may run, where, with which egress and secret references. Never a path or value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    upstream_repository: str
    upstream_revision: str
    image_digest: str
    agents: tuple[str, ...] = Field(min_length=1, max_length=16)
    models: tuple[str, ...] = Field(default=(), max_length=16)  # allowlisted models; empty = none may be named
    presets: tuple[str, ...] = Field(default=(), max_length=16)  # allowlisted upstream presets; empty = none
    egress_hosts: tuple[str, ...] = Field(default=(), max_length=16)
    timeout_seconds: int = Field(ge=1, le=86_400)
    secret_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("profile_id")
    @classmethod
    def _profile_id(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("RESEARCH_PROFILE_INVALID")
        return value

    @field_validator("upstream_repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        if not _REPOSITORY.fullmatch(value):
            raise ValueError("RESEARCH_PROFILE_INVALID")
        return value

    @field_validator("upstream_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        if not _REVISION.fullmatch(value):
            raise ValueError("RESEARCH_PROFILE_INVALID")  # a pinned commit, never a branch or tag
        return value

    @field_validator("image_digest")
    @classmethod
    def _image(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("RESEARCH_PROFILE_INVALID")  # a content digest, never ``latest``
        return value

    @field_validator("agents", "models", "presets")
    @classmethod
    def _agents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _TOKEN.fullmatch(agent) for agent in value) or len(set(value)) != len(value):
            raise ValueError("RESEARCH_PROFILE_INVALID")
        return value

    @property
    def adapter_id(self) -> str:
        return f"research.campaign.{self.profile_id}"
