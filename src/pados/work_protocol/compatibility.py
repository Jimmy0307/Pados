# SPDX-License-Identifier: AGPL-3.0-only
"""Strict protocol version dispatch and deterministic response normalization."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pados.execution.context import canonical_hash
from pados.work_protocol.models import (
    AuthorizationMetadata,
    CapabilityRequest,
    CapabilityResult,
    ExecutionReceiptRef,
    Provenance,
    ResourceKind,
)

T = TypeVar("T", bound=BaseModel)
MAX_PROTOCOL_BYTES = 256_000


class ProtocolCompatibilityError(ValueError):
    """Stable, provider-neutral protocol rejection code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_protocol(payload: Mapping[str, Any], model: type[T]) -> T:
    if not isinstance(payload, Mapping):
        raise ProtocolCompatibilityError("PROTOCOL_DOCUMENT_NOT_OBJECT")
    try:
        size = len(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ProtocolCompatibilityError("PROTOCOL_DOCUMENT_NOT_JSON") from exc
    if size > MAX_PROTOCOL_BYTES:
        raise ProtocolCompatibilityError("PROTOCOL_DOCUMENT_TOO_LARGE")
    version = payload.get("protocol_version")
    if not isinstance(version, str):
        raise ProtocolCompatibilityError("PROTOCOL_VERSION_REQUIRED")
    try:
        return model.model_validate(dict(payload))
    except ValidationError as exc:
        messages = tuple(str(item.get("msg", "")) for item in exc.errors())
        for code in (
            "PROTOCOL_MAJOR_UNSUPPORTED",
            "PROTOCOL_MINOR_UNSUPPORTED",
            "PROTOCOL_SCHEMA_UNSUPPORTED",
        ):
            if any(code in message for message in messages):
                raise ProtocolCompatibilityError(code) from exc
        raise


def normalize_capability_result(
    response: Mapping[str, Any],
    *,
    request: CapabilityRequest,
    result_id: str,
    resource_id: str,
    resource_kind: ResourceKind,
    provider: str | None,
    provenance: Provenance,
    authorization: AuthorizationMetadata,
    execution_receipt_ref: ExecutionReceiptRef,
) -> CapabilityResult:
    """Project a bounded worker response into PADOS result fields.

    Task/goal/capability and authority metadata always come from PADOS inputs;
    provider-supplied identity, scope, classification, authorization and receipt
    fields are ignored and cannot override them.
    """
    if not isinstance(response, Mapping):
        raise ProtocolCompatibilityError("PROVIDER_RESPONSE_NOT_OBJECT")
    allowed = {
        "completed_goals",
        "partial_goals",
        "unresolved_goals",
        "blocked_goals",
        "contributions",
        "artifacts",
        "evidence",
        "metrics",
        "confidence",
        "failure_reason",
        "policy_refusal",
    }
    unknown = set(response) - allowed
    if unknown:
        raise ProtocolCompatibilityError("PROVIDER_RESPONSE_FIELD_UNSUPPORTED")
    raw = {
        "protocol_version": "1.0",
        "schema_version": CapabilityResult.SCHEMA,
        "result_id": result_id,
        "task_id": request.task_id,
        "goal_id": request.goal_id,
        "capability": request.capability,
        "resource_id": resource_id,
        "resource_kind": resource_kind.value,
        "provider": provider,
        "partial_goals": (),
        "unresolved_goals": (),
        "blocked_goals": (),
        "rejected_goals": (),
        "contributions": (),
        "artifacts": (),
        "evidence": (),
        "metrics": {},
        "confidence": None,
        "failure_reason": None,
        "policy_refusal": None,
        "execution_receipt_ref": execution_receipt_ref.model_dump(mode="json"),
        "authorization": authorization.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        **dict(response),
    }
    try:
        result = CapabilityResult.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolCompatibilityError("PROVIDER_RESPONSE_INVALID") from exc
    if result.authorization != authorization:
        raise ProtocolCompatibilityError("AUTHORIZATION_METADATA_CHANGED")
    if result.execution_receipt_ref != execution_receipt_ref:
        raise ProtocolCompatibilityError("EXECUTION_RECEIPT_CHANGED")
    if result.provenance != provenance:
        raise ProtocolCompatibilityError("PROVENANCE_CHANGED")
    if result.task_id != request.task_id or result.goal_id != request.goal_id:
        raise ProtocolCompatibilityError("TASK_BINDING_CHANGED")
    return result


def provider_response_digest(response: Mapping[str, Any]) -> str:
    """Hash a validated provider projection without storing raw provider payloads."""
    return canonical_hash(dict(response))
