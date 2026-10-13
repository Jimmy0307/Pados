# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["OK", "WARNING", "ERROR", "UNKNOWN"]


class Pagination(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_next: bool


class StatusItem(BaseModel):
    name: str
    status: Status
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Status
    version: str
    web_version: str
    environment: str
    database: dict[str, Any]
    migration: dict[str, Any]
    metrics: dict[str, int]
    checks: list[StatusItem]
    operational: dict[str, Any] = Field(default_factory=dict)
    corpus: dict[str, Any] = Field(default_factory=dict)
    runtime_contract: dict[str, Any] = Field(default_factory=dict)


class ProjectItem(BaseModel):
    id: str
    name: str
    root_path: str | None = None
    status: str | None = None
    updated_at: str | None = None
    memory_count: int = 0
    task_count: int = 0
    run_count: int = 0


class ProjectListResponse(BaseModel):
    status: Literal["ok"]
    projects: list[ProjectItem]
    pagination: Pagination


class MemoryItem(BaseModel):
    memory_id: str
    project_id: str
    memory_type: str
    title: str
    content_summary: str
    source_type: str
    source_uri: str
    source_hash: str | None = None
    source_trust_level: str
    verification_status: str
    freshness_status: str
    importance: int
    retrieval_priority: int
    estimated_tokens: int
    score: float = 0
    retrieval_reason: str = "list"


class MemoryListResponse(BaseModel):
    status: Literal["ok"]
    memories: list[MemoryItem]
    pagination: Pagination
