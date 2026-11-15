# SPDX-License-Identifier: AGPL-3.0-only
"""The OIDC relying party: Authorization Code + PKCE S256 + ``state`` + ``nonce`` (design §4).

``start``: a fresh 256-bit ``state`` and a fresh pre-auth browser secret. The store keeps SHA-256(state) with the
provider id, SHA-256(pre-auth secret) and a 10-minute expiry; at most ``MAX_PENDING_STATES`` are kept (oldest
dropped). The PKCE verifier and the nonce are derived from the state with the server key (HMAC), so neither is
stored. The pre-auth secret travels only in an HttpOnly ``SameSite=Lax`` cookie.

``callback``: the state must exist, belong to this provider, be unexpired and be presented by the same browser (the
pre-auth cookie digest matches, constant time); only then is it consumed (single use). A state presented by another
browser is refused without being consumed. Then the code is exchanged with the derived verifier and the client
credential read from the environment variable the configuration names, the ID token is validated, and the identity
key ``(issuer, subject)`` is looked up in the actor directory. No binding: no session (``SSO_IDENTITY_NOT_BOUND``).

Logs carry provider ids, actor ids, closed codes and fixed reason words; never a code, state, nonce, verifier, token
or client credential. An unbound identity is logged with its issuer and subject so the owner can bind it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from pados.config import Settings
from pados.identity.actor_sessions import ActorSessions, IssuedActorSession
from pados.identity.actors import ActorBinding, ActorDirectory, load_actor_directory
from pados.identity.errors import (
    OIDC_CLIENT_CREDENTIAL_UNAVAILABLE,
    OIDC_PROVIDER_ERROR,
    OIDC_PROVIDER_NOT_CONFIGURED,
    OIDC_STATE_INVALID,
    SSO_IDENTITY_NOT_BOUND,
    IdentityError,
)
from pados.identity.oidc.config import OidcConfig, ProviderConfig, load_oidc_config
from pados.identity.oidc.errors import OidcFailure
from pados.identity.oidc.idtoken import validate_id_token
from pados.identity.oidc.jws import b64url_encode
from pados.identity.oidc.provider import ProviderClient, build_http_client
from pados.identity.sessions import IdentityService
from pados.identity.store import OidcStateRecord, StoreState
from pados.identity.tokens import digest
from pados.logging_config import get_logger

STATE_TTL = timedelta(minutes=10)
MAX_PENDING_STATES = 32
_STATE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_CODE = re.compile(r"^[\x21-\x7E]{1,4096}$")


def _log() -> Any:
    return get_logger("pados.identity")


def _derived(server_key: bytes, label: bytes, state: str) -> str:
    return b64url_encode(hmac.new(server_key, label + state.encode("ascii"), hashlib.sha256).digest())


def pkce_verifier(server_key: bytes, state: str) -> str:
    return _derived(server_key, b"oidc-pkce:", state)  # 43 characters of the RFC 7636 unreserved set


def pkce_challenge(verifier: str) -> str:
    return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())


def oidc_nonce(server_key: bytes, state: str) -> str:
    return _derived(server_key, b"oidc-nonce:", state)


@dataclass(frozen=True)
class StartedAuthorization:
    authorization_url: str
    preauth_value: str  # for the pre-auth cookie only


def _pending_states(state: StoreState, now: Any) -> dict[str, OidcStateRecord]:
    live = {k: r for k, r in state.oidc_states.items() if r.expires_at > now}
    ordered = sorted(live.items(), key=lambda item: item[1].created_at)
    return dict(ordered[max(0, len(ordered) - (MAX_PENDING_STATES - 1)):])


class OidcRuntime:
    """Configuration, actor directory, HTTP client and per-provider caches; mounted once per app."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.environ = environ if environ is not None else os.environ
        self.http = build_http_client(transport)
        self._clients: dict[str, ProviderClient] = {}
        self.config: OidcConfig | None = None
        self.config_error: IdentityError | None = None
        self.actors = ActorDirectory()
        self.actors_error: IdentityError | None = None
        try:
            self.config = load_oidc_config(settings.identity_oidc_config, settings.identity_origins)
        except OidcFailure as exc:
            self.config_error = exc
            _log().error("identity.oidc.config_refused", error_code=exc.code, reason=exc.reason)
        try:
            self.actors = load_actor_directory(settings.identity_actors_config)
        except OidcFailure as exc:
            self.actors_error = exc
            _log().error("identity.actors.config_refused", error_code=exc.code, reason=exc.reason)

    # --- configuration ----------------------------------------------------------------------------------------
    def public_providers(self) -> list[dict[str, str]]:
        """What ``GET /session`` shows: nothing unless both configurations loaded cleanly."""
        if self.config is None or self.config_error is not None or self.actors_error is not None:
            return []
        return [{"id": p.id, "kind": p.kind, "display_name": p.label,
                 "start_url": f"/identity/v1/oidc/{p.id}/start"} for p in self.config.providers]

    def provider(self, provider_id: str) -> ProviderConfig:
        if self.config_error is not None:
            raise self.config_error
        found = self.config.provider(provider_id) if self.config is not None else None
        if found is None:
            raise IdentityError(OIDC_PROVIDER_NOT_CONFIGURED)
        return found

    def post_login_redirect(self) -> str:
        return self.config.post_login_redirect if self.config is not None else "/"

    def binding_for(self, actor_id: str, provider_id: str, issuer: str, subject: str) -> ActorBinding:
        """The binding behind a live actor session: its provider still configured, its identity still bound."""
        if self.actors_error is not None:
            raise self.actors_error
        self.provider(provider_id)
        binding = self.actors.bound(issuer, subject)
        if binding is None or binding.actor_id != actor_id:
            raise IdentityError(SSO_IDENTITY_NOT_BOUND)
        return binding

    def _client(self, identity: IdentityService, provider: ProviderConfig) -> ProviderClient:
        client = self._clients.get(provider.id)
        if client is None or client.provider != provider:
            client = ProviderClient(provider, self.http, identity.clock)
            self._clients[provider.id] = client
        return client

    # --- start ------------------------------------------------------------------------------------------------
    def start(self, identity: IdentityService, provider_id: str) -> StartedAuthorization:
        provider = self.provider(provider_id)
        if self.actors_error is not None:
            raise self.actors_error
        discovery = self._client(identity, provider).discovery()
        state_value = secrets.token_urlsafe(32)
        preauth = secrets.token_urlsafe(32)
        with identity.store.locked():
            now = identity.clock()
            server_key = identity.store.server_key()
            stored = identity.store.load()
            pending = _pending_states(stored, now)
            pending[digest(state_value)] = OidcStateRecord(provider_id=provider.id, browser_binding=digest(preauth),
                                                           created_at=now, expires_at=now + STATE_TTL)
            identity.store.save(stored.model_copy(update={"oidc_states": pending}))
        verifier = pkce_verifier(server_key, state_value)
        query = urlencode({
            "response_type": "code", "client_id": provider.client_id, "redirect_uri": provider.redirect_uri,
            "scope": " ".join(provider.scopes), "state": state_value, "nonce": oidc_nonce(server_key, state_value),
            "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256"})
        separator = "&" if "?" in discovery.authorization_endpoint else "?"
        _log().info("identity.oidc.started", provider_id=provider.id)
        return StartedAuthorization(authorization_url=f"{discovery.authorization_endpoint}{separator}{query}",
                                    preauth_value=preauth)

    # --- callback ---------------------------------------------------------------------------------------------
    def _consume_state(self, identity: IdentityService, provider: ProviderConfig, state_value: object,
                       preauth: str | None) -> str:
        """The state, consumed: only by the browser that started it, for this provider, before it expires."""
        if not isinstance(state_value, str) or not _STATE.fullmatch(state_value):
            raise OidcFailure(OIDC_STATE_INVALID, "state_shape")
        key = digest(state_value)
        with identity.store.locked():
            now = identity.clock()
            stored = identity.store.load()
            record = stored.oidc_states.get(key)
            if record is None:
                raise OidcFailure(OIDC_STATE_INVALID, "state_unknown")
            if record.provider_id != provider.id:
                raise OidcFailure(OIDC_STATE_INVALID, "state_provider")
            presented = digest(preauth) if preauth else ""
            if not hmac.compare_digest(presented.encode("ascii"), record.browser_binding.encode("ascii")):
                raise OidcFailure(OIDC_STATE_INVALID, "state_other_browser")
            remaining = {k: r for k, r in stored.oidc_states.items() if k != key}
            identity.store.save(stored.model_copy(update={"oidc_states": remaining}))
            if now >= record.expires_at:
                raise OidcFailure(OIDC_STATE_INVALID, "state_expired")
        return state_value

    def callback(self, identity: IdentityService, provider_id: str, params: Mapping[str, str],
                 preauth: str | None, presented_actor_session: str | None) -> IssuedActorSession:
        provider = self.provider(provider_id)
        try:
            return self._callback(identity, provider, params, preauth, presented_actor_session)
        except IdentityError as exc:
            reason = exc.reason if isinstance(exc, OidcFailure) else "refused"
            _log().warning("identity.oidc.refused", provider_id=provider.id, error_code=exc.code, reason=reason)
            raise

    def _callback(self, identity: IdentityService, provider: ProviderConfig, params: Mapping[str, str],
                  preauth: str | None, presented_actor_session: str | None) -> IssuedActorSession:
        if self.actors_error is not None:
            raise self.actors_error
        state_value = self._consume_state(identity, provider, params.get("state"), preauth)
        if "error" in params:
            raise OidcFailure(OIDC_PROVIDER_ERROR, "authorization_error")
        code = params.get("code")
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise OidcFailure(OIDC_PROVIDER_ERROR, "code_missing")
        client_credential = self.environ.get(provider.client_secret_env, "")
        if not client_credential:
            raise OidcFailure(OIDC_CLIENT_CREDENTIAL_UNAVAILABLE, "environment_unset")
        server_key = identity.store.server_key()
        client = self._client(identity, provider)
        compact = client.exchange_code(code, pkce_verifier(server_key, state_value), client_credential)
        verified = validate_id_token(compact, provider=provider, key_for=client.signing_key,
                                     expected_nonce=oidc_nonce(server_key, state_value), now=identity.clock())
        binding = self.actors.bound(verified.issuer, verified.subject)
        if binding is None:
            _log().info("identity.oidc.identity_not_bound", provider_id=provider.id, issuer=verified.issuer,
                        subject=verified.subject)
            raise OidcFailure(SSO_IDENTITY_NOT_BOUND, "not_bound")
        return ActorSessions(identity).issue(actor_id=binding.actor_id, provider_id=provider.id,
                                             issuer=verified.issuer, subject=verified.subject,
                                             presented_session_id=presented_actor_session)
