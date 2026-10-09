# SPDX-License-Identifier: AGPL-3.0-only
"""Remote intent envelope: intent only, never reconstructed project context."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pados.execution.context import canonical_hash


class RemoteIntentType(StrEnum):
    START_TASK = "START_TASK"
    UPLOAD_AND_INGEST = "UPLOAD_AND_INGEST"
    QUERY_STATUS = "QUERY_STATUS"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


class RemotePriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"


MAX_HUMAN_MESSAGE_CHARS = 2_000
MAX_ATTACHMENT_REFS = 16
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_ATTACHMENT_REF = re.compile(r"staged:[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")

# Keys whose presence means a client tried to ship context instead of intent.
FORBIDDEN_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "context",
        "project_context",
        "sources",
        "semantic",
        "history",
        "chat_history",
        "messages",
        "conversation",
        "memory",
        "promoted_memory",
        "scope_version",
        "system_prompt",
        "provider_history",
    }
)

_TARGET_REQUIRED = {
    RemoteIntentType.APPROVE,
    RemoteIntentType.REJECT,
    RemoteIntentType.PAUSE,
    RemoteIntentType.RESUME,
    RemoteIntentType.CANCEL,
}


class RemoteIntentError(ValueError):
    """Envelope rejected; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_id(value: object, code: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RemoteIntentError(code)
    return value


def _optional_id(value: object, code: str) -> str | None:
    return None if value is None else _require_id(value, code)


@dataclass(frozen=True)
class RemoteIntentEnvelope:
    intent_id: str
    project_id: str
    actor_id: str
    session_id: str
    intent_type: RemoteIntentType
    human_message: str
    requested_capability: str | None = None
    attachment_refs: tuple[str, ...] = ()
    target_run_id: str | None = None
    approval_ref: str | None = None
    priority: RemotePriority = RemotePriority.NORMAL
    deadline: str | None = None
    # Single-use reference minted by a fresh, user-verified step-up (never a secret
    # the client can reuse): required for every APPROVE / REJECT decision.
    step_up_proof: str | None = None
    protocol_version: str = field(default="1.0", init=False)
    schema_version: str = field(default="pados.remote-intent.v1", init=False)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        _require_id(self.intent_id, "INTENT_ID_INVALID")
        _require_id(self.project_id, "PROJECT_ID_INVALID")
        _require_id(self.actor_id, "ACTOR_UNAUTHENTICATED")
        _require_id(self.session_id, "SESSION_UNAUTHENTICATED")
        if not isinstance(self.intent_type, RemoteIntentType):
            raise RemoteIntentError("INTENT_TYPE_UNKNOWN")
        if not isinstance(self.human_message, str) or len(self.human_message) > MAX_HUMAN_MESSAGE_CHARS:
            raise RemoteIntentError("HUMAN_MESSAGE_TOO_LONG")
        if self.intent_type is RemoteIntentType.START_TASK and not self.human_message.strip():
            raise RemoteIntentError("HUMAN_MESSAGE_REQUIRED")
        _optional_id(self.requested_capability, "CAPABILITY_INVALID")
        _optional_id(self.target_run_id, "TARGET_RUN_INVALID")
        _optional_id(self.approval_ref, "APPROVAL_REF_INVALID")
        _optional_id(self.step_up_proof, "STEP_UP_PROOF_INVALID")
        if self.deadline is not None:
            if not isinstance(self.deadline, str) or len(self.deadline) > 80:
                raise RemoteIntentError("DEADLINE_INVALID")
            try:
                deadline = datetime.fromisoformat(self.deadline)
            except (TypeError, ValueError) as exc:
                raise RemoteIntentError("DEADLINE_INVALID") from exc
            if deadline.tzinfo is None:
                raise RemoteIntentError("DEADLINE_TIMEZONE_REQUIRED")
        if len(self.attachment_refs) > MAX_ATTACHMENT_REFS:
            raise RemoteIntentError("TOO_MANY_ATTACHMENTS")
        for ref in self.attachment_refs:
            if not isinstance(ref, str) or not _ATTACHMENT_REF.fullmatch(ref):
                raise RemoteIntentError("ATTACHMENT_REF_NOT_STAGED")
        if self.intent_type in _TARGET_REQUIRED and self.target_run_id is None:
            raise RemoteIntentError("TARGET_RUN_REQUIRED")
        if self.intent_type is RemoteIntentType.UPLOAD_AND_INGEST and not self.attachment_refs:
            raise RemoteIntentError("ATTACHMENT_REQUIRED")
        if self.intent_type in {RemoteIntentType.APPROVE, RemoteIntentType.REJECT} and not self.approval_ref:
            raise RemoteIntentError("APPROVAL_REF_REQUIRED")
        if self.intent_type in {RemoteIntentType.APPROVE, RemoteIntentType.REJECT} and not self.step_up_proof:
            raise RemoteIntentError("APPROVAL_STEP_UP_PROOF_REQUIRED")
        if self.step_up_proof is not None and self.intent_type not in {
            RemoteIntentType.APPROVE, RemoteIntentType.REJECT
        }:
            raise RemoteIntentError("STEP_UP_PROOF_NOT_APPLICABLE")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "project_id": self.project_id,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "intent_type": self.intent_type.value,
            "human_message": self.human_message,
            "requested_capability": self.requested_capability,
            "attachment_refs": list(self.attachment_refs),
            "target_run_id": self.target_run_id,
            "approval_ref": self.approval_ref,
            "priority": self.priority.value,
            "deadline": self.deadline,
            "step_up_proof": self.step_up_proof,
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self.to_dict())

    @property
    def request_fingerprint(self) -> str:
        """Identity of the *request*: excludes server time and the per-connection session,
        so a reconnecting client retrying the same intent_id is recognised as a replay."""
        body = self.to_dict()
        body.pop("created_at")
        body.pop("session_id")
        return canonical_hash(body)

    @property
    def size_bytes(self) -> int:
        return len(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8"))


_CLIENT_KEYS = frozenset(
    {
        "intent_id",
        "project_id",
        "intent_type",
        "human_message",
        "requested_capability",
        "attachment_refs",
        "target_run_id",
        "approval_ref",
        "priority",
        "deadline",
        "step_up_proof",
    }
)


def parse_client_intent(
    payload: Mapping[str, Any], *, actor_id: str, session_id: str
) -> RemoteIntentEnvelope:
    """Build an envelope from an untrusted client payload.

    Actor and session come from the authenticated request, never the payload.
    Any context-shaped or unknown key is rejected (fail closed).
    """
    if not isinstance(payload, Mapping):
        raise RemoteIntentError("PAYLOAD_NOT_OBJECT")
    keys = set(payload)
    if keys & FORBIDDEN_CONTEXT_KEYS:
        raise RemoteIntentError("CLIENT_CONTEXT_FORBIDDEN")
    if keys & {"actor_id", "session_id"}:
        raise RemoteIntentError("CLIENT_IDENTITY_FORBIDDEN")
    if keys - _CLIENT_KEYS:
        raise RemoteIntentError("UNKNOWN_FIELD")
    try:
        intent_type = RemoteIntentType(str(payload.get("intent_type")))
    except ValueError as exc:
        raise RemoteIntentError("INTENT_TYPE_UNKNOWN") from exc
    try:
        priority = RemotePriority(payload.get("priority", "NORMAL"))
    except ValueError as exc:
        raise RemoteIntentError("PRIORITY_UNKNOWN") from exc
    refs = payload.get("attachment_refs", [])
    if not isinstance(refs, list):
        raise RemoteIntentError("ATTACHMENT_REFS_NOT_LIST")
    return RemoteIntentEnvelope(
        intent_id=payload.get("intent_id"),  # type: ignore[arg-type]
        project_id=payload.get("project_id"),  # type: ignore[arg-type]
        actor_id=actor_id,
        session_id=session_id,
        intent_type=intent_type,
        human_message=payload.get("human_message", ""),
        requested_capability=payload.get("requested_capability"),
        attachment_refs=tuple(refs),
        target_run_id=payload.get("target_run_id"),
        approval_ref=payload.get("approval_ref"),
        priority=priority,
        deadline=payload.get("deadline"),
        step_up_proof=payload.get("step_up_proof"),
    )
