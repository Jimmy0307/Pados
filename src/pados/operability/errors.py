# SPDX-License-Identifier: AGPL-3.0-only
"""The versioned PADOS error envelope (``pados.error.v1``) and its explicit HTTP mapping (OP-001).

Every externally visible failure carries a stable UPPER_SNAKE_CASE code, a category, whether a retry is safe, a safe
message, the request context and whether a write occurred. ``NOT_FOUND`` is an addition to the standard's recommended
categories, kept separate so an unknown id is never reported as a validation or authorization failure.

Retry is never offered for validation, authorization, authority or conflict failures; for the rest it is offered only
when the raiser declares it, so an unknown failure is never made retryable by default.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ERROR_SCHEMA_VERSION = "pados.error.v1"
_CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")


class ErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    AUTHORITY = "AUTHORITY"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DEPENDENCY = "DEPENDENCY"
    TIMEOUT = "TIMEOUT"
    RESOURCE = "RESOURCE"
    INTERNAL = "INTERNAL"


class WriteOutcome(StrEnum):
    """Whether the refused or failed operation changed durable state: YES, NO, or UNKNOWN when it cannot be told."""

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


# Default status per category. An existing route may answer more narrowly (401 vs 403, 413/422 vs 400); the envelope
# then keeps that route's status and maps the category from it (``category_for_status``).
HTTP_STATUS_BY_CATEGORY: dict[ErrorCategory, int] = {
    ErrorCategory.VALIDATION: 400,
    ErrorCategory.AUTHORIZATION: 403,
    ErrorCategory.AUTHORITY: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.CONFLICT: 409,
    ErrorCategory.DEPENDENCY: 503,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.RESOURCE: 429,
    ErrorCategory.INTERNAL: 500,
}
NEVER_RETRYABLE = frozenset({ErrorCategory.VALIDATION, ErrorCategory.AUTHORIZATION, ErrorCategory.AUTHORITY,
                             ErrorCategory.NOT_FOUND, ErrorCategory.CONFLICT})


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.error.v1"] = "pados.error.v1"
    code: str = Field(min_length=1, max_length=80)
    category: ErrorCategory
    message: str = Field(max_length=300)
    retryable: bool
    request_id: str
    correlation_id: str
    write_occurred: WriteOutcome
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _stable_code(cls, value: str) -> str:
        if not _CODE.fullmatch(value):
            raise ValueError("ERROR_CODE_NOT_UPPER_SNAKE_CASE")
        return value

    @model_validator(mode="after")
    def _no_retry_for_refusals(self) -> ErrorEnvelope:
        if self.retryable and self.category in NEVER_RETRYABLE:
            raise ValueError("ERROR_CATEGORY_NEVER_RETRYABLE")
        return self


class PadosError(Exception):
    """A boundary failure that declares its own code, category, retry safety and write outcome."""

    def __init__(self, code: str, category: ErrorCategory, *, message: str, retryable: bool = False,
                 write_occurred: WriteOutcome = WriteOutcome.UNKNOWN, details: dict[str, Any] | None = None,
                 status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.message = message
        self.retryable = retryable
        self.write_occurred = write_occurred
        self.details = dict(details or {})
        self.status_code = status_code or HTTP_STATUS_BY_CATEGORY[category]


def is_error_code(value: str) -> bool:
    return bool(_CODE.fullmatch(value)) and len(value) <= 80


def category_for_status(status: int) -> ErrorCategory:
    if status == 404:
        return ErrorCategory.NOT_FOUND
    if status in (401, 403):
        return ErrorCategory.AUTHORIZATION
    if status == 409:
        return ErrorCategory.CONFLICT
    if status in (413, 429, 507):
        return ErrorCategory.RESOURCE
    if status in (502, 503):
        return ErrorCategory.DEPENDENCY
    if status == 504:
        return ErrorCategory.TIMEOUT
    if status >= 500:
        return ErrorCategory.INTERNAL
    return ErrorCategory.VALIDATION


GENERIC_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE", 422: "VALIDATION_FAILED", 429: "RATE_LIMITED", 500: "INTERNAL_ERROR",
    502: "DEPENDENCY_FAILED", 503: "DEPENDENCY_UNAVAILABLE", 504: "TIMEOUT", 507: "INSUFFICIENT_STORAGE",
}
SAFE_MESSAGE_BY_CATEGORY: dict[ErrorCategory, str] = {
    ErrorCategory.VALIDATION: "The request was rejected as invalid.",
    ErrorCategory.AUTHORIZATION: "The request is not authorized.",
    ErrorCategory.AUTHORITY: "The request was refused by a PADOS authority boundary.",
    ErrorCategory.NOT_FOUND: "The requested resource was not found.",
    ErrorCategory.CONFLICT: "The request conflicts with the current state.",
    ErrorCategory.DEPENDENCY: "A required dependency is unavailable.",
    ErrorCategory.TIMEOUT: "The operation exceeded its deadline.",
    ErrorCategory.RESOURCE: "A resource limit was reached.",
    ErrorCategory.INTERNAL: "An internal error occurred.",
}
