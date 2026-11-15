# SPDX-License-Identifier: AGPL-3.0-only
"""Shared pieces of the phase 2 tests: an app with the identity flag on, an OIDC configuration (Entra preset with a
pinned tenant, Google preset), an actor directory, and a fake provider network. No real tenant is contacted."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from tests.identity.identity_support import ORIGIN, FakeClock, Harness
from tests.identity.oidc.oidc_support import (
    ENTRA_CLIENT_CREDENTIAL,
    ENTRA_CLIENT_ID,
    ENTRA_ENV,
    ENTRA_ISSUER,
    ENTRA_TENANT,
    GOOGLE_CLIENT_CREDENTIAL,
    GOOGLE_CLIENT_ID,
    GOOGLE_ENV,
    GOOGLE_ISSUER,
    FakeIssuer,
    FakeNetwork,
)

PROJECT_A = "11111111-1111-4111-8111-111111111111"
PROJECT_B = "22222222-2222-4222-8222-222222222222"
ENTRA_PROVIDER = {"id": "entra", "kind": "entra", "tenant": ENTRA_TENANT, "client_id": ENTRA_CLIENT_ID,
                  "client_secret_env": ENTRA_ENV,
                  "redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/entra/callback",
                  "scopes": ["openid", "profile", "email"]}
GOOGLE_PROVIDER = {"id": "google", "kind": "google", "client_id": GOOGLE_CLIENT_ID, "client_secret_env": GOOGLE_ENV,
                   "redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/google/callback"}
OIDC_CONFIG = {"schema": "pados.identity.oidc.v1", "post_login_redirect": "/", "providers": [ENTRA_PROVIDER,
                                                                                           GOOGLE_PROVIDER]}
ACTORS = [
    {"actor_id": "alice", "display_name": "Alice", "issuer": ENTRA_ISSUER, "subject": "entra-sub-alice",
     "project_roles": {PROJECT_A: ["viewer", "contributor"], PROJECT_B: ["viewer"]}},
    {"actor_id": "bob", "issuer": GOOGLE_ISSUER, "subject": "google-sub-bob",
     "project_roles": {PROJECT_B: ["contributor"]}},
    {"actor_id": "carol", "issuer": ENTRA_ISSUER, "subject": "entra-sub-carol",
     "project_roles": {PROJECT_A: ["viewer"]}},
    {"actor_id": "dave", "issuer": ENTRA_ISSUER, "subject": "entra-sub-dave",
     "project_roles": {PROJECT_A: ["approver"]}},
]
ACTORS_CONFIG = {"schema": "pados.identity.actors.v1", "actors": ACTORS}
ENVIRON = {ENTRA_ENV: ENTRA_CLIENT_CREDENTIAL, GOOGLE_ENV: GOOGLE_CLIENT_CREDENTIAL}


@dataclass
class OidcHarness:
    harness: Harness
    network: FakeNetwork
    entra: FakeIssuer
    google: FakeIssuer
    clients: list[TestClient] = field(default_factory=list)

    @property
    def clock(self) -> FakeClock:
        return self.harness.clock

    def client(self) -> TestClient:
        return self.harness.client()

    def issuer(self, provider_id: str) -> FakeIssuer:
        return {"entra": self.entra, "google": self.google}[provider_id]

    def start(self, client: TestClient, provider_id: str = "entra") -> httpx.Response:
        return client.get(f"/identity/v1/oidc/{provider_id}/start", follow_redirects=False)

    def callback(self, client: TestClient, params: dict[str, str], provider_id: str = "entra") -> httpx.Response:
        return client.get(f"/identity/v1/oidc/{provider_id}/callback", params=params, follow_redirects=False)

    def sign_in(self, client: TestClient, subject: str, provider_id: str = "entra", **authorize: Any) -> httpx.Response:
        started = self.start(client, provider_id)
        assert started.status_code == 302, started.text
        params = self.issuer(provider_id).authorize(started.headers["location"], subject=subject, **authorize)
        return self.callback(client, params, provider_id)

    def signed_in(self, subject: str = "entra-sub-alice", provider_id: str = "entra") -> tuple[TestClient, str]:
        """A browser with a live actor session and its CSRF token (from ``GET /me``)."""
        client = self.client()
        done = self.sign_in(client, subject, provider_id)
        assert done.status_code == 302 and done.headers["location"] == "/?sso=ok", done.headers.get("location")
        me = client.get("/identity/v1/me")
        assert me.status_code == 200, me.text
        return client, str(me.json()["csrf_token"])


def write_json(path: Path, document: object) -> str:
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def write_headers(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-PADOS-CSRF": csrf, "Sec-Fetch-Site": "same-origin"}
