# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS actors and project roles (design §4; ``PADOS_IDENTITY_ACTORS_CONFIG``, schema ``pados.identity.actors.v1``).

An ``ActorBinding`` maps an OIDC identity key ``(issuer, subject)`` to a PADOS ``actor_id`` and grants per-project
roles from a closed set. Email is never an identifier: two identities that share an email but not a subject are two
identities, and an identity without a binding is granted nothing.

Roles grant only what is enumerated in ``ROLE_GRANTS``. ``contributor`` may create a work request on its project
through ``POST /identity/v1/projects/{project_id}/work-requests``. ``viewer`` and ``approver`` are reported by
``GET /identity/v1/me`` and grant no action in phase 2: approvals and result acceptance stay on their existing paths
(result acceptance always needs the remote actor's WebAuthn step-up). No role grants administration, device
management, pairing-code minting or control-token authority.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from pados.identity.errors import ACTORS_CONFIG_INVALID, IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN
from pados.identity.oidc.errors import OidcFailure
from pados.identity.plaintext import contains_plaintext_credential
from pados.identity.store import ActorId

SCHEMA: Final = "pados.identity.actors.v1"
MAX_CONFIG_BYTES = 256 * 1024
PROJECT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

Role = Literal["viewer", "contributor", "approver"]
ROLES: Final = ("viewer", "contributor", "approver")
# The only actions a role unlocks in phase 2. Anything not listed here is granted by no role.
CREATE_WORK_REQUEST: Final = "work_request.create"
ROLE_GRANTS: Final[dict[str, frozenset[str]]] = {
    "viewer": frozenset(),
    "contributor": frozenset({CREATE_WORK_REQUEST}),
    "approver": frozenset(),
}

ProjectId = Annotated[str, StringConstraints(pattern=PROJECT_ID_PATTERN)]


class ActorBindingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actor_id: ActorId
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    issuer: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    project_roles: dict[ProjectId, tuple[Role, ...]] = Field(default_factory=dict, max_length=256)

    @model_validator(mode="after")
    def _roles_unique(self) -> ActorBindingConfig:
        if any(len(set(roles)) != len(roles) for roles in self.project_roles.values()):
            raise ValueError("duplicate role")
        return self


class ActorsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.identity.actors.v1"] = Field(alias="schema")
    actors: tuple[ActorBindingConfig, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def _unique(self) -> ActorsConfig:
        if len({a.actor_id for a in self.actors}) != len(self.actors):
            raise ValueError("actor ids must be unique")
        if len({(a.issuer, a.subject) for a in self.actors}) != len(self.actors):
            raise ValueError("an identity may be bound to one actor only")
        return self


@dataclass(frozen=True)
class ActorBinding:
    actor_id: str
    display_name: str | None
    issuer: str
    subject: str
    project_roles: dict[str, frozenset[str]]

    def roles_for(self, project_id: str) -> frozenset[str]:
        return self.project_roles.get(project_id, frozenset())

    def may(self, project_id: str, action: str) -> bool:
        return any(action in ROLE_GRANTS[role] for role in self.roles_for(project_id))


class ActorDirectory:
    """Exact ``(issuer, subject)`` lookup. An empty directory binds nobody."""

    def __init__(self, bindings: tuple[ActorBinding, ...] = ()) -> None:
        self._by_identity = {(b.issuer, b.subject): b for b in bindings}

    def bound(self, issuer: str, subject: str) -> ActorBinding | None:
        return self._by_identity.get((issuer, subject))

    def __len__(self) -> int:
        return len(self._by_identity)


def load_actor_directory(path: str) -> ActorDirectory:
    """Unset is an empty directory. Any problem is one closed code with a fixed reason word (never file content)."""
    if not path.strip():
        return ActorDirectory()
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise OidcFailure(ACTORS_CONFIG_INVALID, "unreadable") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise OidcFailure(ACTORS_CONFIG_INVALID, "too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OidcFailure(ACTORS_CONFIG_INVALID, "not_json") from exc
    if contains_plaintext_credential(document, data_keyed=frozenset({"project_roles"})):
        raise OidcFailure(IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN, "plaintext_credential")
    try:
        config = ActorsConfig.model_validate(document)
    except ValidationError as exc:
        raise OidcFailure(ACTORS_CONFIG_INVALID, "schema") from exc
    return ActorDirectory(tuple(
        ActorBinding(actor_id=a.actor_id, display_name=a.display_name, issuer=a.issuer, subject=a.subject,
                     project_roles={project: frozenset(roles) for project, roles in a.project_roles.items()})
        for a in config.actors))
