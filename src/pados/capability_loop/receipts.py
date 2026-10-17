# SPDX-License-Identifier: AGPL-3.0-only
"""Operation receipts for external provider operations of the capability loop.

An ``OperationReceipt`` answers, for one external operation: what PADOS asked (request digest), which
provider handled it, which capability and runtime were involved, what evidence and limitations came back,
what failed, whether the result was accepted into the loop, and whether it changed capability state.

These receipts are loop evidence, not Core execution receipts: ``capability.receipts.StageReceipt`` and
``execution.receipts`` stay owned by the Core executor and are only *referenced* here (``core_receipt_ref``).
External runtime logs are never a substitute for this record. Receipts form a hash chain per store.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.capability_loop.contracts import CapabilityState, FailureCode
from pados.execution.context import canonical_hash

RECEIPT_SCHEMA = "pados.capability_loop.receipt.v1"
GENESIS = "0" * 64
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,95}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_SESSION_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# Optional fields added after the original receipt schema. They are excluded from the hash only when unused
# (None) so that adding them never changes the hash of a receipt written before they existed.
_ADDED_OPTIONAL_FIELDS = ("binding_digest", "session_ref")


def is_valid_receipt_id(value: object) -> bool:
    """True when ``value`` is a valid receipt identifier (project_id / provider_id contract)."""
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def is_valid_receipt_code(value: object) -> bool:
    """True when ``value`` is a valid receipt code (capability_id / policy_code contract)."""
    return isinstance(value, str) and bool(_CODE.fullmatch(value))


class OperationKind(StrEnum):
    DISCOVER = "DISCOVER"
    LEARN_CREATE_SESSION = "LEARN_CREATE_SESSION"
    LEARN_ATTACH_MATERIAL = "LEARN_ATTACH_MATERIAL"
    LEARN_SESSION_STATE = "LEARN_SESSION_STATE"
    LEARN_ASSESSMENT = "LEARN_ASSESSMENT"
    EXECUTE_EXISTING = "EXECUTE_EXISTING"
    ASSESS = "ASSESS"
    PROMOTE = "PROMOTE"


class OperationResult(StrEnum):
    REFUSED_BY_POLICY = "REFUSED_BY_POLICY"  # PADOS refused before contacting the provider
    PROVIDER_FAILED = "PROVIDER_FAILED"  # normalized provider failure
    RECORDED = "RECORDED"  # the provider answered; its output was normalized and recorded


class OperationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.capability_loop.receipt.v1"] = "pados.capability_loop.receipt.v1"
    sequence: int = Field(ge=1)
    previous_receipt_hash: str
    operation: OperationKind
    project_id: str
    request_digest: str  # canonical hash of what PADOS asked
    provider_id: str
    capability_id: str
    runtime_version: str | None = Field(default=None, max_length=40)
    policy_code: str  # the gate decision code (ALLOWED or the refusal code)
    policy_decision_hash: str | None = None
    result: OperationResult
    failure: FailureCode | None = None
    evidence_digests: tuple[str, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=16)
    accepted_into_loop: bool
    state_before: CapabilityState | None = None
    state_after: CapabilityState | None = None
    core_receipt_ref: str | None = None  # a Core StageReceipt / PlanReceipt hash, referenced only
    binding_digest: str | None = None  # digest of a session's governed binding; anchors it in this chain
    session_ref: str | None = None  # the external session a learning receipt is about (for binding lineage)
    recorded_at: str = Field(min_length=1, max_length=40)
    semantic_authority: Literal["NONE"] = "NONE"

    @field_validator("previous_receipt_hash", "request_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("RECEIPT_DIGEST_INVALID")
        return value

    @field_validator("policy_decision_hash", "core_receipt_ref", "binding_digest")
    @classmethod
    def _optional_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("RECEIPT_DIGEST_INVALID")
        return value

    @field_validator("evidence_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _DIGEST.fullmatch(item) for item in value):
            raise ValueError("RECEIPT_DIGEST_INVALID")
        return value

    @field_validator("project_id", "provider_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("RECEIPT_ID_INVALID")
        return value

    @field_validator("capability_id", "policy_code")
    @classmethod
    def _codes(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("RECEIPT_CODE_INVALID")
        return value

    @field_validator("limitations")
    @classmethod
    def _limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _CODE.fullmatch(item) for item in value):
            raise ValueError("RECEIPT_CODE_INVALID")
        return value

    @field_validator("session_ref")
    @classmethod
    def _session_ref(cls, value: str | None) -> str | None:
        if value is not None and not _SESSION_REF.fullmatch(value):
            raise ValueError("RECEIPT_SESSION_REF_INVALID")
        return value

    @property
    def state_changed(self) -> bool:
        return self.state_before != self.state_after

    @property
    def receipt_hash(self) -> str:
        # Drop ONLY the fields introduced after the original schema when they are unused (None), keeping every
        # original field (including its null). A receipt written before these fields existed therefore hashes
        # identically after upgrade, so a legacy chain still verifies; a used (non-null) value stays in the
        # hash and remains tamper-evident.
        dump = self.model_dump(mode="json")
        for added in _ADDED_OPTIONAL_FIELDS:
            if dump.get(added) is None:
                dump.pop(added, None)
        return canonical_hash(dump)


def verify_chain(receipts: tuple[OperationReceipt, ...] | list[OperationReceipt]) -> bool:
    """True when the receipts form an unbroken hash chain from the genesis value."""
    previous = GENESIS
    for index, receipt in enumerate(receipts, start=1):
        if receipt.sequence != index or receipt.previous_receipt_hash != previous:
            return False
        previous = receipt.receipt_hash
    return True
