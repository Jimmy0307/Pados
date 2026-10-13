# SPDX-License-Identifier: AGPL-3.0-only
"""Remote actor authentication.

The existing control token authenticates a trusted local caller but carries no
user identity. Remote operation needs an authenticated actor for every intent
and approval, so remote actors are registered with a hashed credential (same
hash + constant-time compare as ``pados.control.security``) and an explicit
project allowlist. Unknown actors, missing credentials and projects outside the
allowlist fail closed. A configuration that carries a plaintext credential field
(password, token, proof, ...) at any depth is refused, and actor rows have a
closed schema: unknown fields are refused rather than silently ignored.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pados.control.security import token_hash
from pados.lifecycle.minimization import prohibited_field
from pados.remote.presence import PresenceCredential, parse_presence_credential

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_HASH = re.compile(r"[0-9A-Fa-f]{64}")
# Remote-specific names for the raw access proof, on top of the shared prohibited fields.
_PLAINTEXT_PROOF_FIELDS = frozenset({"credential", "proof", "accessproof", "remoteproof", "presented"})
# Qualified forms (``admin_password``, ``push_token``) are plaintext too; ``*_ref``/``*_hash`` are not.
_PLAINTEXT_SUFFIXES = ("password", "passwd", "passphrase", "secret", "token", "apikey", "privatekey")


_ACTOR_FIELDS = frozenset(
    {"actor_id", "credential_hash", "projects", "may_approve", "may_control_jobs", "presence_credentials"}
)
# Mappings whose keys are data (project ids), not field names: only their values are inspected.
_DATA_KEYED = frozenset({"project_workspaces"})


def _is_plaintext_field(key: object) -> bool:
    normalized = re.sub(r"[\s_.-]+", "", str(key)).lower()
    return (prohibited_field(str(key)) is not None or normalized in _PLAINTEXT_PROOF_FIELDS
            or normalized.endswith(_PLAINTEXT_SUFFIXES))


def _assert_no_plaintext_credentials(value: Any, *, data_keyed: bool = False) -> None:
    """Refuse a plaintext credential field anywhere in the configuration, at any depth."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not data_keyed and _is_plaintext_field(key):
                raise ValueError("REMOTE_PLAINTEXT_CREDENTIAL_FORBIDDEN")
            _assert_no_plaintext_credentials(item, data_keyed=str(key) in _DATA_KEYED)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_plaintext_credentials(item)


def _actor_row(row: object) -> Mapping[str, Any]:
    """Actor rows have a closed schema: an unknown field is refused, never ignored."""
    if not isinstance(row, Mapping):
        raise ValueError("REMOTE_ACTOR_INVALID")
    if set(row) - _ACTOR_FIELDS:
        raise ValueError("REMOTE_ACTOR_FIELD_UNKNOWN")
    projects = row.get("projects", [])
    if not isinstance(projects, list) or not all(isinstance(p, str) for p in projects):
        raise ValueError("REMOTE_ACTOR_INVALID")
    if not all(isinstance(row.get(flag, False), bool) for flag in ("may_approve", "may_control_jobs")):
        raise ValueError("REMOTE_ACTOR_INVALID")
    presence = row.get("presence_credentials", [])
    if not isinstance(presence, list) or len(presence) > 8:
        raise ValueError("REMOTE_ACTOR_INVALID")
    return row


@dataclass(frozen=True)
class RemoteActor:
    actor_id: str
    credential_hash: str
    projects: frozenset[str]
    may_approve: bool = False
    may_control_jobs: bool = False
    # Public WebAuthn keys only; the private credential never leaves the authenticator.
    presence_credentials: tuple[PresenceCredential, ...] = ()


@dataclass(frozen=True)
class RemoteSession:
    actor: RemoteActor
    session_id: str
    # True only for a server-issued, persisted session (required for approval step-up).
    server_bound: bool = False


class RemoteActorRegistry:
    def __init__(self, actors: Iterable[RemoteActor]) -> None:
        self._actors: dict[str, RemoteActor] = {}
        for actor in actors:
            if not _ID.fullmatch(actor.actor_id) or not _HASH.fullmatch(actor.credential_hash):
                raise ValueError("REMOTE_ACTOR_INVALID")
            if actor.actor_id in self._actors:
                raise ValueError("REMOTE_ACTOR_DUPLICATE")
            ids = [item.credential_id for item in actor.presence_credentials]
            if len(ids) != len(set(ids)) or any(
                item.credential_id in ids for other in self._actors.values() for item in other.presence_credentials
            ):
                raise ValueError("REMOTE_PRESENCE_CREDENTIAL_DUPLICATE")
            self._actors[actor.actor_id] = actor

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RemoteActorRegistry:
        rows = data.get("actors")
        if not isinstance(rows, list):
            raise TypeError("REMOTE_ACTORS_REQUIRED")
        _assert_no_plaintext_credentials(data)
        rows = [_actor_row(row) for row in rows]
        return cls(
            RemoteActor(
                actor_id=str(row["actor_id"]),
                credential_hash=str(row["credential_hash"]).upper(),
                projects=frozenset(str(p) for p in row.get("projects", [])),
                may_approve=bool(row.get("may_approve", False)),
                may_control_jobs=bool(row.get("may_control_jobs", False)),
                presence_credentials=tuple(
                    parse_presence_credential(item) for item in row.get("presence_credentials", [])
                ),
            )
            for row in rows
        )

    @classmethod
    def from_file(cls, path: Path) -> RemoteActorRegistry:
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    def authenticate(self, actor_id: str | None, presented: str | None) -> RemoteSession:
        if not actor_id or not presented:
            raise PermissionError("REMOTE_AUTH_REQUIRED")
        actor = self._actors.get(actor_id)
        expected = actor.credential_hash if actor else "0" * 64
        matched = hmac.compare_digest(token_hash(presented), expected.upper())
        if actor is None or not matched:
            raise PermissionError("REMOTE_AUTH_INVALID")
        return RemoteSession(actor=actor, session_id=f"rs-{secrets.token_hex(12)}")

    def get(self, actor_id: str) -> RemoteActor | None:
        return self._actors.get(actor_id)


def authorize_project(session: RemoteSession, project_id: str) -> None:
    if project_id not in session.actor.projects:
        raise PermissionError("REMOTE_PROJECT_NOT_ALLOWED")


def may_approve(registry: RemoteActorRegistry, actor_id: str, scope_id: str) -> bool:
    actor = registry.get(actor_id)
    return bool(actor and actor.may_approve and scope_id in actor.projects)
