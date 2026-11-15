# SPDX-License-Identifier: AGPL-3.0-only
"""The OIDC provider configuration (``PADOS_IDENTITY_OIDC_CONFIG``, schema ``pados.identity.oidc.v1``).

A provider names its client credential only by an environment variable (``client_secret_env``, which must start with
``PADOS_OIDC_``); a file carrying a plaintext credential field is refused whole. Presets:

* ``entra``: issuer ``https://login.microsoftonline.com/{tenant}/v2.0``. The tenant is pinned (a directory GUID).
  ``common`` and ``organizations`` are refused unless ``allowed_tenant_ids`` pins the accepted ``tid`` values;
  ``consumers`` is always refused.
* ``google``: issuer ``https://accounts.google.com``.
* ``generic``: any HTTPS ``issuer``.

The redirect URI is exact: HTTPS, or HTTP on a loopback host, with the path ``/identity/v1/oidc/{id}/callback`` and
no query or fragment. ``post_login_redirect`` is a same-origin path or an allowlisted origin's URL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from pados.identity.errors import IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN, OIDC_CONFIG_INVALID
from pados.identity.oidc.errors import OidcFailure
from pados.identity.plaintext import contains_plaintext_credential
from pados.identity.store import ProviderId

SCHEMA = "pados.identity.oidc.v1"
MAX_CONFIG_BYTES = 64 * 1024
ENTRA_AUTHORITY = "https://login.microsoftonline.com"
ENTRA_MULTI_TENANT = frozenset({"common", "organizations"})
ENTRA_TENANT_PLACEHOLDER = "{tenantid}"
GOOGLE_ISSUER = "https://accounts.google.com"
# Google documents both forms of ``iss`` for its ID tokens; each is compared exactly.
GOOGLE_TOKEN_ISSUERS = (GOOGLE_ISSUER, "accounts.google.com")
DEFAULT_SCOPES = ("openid", "profile", "email")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SCOPE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]{1,64}$")  # RFC 6749 §3.3 scope-token

ClientId = Annotated[str, StringConstraints(pattern=r"^[\x21-\x7E]{1,256}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^PADOS_OIDC_[A-Z0-9_]{1,100}$")]


def is_https_url(value: str) -> bool:
    parts = urlsplit(value)
    return (parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
            and not parts.fragment)


def callback_path(provider_id: str) -> str:
    return f"/identity/v1/oidc/{provider_id}/callback"


def _redirect_uri_ok(value: str, provider_id: str) -> bool:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        return False
    secure = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)
    return secure and parts.path == callback_path(provider_id)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ProviderId
    kind: Literal["entra", "google", "generic"]
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    issuer: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    tenant: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    allowed_tenant_ids: tuple[str, ...] = ()
    client_id: ClientId
    client_secret_env: EnvName
    redirect_uri: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    @model_validator(mode="after")
    def _preset_rules(self) -> ProviderConfig:
        if not _redirect_uri_ok(self.redirect_uri, self.id):
            raise ValueError("redirect_uri must be exact: https (or loopback http) to this provider's callback")
        if "openid" not in self.scopes or len(set(self.scopes)) != len(self.scopes) or len(self.scopes) > 16:
            raise ValueError("scopes must include openid, without duplicates")
        if not all(_SCOPE.fullmatch(scope) for scope in self.scopes):
            raise ValueError("scope token not allowed")
        if self.kind == "entra":
            self._entra_rules()
        else:
            if self.tenant is not None or self.allowed_tenant_ids:
                raise ValueError("tenant settings apply to entra only")
            if self.kind == "google" and self.issuer not in (None, GOOGLE_ISSUER):
                raise ValueError("the google preset has a fixed issuer")
            if self.kind == "generic" and (self.issuer is None or not is_https_url(self.issuer)):
                raise ValueError("a generic provider needs an https issuer")
        return self

    def _entra_rules(self) -> None:
        if self.issuer is not None or self.tenant is None:
            raise ValueError("entra takes a tenant, not an issuer")
        tenant = self.tenant
        if tenant == "consumers":
            raise ValueError("the consumers tenant is not supported")
        if tenant in ENTRA_MULTI_TENANT:
            if not self.allowed_tenant_ids or not all(_GUID.fullmatch(tid) for tid in self.allowed_tenant_ids):
                raise ValueError("common/organizations need allowed_tenant_ids (lower-case directory GUIDs)")
        elif not _GUID.fullmatch(tenant) or self.allowed_tenant_ids:
            raise ValueError("a pinned entra tenant is a lower-case directory GUID")

    @property
    def multi_tenant(self) -> bool:
        return self.kind == "entra" and self.tenant in ENTRA_MULTI_TENANT

    @property
    def discovery_issuer(self) -> str:
        """The ``issuer`` the discovery document must state, exactly."""
        if self.kind == "entra":
            tenant = ENTRA_TENANT_PLACEHOLDER if self.multi_tenant else str(self.tenant)
            return f"{ENTRA_AUTHORITY}/{tenant}/v2.0"
        if self.kind == "google":
            return GOOGLE_ISSUER
        return str(self.issuer)

    @property
    def discovery_url(self) -> str:
        base = f"{ENTRA_AUTHORITY}/{self.tenant}/v2.0" if self.kind == "entra" else self.discovery_issuer
        return base.rstrip("/") + "/.well-known/openid-configuration"

    def token_issuers(self, tenant_id: object) -> tuple[str, ...]:
        """Every ``iss`` an ID token may carry, compared exactly; empty when the token's tenant is not accepted."""
        if self.kind == "google":
            return GOOGLE_TOKEN_ISSUERS
        if self.multi_tenant:
            if not isinstance(tenant_id, str) or tenant_id not in self.allowed_tenant_ids:
                return ()
            return (f"{ENTRA_AUTHORITY}/{tenant_id}/v2.0",)
        if self.kind == "entra" and tenant_id is not None and tenant_id != self.tenant:
            return ()
        return (self.discovery_issuer,)

    def identity_issuer(self, token_issuer: str) -> str:
        """The issuer half of the identity key: Google's two documented forms are one issuer."""
        return GOOGLE_ISSUER if self.kind == "google" else token_issuer

    @property
    def label(self) -> str:
        defaults = {"entra": "Microsoft Entra ID", "google": "Google", "generic": self.id}
        return self.display_name or defaults[self.kind]


class OidcConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.identity.oidc.v1"] = Field(alias="schema")
    post_login_redirect: Annotated[str, StringConstraints(min_length=1, max_length=512)] = "/"
    providers: tuple[ProviderConfig, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _unique_ids(self) -> OidcConfig:
        if len({provider.id for provider in self.providers}) != len(self.providers):
            raise ValueError("provider ids must be unique")
        return self

    def provider(self, provider_id: str) -> ProviderConfig | None:
        return next((p for p in self.providers if p.id == provider_id), None)


def post_login_redirect_ok(value: str, allowed_origins: tuple[str, ...]) -> bool:
    """A same-origin absolute path (never ``//host`` or a backslash), or a URL on an allowlisted origin.

    No query or fragment: the callback appends ``?sso=ok`` or ``?sso_error=<CODE>`` itself.
    """
    if "?" in value or "#" in value:
        return False
    if value.startswith("/"):
        return not value.startswith("//") and "\\" not in value and value.isprintable()
    parts = urlsplit(value)
    return (f"{parts.scheme}://{parts.netloc}" in allowed_origins and not parts.username
            and "\\" not in value and value.isprintable())


def load_oidc_config(path: str, allowed_origins: tuple[str, ...]) -> OidcConfig | None:
    """``None`` when unset. Any problem is one closed code with a fixed reason word (never file content)."""
    if not path.strip():
        return None
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise OidcFailure(OIDC_CONFIG_INVALID, "unreadable") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise OidcFailure(OIDC_CONFIG_INVALID, "too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OidcFailure(OIDC_CONFIG_INVALID, "not_json") from exc
    if contains_plaintext_credential(document):
        raise OidcFailure(IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN, "plaintext_credential")
    try:
        config = OidcConfig.model_validate(document)
    except ValidationError as exc:
        raise OidcFailure(OIDC_CONFIG_INVALID, "schema") from exc
    if not post_login_redirect_ok(config.post_login_redirect, allowed_origins):
        raise OidcFailure(OIDC_CONFIG_INVALID, "post_login_redirect")
    return config
