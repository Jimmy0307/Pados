# SPDX-License-Identifier: AGPL-3.0-only
"""Devices and server-side sessions (design §3.3).

The browser holds only an opaque session id. The store keeps SHA-256 of it; the CSRF token is derived from it with
the server key and never stored. A session is valid while it is not revoked, its device is not revoked, it is within
its absolute lifetime and its idle timeout, and the control token configured now is the one configured when it was
created (rotating the control token with ``pados init --force`` ends every session).

Logs carry device ids and closed codes only: never a pairing code, a session id or a CSRF token.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.config import Settings
from pados.identity import pairing
from pados.identity.errors import (
    DEVICE_NOT_FOUND,
    SESSION_EXPIRED,
    SESSION_REQUIRED,
    SESSION_REVOKED,
    IdentityError,
)
from pados.identity.store import DeviceRecord, IdentityStore, SessionRecord, StoreState
from pados.identity.tokens import (
    DEFAULT_DEVICE_LABEL,
    control_binding,
    csrf_matches,
    csrf_token,
    digest,
    new_session_id,
    sanitize_label,
    session_id_shape_ok,
)
from pados.logging_config import get_logger

Clock = Callable[[], datetime]
LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)
MAX_ACTIVE_DEVICES = 16
MAX_ACTIVE_SESSIONS_PER_DEVICE = 4
ENDED_SESSION_RETENTION = timedelta(days=7)
REVOKED_DEVICE_RETENTION = timedelta(days=30)
_DEVICE_ID_PREFIX = "dev_"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _log() -> Any:
    return get_logger("pados.identity")


@dataclass(frozen=True)
class ActiveSession:
    session_id: str  # held in memory for this request only, to derive the CSRF token
    key: str
    record: SessionRecord
    device: DeviceRecord
    idle_expires_at: datetime


@dataclass(frozen=True)
class PairResult:
    session_id: str
    device: DeviceRecord
    session: SessionRecord
    csrf_token: str


def _revoke_session(record: SessionRecord, now: datetime) -> SessionRecord:
    return record if record.revoked_at is not None else record.model_copy(update={"revoked_at": now})


def _revoke_device(state: StoreState, device_id: str, now: datetime) -> StoreState:
    device = state.devices[device_id]
    devices = {**state.devices, device_id: device if device.revoked_at else device.model_copy(update={"revoked_at": now})}
    sessions = {key: _revoke_session(record, now) if record.device_id == device_id else record
                for key, record in state.sessions.items()}
    return state.model_copy(update={"devices": devices, "sessions": sessions})


def _prune_records(state: StoreState, now: datetime) -> StoreState:
    def ended(record: SessionRecord) -> bool:
        end = record.revoked_at if record.revoked_at is not None else record.expires_at
        return end + ENDED_SESSION_RETENTION <= now

    devices = {key: device for key, device in state.devices.items()
               if device.revoked_at is None or device.revoked_at + REVOKED_DEVICE_RETENTION > now}
    sessions = {key: record for key, record in state.sessions.items()
                if record.device_id in devices and not ended(record)}
    return state.model_copy(update={"devices": devices, "sessions": sessions})


def _enforce_limits(state: StoreState, new_device_id: str, now: datetime) -> StoreState:
    current = state
    active = sorted((d for d in current.devices.values() if d.revoked_at is None and d.device_id != new_device_id),
                    key=lambda d: d.paired_at)
    for device in active[: max(0, len(active) + 1 - MAX_ACTIVE_DEVICES)]:
        current = _revoke_device(current, device.device_id, now)
        _log().info("identity.device.revoked", device_id=device.device_id, reason="DEVICE_LIMIT")
    for device_id in {record.device_id for record in current.sessions.values()}:
        live = sorted(((k, r) for k, r in current.sessions.items()
                       if r.device_id == device_id and r.revoked_at is None and r.expires_at > now),
                      key=lambda item: item[1].created_at)
        excess = {k for k, _ in live[: max(0, len(live) - MAX_ACTIVE_SESSIONS_PER_DEVICE)]}
        if excess:
            current = current.model_copy(update={"sessions": {
                k: _revoke_session(r, now) if k in excess else r for k, r in current.sessions.items()}})
    return current


class IdentityService:
    def __init__(self, settings: Settings, *, store: IdentityStore | None = None, clock: Clock = utc_now) -> None:
        self.settings = settings
        self.store = store or IdentityStore(Path(settings.identity_store_dir).expanduser().resolve())
        self.clock = clock
        self.idle_timeout = timedelta(seconds=settings.identity_idle_timeout_seconds)
        self.lifetime = timedelta(seconds=settings.identity_session_ttl_seconds)

    @property
    def enabled(self) -> bool:
        return self.settings.identity_sessions_enabled

    def _binding(self, key: bytes) -> str:
        return control_binding(key, self.settings.control_api_token_hash)

    # --- pairing ----------------------------------------------------------------------------------------------
    def mint_pairing_code(self, label_hint: str | None) -> pairing.MintedCode:
        with self.store.locked():
            minted = pairing.mint(self.store.load(), self.clock(), sanitize_label(label_hint))
            self.store.save(minted.state)
        _log().info("identity.pairing_code.minted", expires_at=minted.expires_at.isoformat())
        return minted

    def pair(self, presented_code: str, label: str | None, presented_session_id: str | None) -> PairResult:
        with self.store.locked():
            now = self.clock()
            key = self.store.server_key()
            result = pairing.attempt(self.store.load(), now, presented_code)
            if result.error is not None or result.consumed is None:
                self.store.save(result.state)
                _log().warning("identity.pair.refused", error_code=result.error)
                raise IdentityError(result.error or "PAIRING_CODE_INVALID")
            state = self._revoke_presented(result.state, presented_session_id, now)
            device = DeviceRecord(
                device_id=_DEVICE_ID_PREFIX + secrets.token_hex(8),
                label=sanitize_label(label) or result.consumed.label_hint or DEFAULT_DEVICE_LABEL,
                paired_at=now, last_seen_at=now)
            session_id = new_session_id()
            session = SessionRecord(device_id=device.device_id, created_at=now, expires_at=now + self.lifetime,
                                    last_seen_at=now, binding=self._binding(key))
            state = state.model_copy(update={"devices": {**state.devices, device.device_id: device},
                                             "sessions": {**state.sessions, digest(session_id): session}})
            self.store.save(_prune_records(_enforce_limits(state, device.device_id, now), now))
        _log().info("identity.pair.succeeded", device_id=device.device_id)
        return PairResult(session_id=session_id, device=device, session=session,
                          csrf_token=csrf_token(key, session_id))

    def _revoke_presented(self, state: StoreState, presented: str | None, now: datetime) -> StoreState:
        """Fixation: a session presented to /pair is revoked when it exists; a new id is always issued."""
        if not presented or not session_id_shape_ok(presented):
            return state
        key = digest(presented)
        record = state.sessions.get(key)
        if record is None or record.revoked_at is not None:
            return state
        _log().info("identity.session.revoked", device_id=record.device_id, reason="REPAIRED")
        return state.model_copy(update={"sessions": {**state.sessions, key: _revoke_session(record, now)}})

    # --- sessions ---------------------------------------------------------------------------------------------
    def resolve(self, session_id: str | None) -> ActiveSession:
        """The active session for a presented id, or a closed refusal. Never writes."""
        if not session_id or not session_id_shape_ok(session_id):
            raise IdentityError(SESSION_REQUIRED)
        with self.store.locked():
            state = self.store.load()
            server_key = self.store.server_key()
        key = digest(session_id)
        record = state.sessions.get(key)
        if record is None:
            raise IdentityError(SESSION_REQUIRED)
        device = state.devices[record.device_id]
        now = self.clock()
        if record.revoked_at is not None or device.revoked_at is not None or record.binding != self._binding(server_key):
            raise IdentityError(SESSION_REVOKED)
        idle_expires_at = min(record.last_seen_at + self.idle_timeout, record.expires_at)
        if now >= record.expires_at or now >= idle_expires_at:
            raise IdentityError(SESSION_EXPIRED)
        return ActiveSession(session_id=session_id, key=key, record=record, device=device,
                             idle_expires_at=idle_expires_at)

    def csrf_for(self, active: ActiveSession) -> str:
        return csrf_token(self.store.server_key(), active.session_id)

    def csrf_ok(self, active: ActiveSession, presented: str | None) -> bool:
        return csrf_matches(self.store.server_key(), active.session_id, presented)

    def touch(self, active: ActiveSession) -> ActiveSession:
        """Slide the idle timeout; ``last_seen`` is written at most once per 60 seconds."""
        now = self.clock()
        if now - active.record.last_seen_at < LAST_SEEN_WRITE_INTERVAL:
            return active
        with self.store.locked():
            state = self.store.load()
            record = state.sessions.get(active.key)
            if record is None or record.revoked_at is not None:
                return active
            seen = record.model_copy(update={"last_seen_at": now})
            device = state.devices[record.device_id].model_copy(update={"last_seen_at": now})
            self.store.save(state.model_copy(update={"sessions": {**state.sessions, active.key: seen},
                                                     "devices": {**state.devices, device.device_id: device}}))
        return ActiveSession(session_id=active.session_id, key=active.key, record=seen, device=device,
                             idle_expires_at=min(now + self.idle_timeout, seen.expires_at))

    def logout(self, active: ActiveSession) -> None:
        with self.store.locked():
            state = self.store.load()
            record = state.sessions.get(active.key)
            if record is not None:
                self.store.save(state.model_copy(update={
                    "sessions": {**state.sessions, active.key: _revoke_session(record, self.clock())}}))
        _log().info("identity.session.revoked", device_id=active.device.device_id, reason="LOGOUT")

    # --- devices ----------------------------------------------------------------------------------------------
    def list_devices(self, current_device_id: str | None) -> list[dict[str, object]]:
        with self.store.locked():
            state = self.store.load()
        devices = sorted(state.devices.values(), key=lambda d: d.paired_at, reverse=True)
        return [{"device_id": d.device_id, "device_label": d.label, "paired_at": d.paired_at.isoformat(),
                 "last_seen_at": d.last_seen_at.isoformat(),
                 "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                 "current": d.device_id == current_device_id} for d in devices]

    def revoke_device(self, device_id: str) -> DeviceRecord:
        with self.store.locked():
            state = self.store.load()
            if device_id not in state.devices:
                raise IdentityError(DEVICE_NOT_FOUND)
            updated = _revoke_device(state, device_id, self.clock())
            self.store.save(updated)
        _log().info("identity.device.revoked", device_id=device_id, reason="OPERATOR")
        return updated.devices[device_id]
