# SPDX-License-Identifier: AGPL-3.0-only
"""The OIDC provider configuration and the actor directory: presets, exact redirect URIs, secret references only,
closed role set, and the visible effect of each on ``GET /identity/v1/session`` and the OIDC endpoints."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pados.identity.actors import load_actor_directory
from pados.identity.oidc.config import load_oidc_config
from pados.identity.oidc.errors import OidcFailure
from tests.identity.identity_support import ORIGIN
from tests.identity.oidc.oidc_harness import ACTORS, ACTORS_CONFIG, ENTRA_PROVIDER, GOOGLE_PROVIDER, OIDC_CONFIG, \
    PROJECT_A, OidcHarness
from tests.identity.oidc.oidc_support import ENTRA_TENANT, OTHER_TENANT

ORIGINS = (ORIGIN,)


def _write(tmp_path: Path, document: object) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _config(tmp_path: Path, *providers: dict[str, Any], **top: Any) -> Any:
    return load_oidc_config(_write(tmp_path, {**OIDC_CONFIG, "providers": list(providers), **top}), ORIGINS)


def _refusal(action: Callable[[], object]) -> tuple[str, str]:
    with pytest.raises(OidcFailure) as caught:
        action()
    return caught.value.code, caught.value.reason


def test_unset_means_no_configuration() -> None:
    assert load_oidc_config("", ORIGINS) is None
    assert len(load_actor_directory("")) == 0


def test_presets_resolve_their_issuers(tmp_path: Path) -> None:
    config = _config(tmp_path, ENTRA_PROVIDER, GOOGLE_PROVIDER,
                     {**ENTRA_PROVIDER, "id": "corp", "kind": "generic", "tenant": None,
                      "issuer": "https://idp.example.org/realms/pados",
                      "redirect_uri": "https://pados.example.org/identity/v1/oidc/corp/callback"})
    entra, google, corp = config.providers
    assert entra.discovery_issuer == f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"
    assert entra.discovery_url.endswith(f"/{ENTRA_TENANT}/v2.0/.well-known/openid-configuration")
    assert google.discovery_issuer == "https://accounts.google.com"
    assert corp.discovery_url == "https://idp.example.org/realms/pados/.well-known/openid-configuration"
    assert entra.token_issuers(OTHER_TENANT) == ()  # a token from another tenant matches no issuer


@pytest.mark.parametrize(("change", "reason"), [
    ({"tenant": "common"}, "schema"),
    ({"tenant": "organizations"}, "schema"),
    ({"tenant": "consumers"}, "schema"),
    ({"tenant": "contoso.onmicrosoft.com"}, "schema"),  # tokens name the GUID, so a domain could never match
    ({"tenant": ENTRA_TENANT.upper()}, "schema"),
    ({"tenant": ENTRA_TENANT, "allowed_tenant_ids": [OTHER_TENANT]}, "schema"),
    ({"tenant": None}, "schema"),
    ({"issuer": "https://login.microsoftonline.com/x/v2.0"}, "schema"),
    ({"redirect_uri": "http://pados.example.org/identity/v1/oidc/entra/callback"}, "schema"),  # http, not loopback
    ({"redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/google/callback"}, "schema"),  # another provider
    ({"redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/entra/callback?x=1"}, "schema"),
    ({"redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/entra/callback#f"}, "schema"),
    ({"redirect_uri": "https://user@example.org/identity/v1/oidc/entra/callback"}, "schema"),
    ({"scopes": ["profile", "email"]}, "schema"),
    ({"scopes": ["openid", "openid"]}, "schema"),
    ({"scopes": ["openid", "bad scope"]}, "schema"),
    ({"client_secret_env": "DATABASE_PASSWORD_ENV"}, "schema"),  # only PADOS_OIDC_* names are readable
    ({"client_secret_env": "pados_oidc_lower"}, "schema"),
    ({"kind": "okta"}, "schema"),
    ({"unknown_field": True}, "schema"),
])
def test_entra_provider_rules(tmp_path: Path, change: dict[str, Any], reason: str) -> None:
    provider = {k: v for k, v in {**ENTRA_PROVIDER, **change}.items() if v is not None}
    assert _refusal(lambda: _config(tmp_path, provider)) == ("OIDC_CONFIG_INVALID", reason)


def test_entra_multi_tenant_requires_pinned_tids(tmp_path: Path) -> None:
    config = _config(tmp_path, {**ENTRA_PROVIDER, "tenant": "common", "allowed_tenant_ids": [ENTRA_TENANT]})
    provider = config.providers[0]
    assert provider.discovery_issuer == "https://login.microsoftonline.com/{tenantid}/v2.0"
    assert provider.token_issuers(ENTRA_TENANT) == (f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0",)
    assert provider.token_issuers(OTHER_TENANT) == () and provider.token_issuers(None) == ()


@pytest.mark.parametrize("provider", [
    {**GOOGLE_PROVIDER, "issuer": "https://accounts.google.example"},
    {**GOOGLE_PROVIDER, "tenant": ENTRA_TENANT},
    {**GOOGLE_PROVIDER, "kind": "generic"},  # a generic provider needs an issuer
    {**GOOGLE_PROVIDER, "kind": "generic", "issuer": "http://idp.example.org"},
])
def test_google_and_generic_rules(tmp_path: Path, provider: dict[str, Any]) -> None:
    assert _refusal(lambda: _config(tmp_path, provider))[0] == "OIDC_CONFIG_INVALID"


@pytest.mark.parametrize("credential_field", [
    {"client_secret": "inline-value"},
    {"clientSecret": "inline-value"},
    {"refresh_token": "inline-value"},
    {"password": "inline-value"},
    {"private_key": "inline-value"},
    {"client_assertion": "inline-value"},
])
def test_a_plaintext_credential_anywhere_refuses_the_file(tmp_path: Path, credential_field: dict[str, str]) -> None:
    nested = {**OIDC_CONFIG, "providers": [{**ENTRA_PROVIDER, **credential_field}]}
    top = {**OIDC_CONFIG, **credential_field}
    for document in (nested, top):
        code, reason = _refusal(lambda document=document: load_oidc_config(_write(tmp_path, document), ORIGINS))
        assert (code, reason) == ("IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN", "plaintext_credential")


@pytest.mark.parametrize(("value", "ok"), [
    ("/", True), ("/control-center", True), (ORIGIN + "/", True), ("//evil.example/", False),
    ("/\\evil.example", False), ("https://evil.example/", False), ("/?next=x", False), ("/#x", False),
])
def test_post_login_redirect_stays_on_an_allowlisted_origin(tmp_path: Path, value: str, ok: bool) -> None:
    def load() -> object:
        return _config(tmp_path, ENTRA_PROVIDER, post_login_redirect=value)
    if ok:
        assert load() is not None
    else:
        assert _refusal(load)[0] == "OIDC_CONFIG_INVALID"


def test_unreadable_and_malformed_files(tmp_path: Path) -> None:
    assert _refusal(lambda: load_oidc_config(str(tmp_path / "missing.json"), ORIGINS)) == \
        ("OIDC_CONFIG_INVALID", "unreadable")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _refusal(lambda: load_oidc_config(str(bad), ORIGINS))[1] == "not_json"
    assert _refusal(lambda: load_actor_directory(str(bad))) == ("ACTORS_CONFIG_INVALID", "not_json")
    duplicate = {**OIDC_CONFIG, "providers": [ENTRA_PROVIDER, ENTRA_PROVIDER]}
    assert _refusal(lambda: load_oidc_config(_write(tmp_path, duplicate), ORIGINS))[1] == "schema"


# --- the actor directory ------------------------------------------------------------------------------------------
def _actors(tmp_path: Path, actors: list[dict[str, Any]]) -> Any:
    return load_actor_directory(_write(tmp_path, {"schema": "pados.identity.actors.v1", "actors": actors}))


def test_bindings_are_exact_on_issuer_and_subject(tmp_path: Path) -> None:
    directory = _actors(tmp_path, ACTORS)
    alice = directory.bound(ACTORS[0]["issuer"], "entra-sub-alice")
    assert alice is not None and alice.actor_id == "alice"
    assert alice.may(PROJECT_A, "work_request.create") and not alice.may("other-project", "work_request.create")
    assert directory.bound(ACTORS[0]["issuer"], "ENTRA-SUB-ALICE") is None
    assert directory.bound(ACTORS[0]["issuer"] + "/", "entra-sub-alice") is None
    dave = directory.bound(ACTORS[3]["issuer"], "entra-sub-dave")
    assert dave is not None and not dave.may(PROJECT_A, "work_request.create")  # approver grants no action here


@pytest.mark.parametrize("actors", [
    [{**ACTORS[0], "project_roles": {PROJECT_A: ["admin"]}}],  # outside the closed role set
    [{**ACTORS[0], "project_roles": {PROJECT_A: ["viewer", "viewer"]}}],
    [{**ACTORS[0], "project_roles": {"bad project id!": ["viewer"]}}],
    [ACTORS[0], {**ACTORS[1], "actor_id": "alice"}],  # duplicate actor id
    [ACTORS[0], {**ACTORS[1], "issuer": ACTORS[0]["issuer"], "subject": "entra-sub-alice"}],  # one identity, two
    [{**ACTORS[0], "actor_id": "Alice Admin"}],
    [{**ACTORS[0], "email": "alice@example.com"}],  # email is not a binding field
    [{**ACTORS[0], "may_control": True}],
])
def test_actor_directory_rules(tmp_path: Path, actors: list[dict[str, Any]]) -> None:
    assert _refusal(lambda: _actors(tmp_path, actors)) == ("ACTORS_CONFIG_INVALID", "schema")


def test_actor_directory_refuses_plaintext_credentials(tmp_path: Path) -> None:
    code, _ = _refusal(lambda: _actors(tmp_path, [{**ACTORS[0], "access_token": "x"}]))
    assert code == "IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN"
    # Project ids are data keys: a project literally named "token" is not a credential field.
    assert len(_actors(tmp_path, [{**ACTORS[0], "project_roles": {"token": ["viewer"]}}])) == 1


# --- visible effect on the API ------------------------------------------------------------------------------------
def test_unconfigured_providers_are_hidden_and_refused(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(oidc_config=None)
    client = harness.client()
    assert client.get("/identity/v1/session").json()["providers"] == []
    for path in ("/identity/v1/oidc/entra/start", "/identity/v1/oidc/entra/callback?code=c&state=s",
                 "/identity/v1/oidc/unknown/start"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404 and response.json()["error"]["code"] == "OIDC_PROVIDER_NOT_CONFIGURED"
    assert harness.network.requests == []


def test_an_unknown_provider_id_is_refused(oidc: OidcHarness) -> None:
    response = oidc.client().get("/identity/v1/oidc/okta/start", follow_redirects=False)
    assert response.status_code == 404 and response.json()["error"]["code"] == "OIDC_PROVIDER_NOT_CONFIGURED"


def test_a_plaintext_secret_config_hides_providers_and_refuses(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(oidc_config={**OIDC_CONFIG, "providers": [{**ENTRA_PROVIDER, "client_secret": "inline"}]})
    client = harness.client()
    assert client.get("/identity/v1/session").json()["providers"] == []
    response = client.get("/identity/v1/oidc/entra/start", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN"
    assert harness.network.requests == []


def test_an_invalid_actor_directory_hides_providers_and_refuses(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(actors_config={**ACTORS_CONFIG, "actors": [{**ACTORS[0], "project_roles": {"p": ["root"]}}]})
    client = harness.client()
    assert client.get("/identity/v1/session").json()["providers"] == []
    response = client.get("/identity/v1/oidc/entra/start", follow_redirects=False)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ACTORS_CONFIG_INVALID"


def test_the_feature_flag_gates_everything(make_oidc: Callable[..., OidcHarness]) -> None:
    harness = make_oidc(enabled=False)
    client = harness.client()
    assert client.get("/identity/v1/session").json() == {"enabled": False, "authenticated": False, "providers": []}
    for path in ("/identity/v1/oidc/entra/start", "/identity/v1/oidc/entra/callback", "/identity/v1/me"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404 and response.json()["error"]["code"] == "IDENTITY_DISABLED"


def test_configured_providers_are_listed_without_secrets(oidc: OidcHarness) -> None:
    providers = oidc.client().get("/identity/v1/session").json()["providers"]
    assert providers == [
        {"id": "entra", "kind": "entra", "display_name": "Microsoft Entra ID",
         "start_url": "/identity/v1/oidc/entra/start"},
        {"id": "google", "kind": "google", "display_name": "Google", "start_url": "/identity/v1/oidc/google/start"}]
