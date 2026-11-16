# SPDX-License-Identifier: AGPL-3.0-only
"""A local fake OpenID provider for the phase 2 tests (no real tenant, no network).

TEST ONLY: the RSA keys below are generated in-process from fixed seeds with a textbook Miller-Rabin search. They are
deterministic, never written to disk, and must never be used for anything but these tests.

``FakeNetwork`` is an ``httpx.MockTransport`` serving discovery, JWKS and token endpoints for any number of
``FakeIssuer`` instances (Entra-shaped and Google-shaped URLs over HTTPS). ``FakeIssuer.authorize`` plays the user
signing in at the provider: it reads the authorization request and returns the callback parameters.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from tests.identity.identity_support import FakeClock

ENTRA_TENANT = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
OTHER_TENANT = "0a0b0c0d-1e1f-4a2b-8c3d-4e5f6a7b8c9d"
ENTRA_ISSUER = f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"
GOOGLE_ISSUER = "https://accounts.google.com"
ENTRA_CLIENT_ID = "11111111-2222-4333-8444-555555555555"
GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.example"
ENTRA_ENV = "PADOS_OIDC_ENTRA_CLIENT_SECRET"
GOOGLE_ENV = "PADOS_OIDC_GOOGLE_CLIENT_SECRET"
# Test-only client credentials, held in the fake environment mapping (never in a config file).
ENTRA_CLIENT_CREDENTIAL = "entra-test-client-credential-value-0001"
GOOGLE_CLIENT_CREDENTIAL = "google-test-client-credential-value-0002"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- TEST ONLY RSA ------------------------------------------------------------------------------------------------
_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % d for d in range(2, int(p ** 0.5) + 1))]


def _probable_prime(n: int, rng: random.Random) -> bool:
    if any(n % p == 0 for p in _SMALL_PRIMES):
        return n in _SMALL_PRIMES
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(24):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (3 << (bits - 2)) | 1
        if _probable_prime(candidate, rng):
            return candidate


@dataclass(frozen=True)
class TestRsaKey:
    """TEST ONLY key material: n, e, d."""

    kid: str
    n: int
    e: int
    d: int

    def jwk(self) -> dict[str, str]:
        size = (self.n.bit_length() + 7) // 8
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                "n": b64u(self.n.to_bytes(size, "big")), "e": b64u(self.e.to_bytes(3, "big"))}

    def public_bytes(self) -> bytes:
        return json.dumps(self.jwk(), sort_keys=True).encode()

    def sign(self, message: bytes) -> bytes:
        size = (self.n.bit_length() + 7) // 8
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
        encoded = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
        return pow(int.from_bytes(encoded, "big"), self.d, self.n).to_bytes(size, "big")


@functools.lru_cache(maxsize=None)
def test_rsa_key(seed: int, kid: str, bits: int = 2048) -> TestRsaKey:
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _prime(bits // 2, rng), _prime(bits // 2, rng)
        phi = (p - 1) * (q - 1)
        n = p * q
        if p != q and n.bit_length() == bits and phi % e:
            return TestRsaKey(kid=kid, n=n, e=e, d=pow(e, -1, phi))


test_rsa_key.__test__ = False  # type: ignore[attr-defined]
TestRsaKey.__test__ = False  # type: ignore[attr-defined]


def compact_jws(header: dict[str, Any], claims: dict[str, Any], key: TestRsaKey | None) -> str:
    signing_input = f"{b64u(json.dumps(header).encode())}.{b64u(json.dumps(claims).encode())}"
    signature = key.sign(signing_input.encode("ascii")) if key is not None else b""
    return f"{signing_input}.{b64u(signature)}"


# --- the fake provider --------------------------------------------------------------------------------------------
TokenFactory = Callable[["FakeIssuer", dict[str, Any], dict[str, Any]], str]


@dataclass
class PendingCode:
    challenge: str
    nonce: str
    redirect_uri: str
    subject: str
    email: str | None
    claims: dict[str, Any]
    factory: TokenFactory | None
    used: bool = False


@dataclass
class FakeIssuer:
    clock: FakeClock
    issuer: str
    client_id: str
    client_credential: str
    discovery_url: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    discovery_issuer: str | None = None
    signing_key: TestRsaKey = field(default_factory=lambda: test_rsa_key(1, "kid-primary"))
    published_keys: list[TestRsaKey] = field(default_factory=list)
    extra_claims: dict[str, Any] = field(default_factory=dict)
    jwks_fetches: int = 0
    discovery_fetches: int = 0
    token_requests: list[dict[str, str]] = field(default_factory=list)
    fail_jwks: bool = False
    fail_discovery: bool = False
    codes: dict[str, PendingCode] = field(default_factory=dict)
    issued_tokens: list[str] = field(default_factory=list)
    _counter: int = 0

    def __post_init__(self) -> None:
        if not self.published_keys:
            self.published_keys = [self.signing_key]

    def discovery(self) -> dict[str, Any]:
        return {"issuer": self.discovery_issuer or self.issuer, "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint, "jwks_uri": self.jwks_uri,
                "response_types_supported": ["code", "id_token"], "subject_types_supported": ["pairwise"],
                "id_token_signing_alg_values_supported": ["RS256"], "scopes_supported": ["openid", "profile", "email"]}

    def authorize(self, authorization_url: str, *, subject: str, email: str | None = "user@example.com",
                  claims: dict[str, Any] | None = None, factory: TokenFactory | None = None) -> dict[str, str]:
        """The user signs in: returns the callback query parameters (``code`` and ``state``)."""
        parts = urlsplit(authorization_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == self.authorization_endpoint
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256" and "openid" in query["scope"].split()
        self._counter += 1
        code = f"fake-authorization-code-{self._counter:04d}-{b64u(hashlib.sha256(query['state'].encode()).digest())}"
        self.codes[code] = PendingCode(challenge=query["code_challenge"], nonce=query["nonce"],
                                       redirect_uri=query["redirect_uri"], subject=subject, email=email,
                                       claims=dict(claims or {}), factory=factory)
        return {"code": code, "state": query["state"]}

    def standard_claims(self, pending: PendingCode) -> dict[str, Any]:
        now = int(self.clock.now.timestamp())
        claims: dict[str, Any] = {"iss": self.issuer, "sub": pending.subject, "aud": self.client_id, "iat": now,
                                  "nbf": now, "exp": now + 3600, "nonce": pending.nonce, **self.extra_claims}
        if pending.email is not None:
            claims["email"] = pending.email
        claims.update(pending.claims)
        return {k: v for k, v in claims.items() if v is not None}

    def mint(self, pending: PendingCode) -> str:
        claims = self.standard_claims(pending)
        header = {"alg": "RS256", "typ": "JWT", "kid": self.signing_key.kid}
        if pending.factory is not None:
            return pending.factory(self, header, claims)
        return compact_jws(header, claims, self.signing_key)

    def token(self, form: dict[str, str]) -> httpx.Response:
        self.token_requests.append(form)
        pending = self.codes.get(form.get("code", ""))
        if (form.get("grant_type") != "authorization_code" or form.get("client_id") != self.client_id
                or form.get("client_secret") != self.client_credential or pending is None or pending.used
                or form.get("redirect_uri") != pending.redirect_uri):
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "provider detail"})
        pending.used = True  # a code is single use at the provider as well
        verifier = form.get("code_verifier", "")
        if b64u(hashlib.sha256(verifier.encode()).digest()) != pending.challenge:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE mismatch"})
        minted = self.mint(pending)
        self.issued_tokens.append(minted)
        return httpx.Response(200, json={"id_token": minted, "access_token": "fake-access-value",
                                         "token_type": "Bearer", "expires_in": 3600})


def entra_issuer(clock: FakeClock, *, tenant: str = ENTRA_TENANT, authority_tenant: str | None = None) -> FakeIssuer:
    """``authority_tenant`` ``organizations``/``common`` serves the multi-tenant discovery document."""
    authority = f"https://login.microsoftonline.com/{authority_tenant or tenant}"
    return FakeIssuer(
        clock=clock, issuer=f"https://login.microsoftonline.com/{tenant}/v2.0", client_id=ENTRA_CLIENT_ID,
        client_credential=ENTRA_CLIENT_CREDENTIAL, discovery_url=f"{authority}/v2.0/.well-known/openid-configuration",
        authorization_endpoint=f"{authority}/oauth2/v2.0/authorize", token_endpoint=f"{authority}/oauth2/v2.0/token",
        jwks_uri=f"{authority}/discovery/v2.0/keys",
        discovery_issuer=("https://login.microsoftonline.com/{tenantid}/v2.0" if authority_tenant else None),
        extra_claims={"tid": tenant, "ver": "2.0"})


def google_issuer(clock: FakeClock) -> FakeIssuer:
    return FakeIssuer(
        clock=clock, issuer=GOOGLE_ISSUER, client_id=GOOGLE_CLIENT_ID, client_credential=GOOGLE_CLIENT_CREDENTIAL,
        discovery_url="https://accounts.google.com/.well-known/openid-configuration",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token", jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        signing_key=test_rsa_key(2, "google-kid-1"), extra_claims={"azp": GOOGLE_CLIENT_ID})


class FakeNetwork:
    """One mock transport for every fake issuer; any other URL is a connection error."""

    def __init__(self, *issuers: FakeIssuer) -> None:
        self.issuers = list(issuers)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        for fake in self.issuers:
            if request.method == "GET" and url == fake.discovery_url:
                fake.discovery_fetches += 1
                return httpx.Response(503) if fake.fail_discovery else httpx.Response(200, json=fake.discovery())
            if request.method == "GET" and url == fake.jwks_uri:
                fake.jwks_fetches += 1
                if fake.fail_jwks:
                    return httpx.Response(500, text="jwks outage")
                return httpx.Response(200, json={"keys": [key.jwk() for key in fake.published_keys]})
            if request.method == "POST" and url == fake.token_endpoint:
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                return fake.token(form)
        raise httpx.ConnectError("no route in the fake network", request=request)
