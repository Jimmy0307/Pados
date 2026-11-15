# SPDX-License-Identifier: AGPL-3.0-only
"""OIDC actor sessions (design §4), built from the phase 1 session machinery.

Same opaque id (``secrets.token_urlsafe(32)``, only its SHA-256 stored), same derived CSRF token, same absolute and
idle lifetimes, same control-token binding (rotating the control token ends these sessions too), same store and
lock. The differences are deliberate and structural:

* records live in ``StoreState.actor_sessions``, never in ``sessions``, so ``IdentityService.resolve`` (the only
  lookup the control plane's ``control_auth`` performs) cannot find one;
* the cookie is a separate name (``pados_actor`` / ``__Host-pados_actor``) that the control plane never reads;
* a record carries ``auth_method: "oidc"``, the ``actor_id`` and the identity key ``(issuer, subject)``.

Logs carry actor ids and closed codes only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pados.identity.errors import SESSION_EXPIRED, SESSION_REQUIRED, SESSION_REVOKED, IdentityError
from pados.identity.sessions import ENDED_SESSION_RETENTION, LAST_SEEN_WRITE_INTERVAL, IdentityService
from pados.identity.store import ActorSessionRecord
from pados.identity.tokens import control_binding, csrf_matches, csrf_token, digest, new_session_id, \
    session_id_shape_ok
from pados.logging_config import get_logger

MAX_ACTIVE_SESSIONS_PER_ACTOR = 4
MAX_ACTIVE_ACTOR_SESSIONS = 64


def _log() -> Any:
    return get_logger("pados.identity")


@dataclass(frozen=True)
class ActiveActorSession:
    session_id: str  # held in memory for this request only, to derive the CSRF token
    key: str
    record: ActorSessionRecord
    idle_expires_at: datetime


@dataclass(frozen=True)
class IssuedActorSession:
    session_id: str
    record: ActorSessionRecord
    csrf_token: str


def _revoked(record: ActorSessionRecord, now: datetime) -> ActorSessionRecord:
    return record if record.revoked_at is not None else record.model_copy(update={"revoked_at": now})


def _live(record: ActorSessionRecord, now: datetime) -> bool:
    return record.revoked_at is None and record.expires_at > now


def _pruned_and_limited(sessions: dict[str, ActorSessionRecord], actor_id: str,
                        now: datetime) -> dict[str, ActorSessionRecord]:
    def ended(record: ActorSessionRecord) -> bool:
        end = record.revoked_at if record.revoked_at is not None else record.expires_at
        return end + ENDED_SESSION_RETENTION <= now

    kept = {key: record for key, record in sessions.items() if not ended(record)}
    for scope, limit in (((lambda r: r.actor_id == actor_id), MAX_ACTIVE_SESSIONS_PER_ACTOR),
                         ((lambda r: True), MAX_ACTIVE_ACTOR_SESSIONS)):
        live = sorted(((k, r) for k, r in kept.items() if scope(r) and _live(r, now)), key=lambda i: i[1].created_at)
        excess = {k for k, _ in live[: max(0, len(live) - limit)]}
        kept = {k: _revoked(r, now) if k in excess else r for k, r in kept.items()}
    return kept


class ActorSessions:
    def __init__(self, identity: IdentityService) -> None:
        self.identity = identity
        self.store = identity.store
        self.clock = identity.clock
        self.idle_timeout = identity.idle_timeout
        self.lifetime = identity.lifetime

    def _binding(self, key: bytes) -> str:
        return control_binding(key, self.identity.settings.control_api_token_hash)

    def issue(self, *, actor_id: str, provider_id: str, issuer: str, subject: str,
              presented_session_id: str | None) -> IssuedActorSession:
        """A new actor session. A presented actor session is revoked first (fixation); the id is always new."""
        with self.store.locked():
            now = self.clock()
            key = self.store.server_key()
            state = self.store.load()
            sessions = dict(state.actor_sessions)
            if presented_session_id and session_id_shape_ok(presented_session_id):
                old = sessions.get(digest(presented_session_id))
                if old is not None:
                    sessions[digest(presented_session_id)] = _revoked(old, now)
            session_id = new_session_id()
            record = ActorSessionRecord(actor_id=actor_id, provider_id=provider_id, issuer=issuer, subject=subject,
                                        created_at=now, expires_at=now + self.lifetime, last_seen_at=now,
                                        binding=self._binding(key))
            sessions[digest(session_id)] = record
            self.store.save(state.model_copy(update={"actor_sessions": _pruned_and_limited(sessions, actor_id, now)}))
        _log().info("identity.actor_session.issued", actor_id=actor_id, provider_id=provider_id)
        return IssuedActorSession(session_id=session_id, record=record, csrf_token=csrf_token(key, session_id))

    def resolve(self, session_id: str | None) -> ActiveActorSession:
        """The active actor session for a presented id, or a closed refusal. Never writes."""
        if not session_id or not session_id_shape_ok(session_id):
            raise IdentityError(SESSION_REQUIRED)
        with self.store.locked():
            state = self.store.load()
            server_key = self.store.server_key()
        key = digest(session_id)
        record = state.actor_sessions.get(key)
        if record is None:
            raise IdentityError(SESSION_REQUIRED)
        now = self.clock()
        if record.revoked_at is not None or record.binding != self._binding(server_key):
            raise IdentityError(SESSION_REVOKED)
        idle_expires_at = min(record.last_seen_at + self.idle_timeout, record.expires_at)
        if now >= record.expires_at or now >= idle_expires_at:
            raise IdentityError(SESSION_EXPIRED)
        return ActiveActorSession(session_id=session_id, key=key, record=record, idle_expires_at=idle_expires_at)

    def csrf_for(self, active: ActiveActorSession) -> str:
        return csrf_token(self.store.server_key(), active.session_id)

    def csrf_ok(self, active: ActiveActorSession, presented: str | None) -> bool:
        return csrf_matches(self.store.server_key(), active.session_id, presented)

    def touch(self, active: ActiveActorSession) -> ActiveActorSession:
        now = self.clock()
        if now - active.record.last_seen_at < LAST_SEEN_WRITE_INTERVAL:
            return active
        with self.store.locked():
            state = self.store.load()
            record = state.actor_sessions.get(active.key)
            if record is None or record.revoked_at is not None:
                return active
            seen = record.model_copy(update={"last_seen_at": now})
            self.store.save(state.model_copy(update={"actor_sessions": {**state.actor_sessions, active.key: seen}}))
        return ActiveActorSession(session_id=active.session_id, key=active.key, record=seen,
                                  idle_expires_at=min(now + self.idle_timeout, seen.expires_at))

    def logout(self, active: ActiveActorSession) -> None:
        with self.store.locked():
            state = self.store.load()
            record = state.actor_sessions.get(active.key)
            if record is not None:
                updated = {**state.actor_sessions, active.key: _revoked(record, self.clock())}
                self.store.save(state.model_copy(update={"actor_sessions": updated}))
        _log().info("identity.actor_session.revoked", actor_id=active.record.actor_id, reason="LOGOUT")
