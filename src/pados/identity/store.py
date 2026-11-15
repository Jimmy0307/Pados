# SPDX-License-Identifier: AGPL-3.0-only
"""The identity store: one owner-only JSON document of hashes plus a server key (design §3.3).

``sessions.json`` holds devices, session hashes, pairing-code hashes and the recent failed pair attempts. It never
holds a session id, a pairing code, a CSRF token or the control token. Every write is a fresh owner-only sibling file
that atomically replaces the document (never a write through an existing file), under one process-wide lock.

A document that cannot be read or does not validate fails closed: no session is valid and nothing is written until
the operator removes it (rollback, design §6). ``server.key`` (32 random bytes) keys the CSRF derivation and the
control-token binding; it is created once, atomically, and never replaced.

File-system protection (design §3.9, ``pados.identity.fs_security``): the directory is created owner-only from its
first moment (Windows: an explicit protected DACL for this process's user SID and SYSTEM, inherited by every file;
POSIX: 0700, files 0600). Every load, save and key read re-checks the directory and both files and fails closed with
``IDENTITY_STORE_INSECURE`` when any of them is readable or writable by someone else.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from pados.identity import fs_security
from pados.identity.errors import IDENTITY_STORE_CORRUPT, IDENTITY_STORE_INSECURE, IdentityError
from pados.logging_config import get_logger

STORE_SCHEMA: Final = "pados.identity.store.v1"
STORE_FILE = "sessions.json"
KEY_FILE = "server.key"
KEY_BYTES = 32
_MAX_STORE_BYTES = 4 * 1024 * 1024
DEVICE_ID_PATTERN = r"^dev_[0-9a-f]{16}$"
_HASH = re.compile(r"^[0-9a-f]{64}$")
# One process-wide lock: the store is a single document, so every read-modify-write is serialised.
_LOCK = threading.RLock()
_PHASE_TWO_MAPS = ("actor_sessions", "oidc_states")

DeviceId = Annotated[str, StringConstraints(pattern=DEVICE_ID_PATTERN)]


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeviceRecord(_Record):
    device_id: DeviceId
    label: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    paired_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None


class SessionRecord(_Record):
    device_id: DeviceId
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None
    # HMAC(server key, control-token hash at pairing): rotating the control token invalidates every session.
    binding: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PairingCodeRecord(_Record):
    created_at: datetime
    expires_at: datetime
    failed_attempts: int = Field(ge=0)
    label_hint: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None


ACTOR_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
ActorId = Annotated[str, StringConstraints(pattern=ACTOR_ID_PATTERN)]
ProviderId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]


class ActorSessionRecord(_Record):
    """An OIDC (phase 2) session. Kept apart from ``sessions``: the control plane never sees one (design §4)."""

    auth_method: Literal["oidc"] = "oidc"
    actor_id: ActorId
    provider_id: ProviderId
    issuer: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None
    binding: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class OidcStateRecord(_Record):
    """A pending OIDC authorization, keyed by SHA-256(state). PKCE verifier and nonce are derived, never stored."""

    provider_id: ProviderId
    browser_binding: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]  # SHA-256(pre-auth cookie)
    created_at: datetime
    expires_at: datetime


class StoreState(_Record):
    schema_version: Literal["pados.identity.store.v1"] = STORE_SCHEMA
    devices: dict[str, DeviceRecord] = Field(default_factory=dict)
    sessions: dict[str, SessionRecord] = Field(default_factory=dict)  # keyed by SHA-256(session id), hex
    pairing_codes: dict[str, PairingCodeRecord] = Field(default_factory=dict)  # keyed by SHA-256(code), hex
    pair_failures: tuple[datetime, ...] = ()
    # Phase 2 (optional, additive): OIDC actor sessions and pending authorizations, keyed by SHA-256 hex.
    actor_sessions: dict[str, ActorSessionRecord] = Field(default_factory=dict)
    oidc_states: dict[str, OidcStateRecord] = Field(default_factory=dict)


def _corrupt(reason: str) -> IdentityError:
    # The reason is a fixed word chosen here, never file content.
    get_logger("pados.identity").error("identity.store.corrupt", error_code=IDENTITY_STORE_CORRUPT, reason=reason)
    return IdentityError(IDENTITY_STORE_CORRUPT)


def _insecure(reason: str) -> IdentityError:
    # A fixed reason word only: never a path, a SID or an ACL dump.
    get_logger("pados.identity").error("identity.store.insecure", error_code=IDENTITY_STORE_INSECURE, reason=reason)
    return IdentityError(IDENTITY_STORE_INSECURE)


def _validated(state: StoreState) -> StoreState:
    if any(not _HASH.fullmatch(key) for key in (*state.sessions, *state.pairing_codes, *state.actor_sessions,
                                                 *state.oidc_states)):
        raise _corrupt("key_shape")
    if any(key != device.device_id for key, device in state.devices.items()):
        raise _corrupt("device_key")
    if any(record.device_id not in state.devices for record in state.sessions.values()):
        raise _corrupt("orphan_session")
    return state


def replace_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, owner-only from its first byte (``mkstemp`` is mode 0600)."""
    fd, staged = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


class IdentityStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / STORE_FILE
        self.key_path = directory / KEY_FILE

    def _ensure_directory(self) -> None:
        if not self.directory.exists():
            try:
                fs_security.make_private_directory(self.directory)
            except fs_security.StoreInsecure as exc:
                raise _insecure(exc.reason) from exc
        self._check_protection()

    def _check_protection(self) -> None:
        """Fail closed unless the directory and each existing store file are owner-only (design §3.9)."""
        for target in (self.directory, self.path, self.key_path):
            if not os.path.lexists(target):
                continue  # nothing there yet: nothing to protect
            try:
                reason = fs_security.problem(target)
            except fs_security.StoreInsecure as exc:
                reason = exc.reason
            except OSError:
                reason = "acl_unreadable"
            if reason is not None:
                raise _insecure(reason)

    def load(self) -> StoreState:
        with _LOCK:
            if self.path.is_symlink():
                raise _corrupt("symlink")
            self._check_protection()
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                return StoreState()
            except OSError as exc:
                raise _corrupt("unreadable") from exc
            if len(raw) > _MAX_STORE_BYTES:
                raise _corrupt("too_large")
            try:
                document = json.loads(raw.decode("utf-8"))
                return _validated(StoreState.model_validate(document))
            except (UnicodeDecodeError, ValueError, ValidationError) as exc:
                raise _corrupt("invalid_document") from exc

    def save(self, state: StoreState) -> None:
        with _LOCK:
            if self.path.is_symlink():
                raise _corrupt("symlink")
            self._ensure_directory()
            document = _validated(state).model_dump(mode="json")
            for optional in _PHASE_TWO_MAPS:  # absent while empty: a phase 1 document stays byte-identical
                if not document[optional]:
                    del document[optional]
            payload = json.dumps(document, sort_keys=True, indent=1)
            replace_private_bytes(self.path, payload.encode("utf-8"))

    @contextmanager
    def locked(self) -> Iterator[None]:
        with _LOCK:
            yield

    def server_key(self) -> bytes:
        with _LOCK:
            if self.key_path.is_symlink():
                raise _corrupt("key_symlink")
            self._check_protection()
            try:
                key = self.key_path.read_bytes()
            except FileNotFoundError:
                key = self._create_key()
            except OSError as exc:
                raise _corrupt("key_unreadable") from exc
            if len(key) != KEY_BYTES:
                raise _corrupt("key_length")
            return key

    def _create_key(self) -> bytes:
        """Create the key once: a fresh owner-only file hard-linked into place, so an existing key is never replaced."""
        self._ensure_directory()
        key = secrets.token_bytes(KEY_BYTES)
        fd, staged = tempfile.mkstemp(prefix=f".{KEY_FILE}.", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(staged, self.key_path)
            except FileExistsError:  # another process created it first: use that one
                return self.key_path.read_bytes()
            return key
        finally:
            Path(staged).unlink(missing_ok=True)
