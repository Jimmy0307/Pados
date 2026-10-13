# SPDX-License-Identifier: AGPL-3.0-only
"""Machine operations and the privileged operation request a grant is bound to.

A ``GrantRequest`` is the complete operation request (machine, operation,
project, command and validated arguments). Its ``digest`` binds challenges,
operator proofs, grants, authorizations and the execution boundary to exactly
that request: any changed field produces a different digest.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.execution.context import canonical_hash
from pados.machines.errors import MachineError
from pados.machines.identity import MACHINE_ID_PATTERN
from pados.machines.profile import validated

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_ARGUMENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ARGUMENT_VALUE = re.compile(r"^[A-Za-z0-9_./:=+@,][A-Za-z0-9_./:=+@,-]{0,199}$")
MAX_ARGUMENTS = 16


class MachineOperation(StrEnum):
    CONNECTION_TEST = "CONNECTION_TEST"
    STATUS = "STATUS"
    TERMINAL = "TERMINAL"
    SHELL = "SHELL"
    ADMIN = "ADMIN"
    FILE_TRANSFER = "FILE_TRANSFER"


class OperationClass(StrEnum):
    ORDINARY = "ORDINARY"
    PRIVILEGED_OPERATION = "PRIVILEGED_OPERATION"


PRIVILEGED_OPERATIONS = frozenset({MachineOperation.TERMINAL, MachineOperation.SHELL, MachineOperation.ADMIN,
                                   MachineOperation.FILE_TRANSFER})
COMMAND_OPERATIONS = frozenset({MachineOperation.SHELL, MachineOperation.ADMIN})


def operation_class(operation: MachineOperation | str) -> OperationClass:
    try:
        resolved = MachineOperation(operation)
    except ValueError:
        raise MachineError("MACHINE_OPERATION_UNKNOWN") from None
    return OperationClass.PRIVILEGED_OPERATION if resolved in PRIVILEGED_OPERATIONS else OperationClass.ORDINARY


def normalized_arguments(arguments: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    supplied = dict(arguments or {})
    if len(supplied) > MAX_ARGUMENTS:
        raise MachineError("PRIVILEGED_COMMAND_ARGUMENT_INVALID")
    for name, value in supplied.items():
        if not isinstance(name, str) or not _ARGUMENT_NAME.fullmatch(name):
            raise MachineError("PRIVILEGED_COMMAND_ARGUMENT_INVALID")
        if not isinstance(value, str) or not _ARGUMENT_VALUE.fullmatch(value):
            raise MachineError("PRIVILEGED_COMMAND_ARGUMENT_INVALID")
    return tuple(sorted(supplied.items()))


class GrantRequest(BaseModel):
    """The full privileged operation request; arguments are digested, never recorded as text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_id: str = Field(pattern=MACHINE_ID_PATTERN)
    operation: MachineOperation
    project_id: str
    command_id: str | None = None
    arguments: tuple[tuple[str, str], ...] = ()

    @field_validator("project_id", "command_id")
    @classmethod
    def identifier(cls, value: str | None) -> str | None:
        if value is not None and not IDENTIFIER.fullmatch(value):
            raise MachineError("PRIVILEGED_REQUEST_IDENTIFIER_INVALID")
        return value

    @field_validator("arguments", mode="before")
    @classmethod
    def plain_arguments(cls, value: Any) -> tuple[tuple[str, str], ...]:
        if isinstance(value, Mapping):
            return normalized_arguments(value)
        if isinstance(value, (list, tuple)) and all(isinstance(i, (list, tuple)) and len(i) == 2 for i in value):
            pairs = {str(k): v for k, v in value}
            if len(pairs) != len(value):
                raise MachineError("PRIVILEGED_COMMAND_ARGUMENT_INVALID")
            return normalized_arguments(pairs)
        raise MachineError("PRIVILEGED_COMMAND_ARGUMENT_INVALID")

    @model_validator(mode="after")
    def scoped(self) -> GrantRequest:
        if self.operation not in PRIVILEGED_OPERATIONS:
            raise MachineError("MACHINE_OPERATION_NOT_PRIVILEGED")
        if (self.operation in COMMAND_OPERATIONS) != (self.command_id is not None):
            raise MachineError("PRIVILEGED_GRANT_COMMAND_SCOPE_INVALID")
        if self.command_id is None and self.arguments:
            raise MachineError("PRIVILEGED_COMMAND_ARGUMENTS_NOT_ALLOWED")
        return self

    @property
    def digest(self) -> str:
        return canonical_hash({"domain": "pados-privileged-request-v1", **self.model_dump(mode="json")})


def request_for(*, machine_id: str, operation: MachineOperation | str, project_id: str,
                command_id: str | None = None, arguments: Mapping[str, str] | None = None) -> GrantRequest:
    return validated(GrantRequest, {"machine_id": machine_id, "operation": operation, "project_id": project_id,
                                    "command_id": command_id, "arguments": dict(arguments or {})},
                     prefix="PRIVILEGED_REQUEST")
