# SPDX-License-Identifier: AGPL-3.0-only
"""The actor-authorized surface (design §4). It is explicitly enumerated and minimal:

| Method and path | Authority |
| --- | --- |
| ``GET /identity/v1/me`` | a live actor session whose identity is still bound |
| ``POST /identity/v1/projects/{project_id}/work-requests`` | the same, plus ``contributor`` on that project, CSRF and Origin |

Nothing else accepts an actor session. Work-request creation delegates to the very function the control route
``POST /control/work-requests`` uses (passed in by ``pados.api.app``), so there is no second creation path and no
scheduler, router, CAS or ScopeVersion change. The actor's idempotency key is namespaced
(``actor:<actor_id>:<key>``) so one actor can never replay or read back another caller's request through a key.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pados.config import Settings
from pados.control.schemas import MAX_WORK_REQUEST_BODY_CHARS, WorkRequestCreate
from pados.identity import actor_http
from pados.identity import http as identity_http
from pados.identity.actor_sessions import ActorSessions
from pados.identity.actors import CREATE_WORK_REQUEST, PROJECT_ID_PATTERN, ROLES
from pados.worker.schemas import CapabilityId
from pados.identity.errors import ACTOR_ROLE_REQUIRED, IDENTITY_DISABLED
from pados.identity.sessions import IdentityService
from pados.logging_config import get_logger

PREFIX = "/identity/v1"
ACTOR_KEY_MAX = 48
WorkRequestCreator = Callable[[WorkRequestCreate], object]


class ActorWorkRequestCreate(BaseModel):
    """The body: no project (the path names it) and no actor (the session names it); unknown fields refused."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=MAX_WORK_REQUEST_BODY_CHARS)
    idempotency_key: str = Field(min_length=1, max_length=ACTOR_KEY_MAX, pattern=r"^[A-Za-z0-9._:-]+$")
    requested_capability: CapabilityId | None = None


def _service(request: Request) -> IdentityService:
    service = identity_http.service_for(request)
    if service is None or not service.enabled:
        identity_http.refuse(IDENTITY_DISABLED)
    return service


def mount_actor_routes(app: FastAPI, settings: Settings, *,
                       create_work_request: WorkRequestCreator | None = None) -> None:
    @app.get(f"{PREFIX}/me")
    def me(request: Request) -> JSONResponse:
        service = _service(request)
        actor = actor_http.authorize_actor(settings, request, service)
        record, binding = actor.session.record, actor.binding
        projects = [{"project_id": project, "roles": [role for role in ROLES if role in roles]}
                    for project, roles in sorted(binding.project_roles.items()) if roles]
        return identity_http.json_no_store({
            "actor_id": binding.actor_id, "display_name": binding.display_name, "auth_method": record.auth_method,
            "provider_id": record.provider_id, "issuer": record.issuer, "subject": record.subject,
            "projects": projects, "expires_at": record.expires_at.isoformat(),
            "idle_expires_at": actor.session.idle_expires_at.isoformat(),
            "csrf_token": ActorSessions(service).csrf_for(actor.session)})

    if create_work_request is None:
        return
    creator = create_work_request

    @app.post(f"{PREFIX}/projects/{{project_id}}/work-requests")
    def actor_work_request(project_id: Annotated[str, Path(pattern=PROJECT_ID_PATTERN)],
                           payload: ActorWorkRequestCreate, request: Request) -> object:
        service = _service(request)
        actor = actor_http.authorize_actor(settings, request, service)
        if not actor.binding.may(project_id, CREATE_WORK_REQUEST):
            get_logger("pados.identity").info("identity.actor.refused", actor_id=actor.binding.actor_id,
                                              error_code=ACTOR_ROLE_REQUIRED)
            identity_http.refuse(ACTOR_ROLE_REQUIRED)
        # At most 6 + 64 + 1 + 48 = 119 characters: within WorkRequestCreate's own bound of 120.
        namespaced = f"actor:{actor.binding.actor_id}:{payload.idempotency_key}"
        work_request = WorkRequestCreate(project_id=project_id, title=payload.title, body=payload.body,
                                         idempotency_key=namespaced,
                                         requested_capability=payload.requested_capability)
        result = creator(work_request)
        get_logger("pados.identity").info("identity.actor.work_request_created", actor_id=actor.binding.actor_id)
        return result
