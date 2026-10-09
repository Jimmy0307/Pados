# SPDX-License-Identifier: AGPL-3.0-only
"""Safe one-time password bootstrap (``ssh-copy-id`` style).

PASSWORD MAY AUTHENTICATE does not mean PADOS STORES PASSWORD. ``bootstrap``
uses an operator-typed password exactly once, through an injected
``BootstrapTransport``, to install a PUBLIC key on the machine; it then
verifies that key-based login works and returns a profile whose
``auth_method`` is ``PASSWORD_BOOTSTRAP_COMPLETED`` with the key-based
``key_auth_method`` (``SSH_KEY`` or ``SSH_AGENT``) and its ``auth_ref``.

* The password arrives only as a ``SecretValue`` (its repr is redacted) and is
  handed to the transport only after the machine presented the pinned,
  operator-confirmed host key, so it is never sent to a spoofed host.
* It is used inside one function (``_install_with_password``) that returns a
  code and deletes its reference in ``finally``; ``bootstrap`` deletes its own
  reference the same way and raises its code-only ``MachineError`` from a fresh
  frame (no chaining, no transport text, no frame local holding the password).
  This module logs codes only.
* Claim, precisely: the password is not persisted, and it is not in PADOS
  files, logs, receipts or PADOS exception text or traceback locals. Python
  cannot wipe immutable string memory, the caller's own references are the
  caller's, and a transport is bound only by its documented contract
  (``BootstrapTransport``). See ``docs/machines/MACHINE_CONNECTION_V1.md``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.enterprise.secrets import SecretValue
from pados.machines.errors import MachineError
from pados.machines.identity import HostKey
from pados.machines.inventory import utc_now
from pados.machines.profile import (
    COMPUTE_NODE_ID_PATTERN,
    KEY_AUTH_METHODS,
    USERNAME_PATTERN,
    AuthMethod,
    ConnectionState,
    HerdrMetadata,
    MachineProfile,
    MachineRole,
    Platform,
    guard_input,
    validate_auth_ref,
    validated,
)
from pados.machines.targets import validate_machine_host

logger = logging.getLogger("pados.machines")
_KEY_COMMENT = re.compile(r"^[A-Za-z0-9_.@+-]{1,64}$")


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    username: str


class BootstrapTransport(Protocol):
    """Password-capable transport (operator-supplied; not shipped in V1).

    Secret-handling contract an implementation must honour: call
    ``one_time_password.reveal()`` only at the moment of authentication and keep
    the revealed string in no attribute, cache, log, file, receipt or exception
    message; never retain the ``SecretValue`` after ``install_public_key``
    returns; never send it before the pinned host key was verified; never
    serialize the transport object. PADOS discards any exception the transport
    raises and keeps only a code, but cannot police the transport's own memory.
    """

    def presented_host_key(self, target: SshTarget) -> HostKey | None:
        """The host key ``target`` presents (no authentication attempted)."""

    def install_public_key(self, target: SshTarget, pinned: HostKey, one_time_password: SecretValue,
                           public_key: str) -> None:
        """Authenticate once with the password against ``pinned`` and append ``public_key``."""

    def verify_key_login(self, target: SshTarget, pinned: HostKey, key_auth_method: AuthMethod,
                         auth_ref: str) -> bool:
        """True when key-based login with ``auth_ref`` succeeds against ``pinned``."""


def canonical_public_key(value: object) -> str:
    """An OpenSSH PUBLIC key line (``type base64 [comment]``); private key material is refused."""
    if not isinstance(value, str) or "PRIVATE" in value.upper() or "\n" in value.strip():
        raise MachineError("PUBLIC_KEY_INVALID")
    parts = value.strip().split()
    if len(parts) not in {2, 3}:
        raise MachineError("PUBLIC_KEY_INVALID")
    try:
        key = HostKey.from_openssh(f"{parts[0]} {parts[1]}")
    except MachineError:
        raise MachineError("PUBLIC_KEY_INVALID") from None
    if len(parts) == 3 and not _KEY_COMMENT.fullmatch(parts[2]):
        raise MachineError("PUBLIC_KEY_COMMENT_INVALID")
    return " ".join([key.openssh(), *parts[2:]])


class BootstrapDraft(BaseModel):
    """Everything a bootstrapped profile needs except the observation; holds no password."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    hostname_confirmed: bool = False
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=USERNAME_PATTERN)
    platform: Platform
    roles: tuple[MachineRole, ...] = ()
    host_public_key: str
    key_auth_method: AuthMethod
    auth_ref: str
    public_key: str
    herdr: HerdrMetadata = HerdrMetadata()
    compute_node_id: str | None = Field(default=None, pattern=COMPUTE_NODE_ID_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def minimized(cls, data: Any) -> Any:
        return guard_input(data, DRAFT_KEYS)

    @field_validator("public_key")
    @classmethod
    def public_only(cls, value: str) -> str:
        return canonical_public_key(value)

    @model_validator(mode="after")
    def key_based(self) -> BootstrapDraft:
        if self.key_auth_method not in KEY_AUTH_METHODS:
            raise MachineError("BOOTSTRAP_REQUIRES_KEY_AUTH_METHOD")
        validate_auth_ref(self.key_auth_method, self.auth_ref)
        validate_machine_host(self.host, hostname_confirmed=self.hostname_confirmed)
        HostKey.from_openssh(self.host_public_key)
        return self


DRAFT_KEYS = frozenset(BootstrapDraft.model_fields)


def bootstrap(draft: BootstrapDraft, one_time_password: SecretValue, transport: BootstrapTransport, *,
              now: datetime | None = None) -> MachineProfile:
    """Install the draft's public key with the one-time password and return the key-based profile."""
    try:
        code = _install_with_password(draft, one_time_password, transport)
    finally:
        del one_time_password
    # Raised from this frame, outside any except block: no chained exception, no frame holding the password.
    if code is not None:
        machine_id = _machine_id_of(draft)
        logger.warning("machine.bootstrap_failed machine_id=%s code=%s", machine_id, code)
        raise MachineError(code, machine_id=machine_id)
    pinned = HostKey.from_openssh(draft.host_public_key)
    host = validate_machine_host(draft.host, hostname_confirmed=draft.hostname_confirmed)
    observed = now or utc_now()
    profile = validated(MachineProfile, {
        "machine_id": pinned.machine_id, "display_name": draft.display_name, "host": host,
        "hostname_confirmed": draft.hostname_confirmed, "port": draft.port, "username": draft.username,
        "auth_method": AuthMethod.PASSWORD_BOOTSTRAP_COMPLETED, "auth_ref": draft.auth_ref,
        "key_auth_method": draft.key_auth_method, "platform": draft.platform, "roles": draft.roles,
        "host_public_key": pinned.openssh(), "host_key_fingerprint": pinned.fingerprint, "enrolled_at": observed,
        "connection_state": ConnectionState.ONLINE, "state_observed_at": observed, "last_seen": observed,
        "herdr": draft.herdr, "compute_node_id": draft.compute_node_id,
    }, prefix="MACHINE_PROFILE")
    logger.info("machine.bootstrap_completed machine_id=%s key_auth_method=%s", profile.machine_id,
                draft.key_auth_method.value)
    return profile


def _machine_id_of(draft: object) -> str | None:
    try:
        return HostKey.from_openssh(getattr(draft, "host_public_key", None)).machine_id
    except MachineError:
        return None


def _install_with_password(draft: object, one_time_password: object, transport: BootstrapTransport) -> str | None:
    """The only place the password is used. Returns a code; never raises an exception that saw it.

    The password local is deleted in ``finally`` so no traceback frame (even one
    from an unexpected ``BaseException``) retains it.
    """
    try:
        if not isinstance(draft, BootstrapDraft):
            return "BOOTSTRAP_DRAFT_INVALID"
        if not isinstance(one_time_password, SecretValue):
            return "BOOTSTRAP_PASSWORD_MUST_BE_SECRET_VALUE"
        try:
            pinned = HostKey.from_openssh(draft.host_public_key)
            host = validate_machine_host(draft.host, hostname_confirmed=draft.hostname_confirmed)
        except MachineError as exc:
            return exc.code
        target = SshTarget(host=host, port=draft.port, username=draft.username)
        code = _check_presented_key(transport, target, pinned)
        if code is not None:
            return code
        try:
            transport.install_public_key(target, pinned, one_time_password, draft.public_key)
        except Exception:  # noqa: BLE001 - transport text may echo the password, keep the code only
            return "BOOTSTRAP_KEY_INSTALL_FAILED"
        try:
            verified = transport.verify_key_login(target, pinned, draft.key_auth_method, draft.auth_ref) is True
        except Exception:  # noqa: BLE001 - mapped to a local code
            verified = False
        return None if verified else "BOOTSTRAP_KEY_LOGIN_FAILED"
    finally:
        del one_time_password


def _check_presented_key(transport: BootstrapTransport, target: SshTarget, pinned: HostKey) -> str | None:
    """``None`` when the machine presents the pinned key; the password is never sent otherwise."""
    try:
        presented = transport.presented_host_key(target)
    except Exception:  # noqa: BLE001 - mapped to a local code
        return "MACHINE_UNREACHABLE"
    if not isinstance(presented, HostKey):
        return "MACHINE_UNREACHABLE"
    return None if presented == pinned else "HOST_KEY_MISMATCH"
