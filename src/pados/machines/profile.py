# SPDX-License-Identifier: AGPL-3.0-only
"""Machine profile: one physical machine PADOS may connect to.

A profile holds location (host, port), the login account, a credential
*reference* and the pinned host public key that defines the machine's
identity. There is no password, passphrase or private-key field anywhere:
``auth_ref`` is a ``secretref://`` reference (SSH key), an ``ssh-agent://<name>``
reference or an OS credential store reference ``oscred://<store>/<name>``. A
password may authenticate once during bootstrap, but PADOS never stores it; the
completed bootstrap is recorded only as ``PASSWORD_BOOTSTRAP_COMPLETED`` with the
key-based ``key_auth_method`` it installed. Every raw input passes the shared
``assert_minimized`` guard against the explicit profile schema before
validation, and every persisted record passes it again.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pados.enterprise.secrets import is_secret_ref
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.machines.errors import MachineError
from pados.machines.identity import FINGERPRINT_PATTERN, MACHINE_ID_PATTERN, HostKey
from pados.machines.targets import validate_machine_host, validate_port, validate_username

COMPUTE_NODE_ID_PATTERN = r"^lcn-[a-f0-9]{20}$"
_SSH_AGENT_REF = re.compile(r"^ssh-agent://[a-z0-9][a-z0-9_-]{0,63}$")
_OS_CREDENTIAL_REF = re.compile(r"^oscred://[a-z0-9][a-z0-9_-]{0,31}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HERDR_REF = re.compile(r"^[a-z][a-z0-9+.-]{0,15}:[A-Za-z0-9_./+-]{1,200}$")
# Schema-level shapes (also published in docs/machines/MACHINE_PROFILE_SCHEMA.json); the
# validators below add the per-method scheme rule, canonical forms and identity checks.
AUTH_REF_PATTERN = (r"^(secretref://[a-z0-9_-]+/[A-Za-z0-9_./-]+|ssh-agent://[a-z0-9][a-z0-9_-]{0,63}"
                    r"|oscred://[a-z0-9][a-z0-9_-]{0,31}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$")
HOST_PUBLIC_KEY_PATTERN = r"^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa) [A-Za-z0-9+/]+={0,2}$"
USERNAME_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$"
_DISPLAY_NAME = re.compile(r"^[^\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]{1,80}$")


class AuthMethod(StrEnum):
    SSH_KEY = "SSH_KEY"
    SSH_AGENT = "SSH_AGENT"
    OS_CREDENTIAL_REF = "OS_CREDENTIAL_REF"
    PASSWORD_BOOTSTRAP_COMPLETED = "PASSWORD_BOOTSTRAP_COMPLETED"


KEY_AUTH_METHODS = frozenset({AuthMethod.SSH_KEY, AuthMethod.SSH_AGENT})


class Platform(StrEnum):
    MACOS = "MACOS"
    LINUX = "LINUX"
    WINDOWS = "WINDOWS"


class MachineRole(StrEnum):
    HOME_BASE = "HOME_BASE"
    LOCAL_COMPUTE_NODE = "LOCAL_COMPUTE_NODE"
    HERDR_MACHINE = "HERDR_MACHINE"
    SANDBOX_HOST = "SANDBOX_HOST"


class ConnectionState(StrEnum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAUTHORIZED = "UNAUTHORIZED"
    HERDR_UNAVAILABLE = "HERDR_UNAVAILABLE"


class IdentityAlert(StrEnum):
    """Sticky until a human re-pins or acknowledges; forces ``UNAUTHORIZED``."""

    HOST_KEY_MISMATCH = "HOST_KEY_MISMATCH"


def auth_ref_scheme(ref: str) -> str:
    return ref.split("://", 1)[0] if "://" in ref else "unknown"


def validate_auth_ref(method: AuthMethod, ref: object) -> str:
    """``ref`` must be a reference of the scheme ``method`` uses; never key or password material."""
    if not isinstance(ref, str) or not 1 <= len(ref) <= 256:
        raise MachineError("AUTH_REF_INVALID")
    if method is AuthMethod.SSH_KEY:
        valid = is_secret_ref(ref)
    elif method is AuthMethod.SSH_AGENT:
        valid = _SSH_AGENT_REF.fullmatch(ref) is not None
    elif method is AuthMethod.OS_CREDENTIAL_REF:
        valid = _OS_CREDENTIAL_REF.fullmatch(ref) is not None
    else:
        raise MachineError("AUTH_METHOD_REQUIRES_KEY_AUTH_METHOD")
    if not valid:
        raise MachineError("AUTH_REF_MUST_BE_REFERENCE")
    return ref


def _jsonable(value: Any) -> Any:
    """JSON form of raw input so the minimization guard sees exactly what would be persisted."""
    def default(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if hasattr(item, "isoformat"):
            return item.isoformat()
        raise DataMinimizationViolation("UNSUPPORTED_VALUE_TYPE", "")

    return json.loads(json.dumps(value, default=default))


def guard_input(data: Any, allowed_keys: frozenset[str], *, max_string: int = 2048) -> Any:
    """Refuse raw input carrying prohibited fields or credential-shaped values before validation."""
    if isinstance(data, Mapping):
        assert_minimized(_jsonable(dict(data)), allowed_keys=allowed_keys, max_string=max_string)
    return data


class HerdrMetadata(BaseModel):
    """Optional Herdr link: an enabled flag and an opaque socket/ref string, nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    ref: str | None = Field(default=None, json_schema_extra={"pattern": _HERDR_REF.pattern, "maxLength": 217})

    @model_validator(mode="before")
    @classmethod
    def minimized(cls, data: Any) -> Any:
        return guard_input(data, frozenset({"enabled", "ref"}), max_string=256)

    @model_validator(mode="after")
    def ref_shape(self) -> HerdrMetadata:
        if self.ref is not None and not _HERDR_REF.fullmatch(self.ref):
            raise MachineError("HERDR_REF_INVALID")
        if self.enabled and self.ref is None:
            raise MachineError("HERDR_REF_REQUIRED")
        return self

    @property
    def ref_scheme(self) -> str | None:
        return self.ref.split(":", 1)[0] if self.ref else None


class MachineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_id: str = Field(pattern=MACHINE_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    hostname_confirmed: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=USERNAME_PATTERN)
    auth_method: AuthMethod
    auth_ref: str = Field(pattern=AUTH_REF_PATTERN, max_length=256)
    key_auth_method: AuthMethod | None = None
    platform: Platform
    roles: tuple[MachineRole, ...] = Field(default=(), max_length=len(MachineRole))
    host_public_key: str = Field(pattern=HOST_PUBLIC_KEY_PATTERN, max_length=2400)
    host_key_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    enrolled_at: AwareDatetime
    connection_state: ConnectionState
    state_observed_at: AwareDatetime
    last_seen: AwareDatetime | None = None
    identity_alert: IdentityAlert | None = None
    herdr: HerdrMetadata = HerdrMetadata()
    compute_node_id: str | None = Field(default=None, pattern=COMPUTE_NODE_ID_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def minimized(cls, data: Any) -> Any:
        return guard_input(data, PROFILE_KEYS)

    @field_validator("display_name")
    @classmethod
    def plain_display_name(cls, value: str) -> str:
        if value != value.strip() or not _DISPLAY_NAME.fullmatch(value):
            raise MachineError("DISPLAY_NAME_INVALID")
        return value

    @field_validator("port")
    @classmethod
    def valid_port(cls, value: int) -> int:
        return validate_port(value)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        return validate_username(value)

    @model_validator(mode="after")
    def consistent(self) -> MachineProfile:
        if validate_machine_host(self.host, hostname_confirmed=self.hostname_confirmed) != self.host:
            raise MachineError("HOST_NOT_CANONICAL")
        key = HostKey.from_openssh(self.host_public_key)
        if key.openssh() != self.host_public_key:
            raise MachineError("HOST_KEY_NOT_CANONICAL")
        if key.machine_id != self.machine_id or key.fingerprint != self.host_key_fingerprint:
            raise MachineError("MACHINE_IDENTITY_INCONSISTENT")
        if self.auth_method is AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED:
            if self.key_auth_method not in KEY_AUTH_METHODS:
                raise MachineError("BOOTSTRAP_REQUIRES_KEY_AUTH_METHOD")
            validate_auth_ref(AuthMethod(str(self.key_auth_method)), self.auth_ref)
        else:
            if self.key_auth_method is not None:
                raise MachineError("KEY_AUTH_METHOD_ONLY_AFTER_BOOTSTRAP")
            validate_auth_ref(self.auth_method, self.auth_ref)
        if len(set(self.roles)) != len(self.roles):
            raise MachineError("MACHINE_ROLE_DUPLICATE")
        if self.herdr.enabled and MachineRole.HERDR_MACHINE not in self.roles:
            raise MachineError("HERDR_REQUIRES_HERDR_MACHINE_ROLE")
        if self.compute_node_id is not None and MachineRole.LOCAL_COMPUTE_NODE not in self.roles:
            raise MachineError("COMPUTE_NODE_LINK_REQUIRES_LOCAL_COMPUTE_NODE_ROLE")
        if self.identity_alert is not None and self.connection_state is not ConnectionState.UNAUTHORIZED:
            raise MachineError("IDENTITY_ALERT_REQUIRES_UNAUTHORIZED")
        return self

    @property
    def effective_auth_method(self) -> AuthMethod:
        return self.key_auth_method or self.auth_method

    @property
    def host_key(self) -> HostKey:
        return HostKey.from_openssh(self.host_public_key)


PROFILE_KEYS = frozenset(MachineProfile.model_fields)
_Model = TypeVar("_Model", bound=BaseModel)


def validated(model: type[_Model], data: Mapping[str, Any], *, prefix: str) -> _Model:
    """Validate ``data`` into ``model``; failures become a code-only ``MachineError``.

    Pydantic's own error text echoes input values, so it never leaves this
    function, and the raw input is deleted before the code is raised so no
    traceback frame keeps it.
    """
    code = f"{prefix}_INVALID"
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        code = _error_code(exc, model, prefix)
    finally:
        # Every exit path, including an exception a validator raises without pydantic wrapping it.
        del data
    raise MachineError(code)


def _error_code(exc: ValidationError, model: type[BaseModel], prefix: str) -> str:
    for error in exc.errors(include_url=False, include_input=False):
        inner = (error.get("ctx") or {}).get("error")
        if isinstance(inner, MachineError):
            return inner.code
        if isinstance(inner, DataMinimizationViolation):
            return f"{prefix}_{inner.code}"
        if error.get("type") == "extra_forbidden":
            return f"{prefix}_FIELD_NOT_IN_SCHEMA"
        location = error.get("loc") or ()
        if location and isinstance(location[0], str) and location[0] in model.model_fields:
            return f"{prefix}_{location[0].upper()}_INVALID"
    return f"{prefix}_INVALID"


def profile_record(profile: MachineProfile) -> dict[str, Any]:
    """The persisted form of ``profile``, checked by the shared minimization guard."""
    record = profile.model_dump(mode="json")
    assert_minimized(record, allowed_keys=PROFILE_KEYS)
    return record
