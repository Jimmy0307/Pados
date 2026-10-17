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
"""
from __future__ import annotations

import re
from datetime import datetime
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


@runtime_checkable
class SessionBindingAuthority(Protocol):
    """Authoritative, externally anchored binding of an external session to its governed context.

    Every method fails closed: an unavailable/missing/mismatched/corrupt anchor or log raises
    ``BindingAuthorityError``. There is NO fallback to a mutable projection or caller-supplied ownership.
    """

    def bind(self, binding: GovernedSessionBinding) -> BindingProof:
        """Immutably record ``binding``. Idempotent for an identical binding; fails closed (no rebind) when
        the same identity is already bound to a different governed context."""

    def resolve(self, provider_id: str, external_session_ref: str) -> GovernedSessionBinding | None:
        """The authoritative governed context for an external session, integrity- and anchor-verified, or
        ``None`` when nothing is bound. Any integrity/anchor failure raises rather than returning ``None``."""
