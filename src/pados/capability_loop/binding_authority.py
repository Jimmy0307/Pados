# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-neutral contract for an authoritative external-session binding (P7.2 R4-F4 hardening).

An external learning/discovery runtime hands PADOS an opaque ``external_session_ref``. Authorization of every
follow-up on that session must rest on the ORIGINAL governed context the session was created under -- never on
a mutable projection, a caller-supplied project, or a provider-returned project. This module defines, inside
the capability_loop boundary, only the PROTOCOL and the immutable value types; the authoritative, externally
anchored implementation lives outside the boundary and is injected (dependency inversion), so capability_loop
never imports an anchor, a database, or any Core receipt module.

Security level of a conforming implementation: ``INDEPENDENTLY_ANCHORED_TAMPER_EVIDENT`` -- protected against a
binding-log writer who cannot also modify the independently stored anchor; NOT protected against an actor who
can modify both the log and the independent anchor (that would require an asymmetric signing trust root, a
separate future user decision). It is never described as cryptographically tamper-proof.

Creation is a two-phase commit with the operational evidence: ``stage`` records the binding as PENDING (never
resolvable), the caller durably records the creation receipt, and only then ``commit`` finalizes the binding
against that receipt's hash, making it resolvable. A failure or crash anywhere before ``commit`` leaves no
resolvable binding (fail closed); ``abort`` retires a pending binding. A committed binding can never be aborted,
re-staged or re-pointed, and an aborted identity can never be revived.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

AUTHORITY_SECURITY_LEVEL = "INDEPENDENTLY_ANCHORED_TAMPER_EVIDENT"
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,95}$")
_SESSION_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class BindingAuthorityError(RuntimeError):
    """Closed-code refusal from a session binding authority (every path fails closed)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GovernedSessionBinding(BaseModel):
    """The immutable governed context an external session was created under.

    Identity is ``(provider_id, external_session_ref)``. Once a session is bound, the governed context
    (project, work contract, classification, capability gap) can never be rebound to anything different; an
    identical rebind is idempotent, a differing one fails closed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    external_session_ref: str
    project_id: str
    work_contract_id: str
    classification: DataClassification
    capability_gap_ref: str
    created_at: str

    @field_validator("provider_id", "project_id", "work_contract_id", "capability_gap_ref")
    @classmethod
    def _ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("BINDING_ID_INVALID")
        return value

    @field_validator("external_session_ref")
    @classmethod
    def _session_ref(cls, value: str) -> str:
        if not _SESSION_REF.fullmatch(value):
            raise ValueError("BINDING_SESSION_REF_INVALID")
        return value

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("BINDING_TIMESTAMP_NOT_AWARE")
        return value

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider_id, self.external_session_ref

    @property
    def context_hash(self) -> str:
        """Hash of the governed context (identity + project/work/classification/gap); excludes created_at.

        Two binds with the same identity and context_hash are the same binding (idempotent); a differing
        context_hash for the same identity is a conflict.
        """
        return canonical_hash({
            "provider_id": self.provider_id, "external_session_ref": self.external_session_ref,
            "project_id": self.project_id, "work_contract_id": self.work_contract_id,
            "classification": self.classification.value, "capability_gap_ref": self.capability_gap_ref})


class BindingProof(BaseModel):
    """Opaque evidence that a binding is recorded and externally anchored. Carries no authority itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_hash: str
    sequence: int
    anchor_checkpoint_hash: str
    semantic_authority: Literal["NONE"] = "NONE"


class BindingState(StrEnum):
    """Lifecycle of one binding identity: STAGED -> COMMITTED | ABORTED (terminal). Only COMMITTED resolves."""

    STAGED = "STAGED"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


@runtime_checkable
class SessionBindingAuthority(Protocol):
    """Authoritative, externally anchored binding of an external session to its governed context.

    Every method fails closed: an unavailable/missing/mismatched/corrupt anchor or log raises
    ``BindingAuthorityError``. There is NO fallback to a mutable projection or caller-supplied ownership. A
    differing governed context for an identity already present raises ``BINDING_CONFLICT`` at every step.
    """

    def stage(self, binding: GovernedSessionBinding) -> BindingProof:
        """Record ``binding`` as pending (NOT resolvable). Idempotent while pending; ``BINDING_ALREADY_COMMITTED``
        or ``BINDING_ABORTED`` when the identity already reached a terminal state."""

    def commit(self, binding: GovernedSessionBinding, *, evidence_ref: str | None) -> BindingProof:
        """Finalize a pending binding against the durable creation evidence ``evidence_ref`` (a SHA-256), making
        it resolvable. Idempotent for the same evidence; ``BINDING_NOT_STAGED`` / ``BINDING_ABORTED`` /
        ``BINDING_ALREADY_COMMITTED`` otherwise."""

    def abort(self, binding: GovernedSessionBinding) -> None:
        """Retire a pending binding so it can never resolve. Idempotent once aborted; a committed binding can
        never be aborted (``BINDING_ALREADY_COMMITTED``)."""

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        """The authoritative governed context of a COMMITTED binding, integrity- and anchor-verified, or ``None``
        when nothing is committed for the session (unknown, pending or aborted). Any integrity/anchor failure
        raises rather than returning ``None``."""
