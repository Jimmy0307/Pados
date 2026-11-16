# SPDX-License-Identifier: AGPL-3.0-only
"""Talking to one OIDC provider: discovery, the JWKS cache and the authorization-code exchange.

Every URL is HTTPS (the discovery URL, and the authorization, token and JWKS endpoints it names); redirects are not
followed; responses are size-bounded JSON objects. The discovery document must state the configured issuer exactly.
The JWKS is cached for ``JWKS_TTL``; a ``kid`` that is not in the cache triggers exactly one refetch, then a refusal.
Provider response bodies are never logged or returned: refusals carry a closed code and a fixed reason word.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from pados.identity.errors import OIDC_PROVIDER_ERROR, OIDC_PROVIDER_UNAVAILABLE, OIDC_TOKEN_INVALID
from pados.identity.oidc.config import ProviderConfig, is_https_url
from pados.identity.oidc.errors import OidcFailure
from pados.identity.oidc.jws import RsaPublicKey, rsa_key_from_jwk

Clock = Callable[[], datetime]
DISCOVERY_TTL = timedelta(hours=1)
# After a failed discovery, /start answers OIDC_PROVIDER_UNAVAILABLE without an outbound request for this long.
DISCOVERY_FAILURE_COOLDOWN = timedelta(seconds=30)
JWKS_TTL = timedelta(hours=1)
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JWKS_KEYS = 64
HTTP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Discovery:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    fetched_at: datetime


@dataclass(frozen=True)
class KeySet:
    keys: dict[str, RsaPublicKey]
    fetched_at: datetime


def _unavailable(reason: str) -> OidcFailure:
    return OidcFailure(OIDC_PROVIDER_UNAVAILABLE, reason)


def _json_response(response: httpx.Response, reason: str) -> dict[str, Any]:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise _unavailable(f"{reason}_too_large")
    try:
        document = response.json()
    except ValueError as exc:
        raise _unavailable(f"{reason}_not_json") from exc
    if not isinstance(document, dict):
        raise _unavailable(f"{reason}_not_object")
    return document


def _supports(document: dict[str, Any], field: str, value: str) -> bool:
    """An advertised list must contain ``value``; an absent list is not a refusal (Entra omits some)."""
    advertised = document.get(field)
    return advertised is None or (isinstance(advertised, list) and value in advertised)


def _validated_discovery(provider: ProviderConfig, document: dict[str, Any], now: datetime) -> Discovery:
    if document.get("issuer") != provider.discovery_issuer:
        raise _unavailable("discovery_issuer")
    endpoints = [document.get(name) for name in ("authorization_endpoint", "token_endpoint", "jwks_uri")]
    if not all(isinstance(url, str) and len(url) <= 2048 and is_https_url(url) for url in endpoints):
        raise _unavailable("discovery_endpoint")
    if not (_supports(document, "response_types_supported", "code")
            and _supports(document, "id_token_signing_alg_values_supported", "RS256")
            and _supports(document, "code_challenge_methods_supported", "S256")):
        raise _unavailable("discovery_capability")
    authorization, token_endpoint, jwks = (str(url) for url in endpoints)
    return Discovery(authorization_endpoint=authorization, token_endpoint=token_endpoint, jwks_uri=jwks,
                     fetched_at=now)


def _key_set(document: dict[str, Any], now: datetime) -> KeySet:
    entries = document.get("keys")
    if not isinstance(entries, list) or len(entries) > MAX_JWKS_KEYS:
        raise _unavailable("jwks_shape")
    keys: dict[str, RsaPublicKey] = {}
    ambiguous: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            key = rsa_key_from_jwk(entry)
        except OidcFailure:
            continue  # an unusable entry is skipped; a token naming it then finds no key
        if key is None:
            continue
        if key.kid in keys:
            ambiguous.add(key.kid)
        keys[key.kid] = key
    return KeySet(keys={kid: key for kid, key in keys.items() if kid not in ambiguous}, fetched_at=now)


class ProviderClient:
    def __init__(self, provider: ProviderConfig, http: httpx.Client, clock: Clock) -> None:
        self.provider = provider
        self.http = http
        self.clock = clock
        self._lock = threading.Lock()
        self._discovery: Discovery | None = None
        self._keys: KeySet | None = None
        self._discovery_failed_at: datetime | None = None

    def _get(self, url: str, reason: str) -> dict[str, Any]:
        try:
            https = is_https_url(url)
        except ValueError as exc:  # a malformed provider URL (for example an unclosed IPv6 bracket)
            raise _unavailable(f"{reason}_url_invalid") from exc
        if not https:
            raise _unavailable(f"{reason}_not_https")
        try:
            response = self.http.get(url, headers={"Accept": "application/json"})
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise _unavailable(f"{reason}_unreachable") from exc
        if response.status_code != 200:
            raise _unavailable(f"{reason}_status")
        return _json_response(response, reason)

    def discovery(self) -> Discovery:
        now = self.clock()
        with self._lock:
            cached = self._discovery
        if cached is not None and now - cached.fetched_at < DISCOVERY_TTL:
            return cached
        with self._lock:
            failed_at = self._discovery_failed_at
        if failed_at is not None and now - failed_at < DISCOVERY_FAILURE_COOLDOWN:
            raise _unavailable("discovery_cooldown")  # no outbound request while the provider is failing
        try:
            fresh = _validated_discovery(self.provider, self._get(self.provider.discovery_url, "discovery"), now)
        except ValueError as exc:  # OidcFailure is not a ValueError; this catches malformed discovery URLs
            with self._lock:
                self._discovery_failed_at = now
            raise _unavailable("discovery_invalid") from exc
        except OidcFailure:
            with self._lock:
                self._discovery_failed_at = now
            raise
        with self._lock:
            self._discovery = fresh
            self._discovery_failed_at = None
        return fresh

    def _fetch_keys(self) -> KeySet:
        fresh = _key_set(self._get(self.discovery().jwks_uri, "jwks"), self.clock())
        with self._lock:
            self._keys = fresh
        return fresh

    def signing_key(self, kid: str) -> RsaPublicKey:
        """The key for ``kid`` from the cache, else after exactly one refetch; otherwise a refusal."""
        with self._lock:
            cached = self._keys
        if cached is not None and self.clock() - cached.fetched_at < JWKS_TTL and kid in cached.keys:
            return cached.keys[kid]
        refreshed = self._fetch_keys()
        if kid not in refreshed.keys:
            raise OidcFailure(OIDC_TOKEN_INVALID, "unknown_kid")
        return refreshed.keys[kid]

    def exchange_code(self, code: str, verifier: str, client_credential: str) -> str:
        """Authorization code -> the compact ID token. Access and refresh tokens are discarded unread."""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.provider.redirect_uri,
                "client_id": self.provider.client_id, "code_verifier": verifier,
                "client_secret": client_credential}
        endpoint = self.discovery().token_endpoint
        try:
            response = self.http.post(endpoint, data=form, headers={"Accept": "application/json"})
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise _unavailable("token_unreachable") from exc
        if response.status_code != 200:
            raise OidcFailure(OIDC_PROVIDER_ERROR, "token_refused")
        document = _json_response(response, "token")
        compact = document.get("id_token")
        if not isinstance(compact, str) or not compact:
            raise OidcFailure(OIDC_PROVIDER_ERROR, "token_without_id_token")
        return compact


def build_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False)
