# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pydantic import BaseModel, Field

from pados.worker.schemas import RouteRequest

MAX_WORK_REQUEST_BODY_CHARS = 65_536


class MessageCreate(BaseModel):
    project_id: str
    body: str = Field(min_length=1, max_length=8000)
    author: str = Field(default="human", max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=120)


class WorkRequestCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=MAX_WORK_REQUEST_BODY_CHARS)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ApprovalCreate(BaseModel):
    project_id: str
    proposal_summary: str = Field(min_length=1, max_length=4000)
    proposal_hash: str = Field(pattern=r"^[0-9A-Fa-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=120)


class ApprovalDecision(BaseModel):
    version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)


class ProjectionRenderRequest(BaseModel):
    vault_root: str
    projects: list[dict[str, object]]
    existing_markdown: str | None = None
    existing_canvas: dict[str, object] | None = None


class WorkRequestRoute(RouteRequest):
    pass
