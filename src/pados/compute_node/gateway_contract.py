# SPDX-License-Identifier: AGPL-3.0-only
"""Declarative contract of a generic enterprise HTTP AI gateway (``pados.compute-node.gateway-contract.v1``).

The contract lives in a PRIVATE deployment file (never in the repository): it describes how PADOS talks to one gateway
without assuming OpenAI compatibility: the base URL, the auth scheme with a secret REFERENCE (``secretref://env/NAME``,
never a value), the discovery route and the JSON path to its model list, the chat route with a request template
filled from generic fields and the JSON paths to the answer and usage, the health route, timeouts, retry and circuit
settings, the rate-limit headers, whether the gateway honours an idempotency key, and the data policy (the highest
classification it may receive and whether execution is enabled at all).

Nothing here authorizes a model: a gateway model is unmeasured until a measured profile exists for it, and the existing
compute-node registration stays the only authorization. The contract can only narrow what may be sent.
"""
from __future__ import annotations

import ipaddress
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import is_secret_ref, looks_like_secret, reserved_env_ref

GATEWAY_CONTRACT_SCHEMA = "pados.compute-node.gateway-contract.v1"
MAX_CONTRACT_BYTES = 256 * 1024
PLACEHOLDERS = frozenset({"model", "messages", "prompt", "max_output_tokens", "temperature"})
_PLACEHOLDER = re.compile(r"^\{\{([a-z_]{1,32})\}\}$")
_SEGMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_-]{0,63})|\[(\*|[0-9]{1,3})\]")
_JSON_PATH = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_-]{0,63}|\[(?:\*|[0-9]{1,3})\])(?:\.[A-Za-z_][A-Za-z0-9_-]{0,63}|\[(?:\*|[0-9]{1,3})\])*$")
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection", "cookie"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ContractError(ValueError):
    """A contract that cannot be loaded; the message is one closed code."""


class AuthScheme(StrEnum):
    BEARER = "bearer"
    HEADER = "header"
    NONE = "none"


def _json_path(value: str | None) -> str | None:
    if value is not None and not _JSON_PATH.fullmatch(value):
        raise ValueError("GATEWAY_JSON_PATH_INVALID")
    return value


def json_path_values(document: Any, path: str) -> list[Any]:
    """Every value at ``path`` (``data[*].id``, ``choices[0].message.content``). Missing -> []. Never raises."""
    current: list[Any] = [document]
    for match in _SEGMENT.finditer(path):
        key, index = match.group(1), match.group(2)
        following: list[Any] = []
        for value in current:
            if key is not None:
                if isinstance(value, dict) and key in value:
                    following.append(value[key])
            elif index == "*":
                if isinstance(value, list):
                    following.extend(value)
            elif isinstance(value, list) and int(index) < len(value):
                following.append(value[int(index)])
        current = following
    return current


def json_path_one(document: Any, path: str | None) -> Any:
    """The single value at ``path`` (``None`` when absent or ambiguous)."""
    if path is None:
        return None
    values = json_path_values(document, path)
    return values[0] if len(values) == 1 else None


class GatewayAuth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: AuthScheme
    # A reference only (``secretref://env/<NAME>``): the value is resolved inside one call and never stored or returned.
    auth_ref: str | None = None
    header_name: str | None = None

    @model_validator(mode="after")
    def scheme_fields(self) -> GatewayAuth:
        if self.scheme is AuthScheme.NONE:
            if self.auth_ref is not None or self.header_name is not None:
                raise ValueError("GATEWAY_AUTH_NONE_TAKES_NO_REFERENCE")
            return self
        if self.auth_ref is None or not is_secret_ref(self.auth_ref):
            raise ValueError("SECRET_REFERENCE_REQUIRED")
        if reserved_env_ref(self.auth_ref):
            raise ValueError("GATEWAY_AUTH_REFERENCE_RESERVED")
        if self.scheme is AuthScheme.HEADER:
            if self.header_name is None or not _HEADER.fullmatch(self.header_name) \
                    or self.header_name.casefold() in _RESERVED_HEADERS:
                raise ValueError("GATEWAY_AUTH_HEADER_INVALID")
        elif self.header_name is not None:
            raise ValueError("GATEWAY_AUTH_HEADER_ONLY_FOR_HEADER_SCHEME")
        return self


class RouteSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=200)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if not value.startswith("/") or "//" in value or ".." in value or any(c in value for c in "?#\\ @") \
                or "://" in value or "{" in value:
            raise ValueError("GATEWAY_ROUTE_PATH_INVALID")
        return value


class DiscoverySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: RouteSpec
    models_path: str  # JSON path to the list of model objects
    id_field: str = "id"
    digest_field: str | None = None  # a content identity the gateway reports for the model, when it reports one
    state_field: str | None = None  # e.g. a load state; models whose state is not in ``loaded_values`` are not loaded
    loaded_values: tuple[str, ...] = ()

    @field_validator("models_path", "id_field", "digest_field", "state_field")
    @classmethod
    def paths(cls, value: str | None) -> str | None:
        return _json_path(value)

    @model_validator(mode="after")
    def read_only(self) -> DiscoverySpec:
        if self.route.method != "GET":
            raise ValueError("GATEWAY_DISCOVERY_MUST_BE_GET")
        if (self.state_field is None) != (not self.loaded_values):
            raise ValueError("GATEWAY_LOADED_STATE_INCOMPLETE")
        return self


class HealthSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: RouteSpec
    expected_status: int = Field(default=200, ge=200, le=299)

    @model_validator(mode="after")
    def read_only(self) -> HealthSpec:
        if self.route.method != "GET":
            raise ValueError("GATEWAY_HEALTH_MUST_BE_GET")
        return self


class ChatSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: RouteSpec
    # JSON request body; a string value that is exactly ``{{name}}`` is replaced (type-preserving) by a generic field
    # from PLACEHOLDERS. Anything else is sent as written.
    request_template: dict[str, Any]
    text_path: str
    model_path: str | None = None  # the model identity the gateway reports in the answer (checked when present)
    finish_reason_path: str | None = None
    truncated_values: tuple[str, ...] = ("length",)
    usage_prompt_path: str | None = None
    usage_completion_path: str | None = None
    # Gateway-specific error codes in an error body -> closed PADOS failures (only these four may be mapped).
    error_code_path: str | None = None
    error_codes: dict[str, Literal["GATEWAY_MODEL_NOT_LOADED", "GATEWAY_MODEL_NOT_FOUND", "GATEWAY_RATE_LIMITED",
                                   "GATEWAY_AUTH_FAILED"]] = Field(default_factory=dict)

    @field_validator("text_path", "model_path", "finish_reason_path", "usage_prompt_path", "usage_completion_path",
                     "error_code_path")
    @classmethod
    def paths(cls, value: str | None) -> str | None:
        return _json_path(value)

    @model_validator(mode="after")
    def template(self) -> ChatSpec:
        if self.route.method != "POST":
            raise ValueError("GATEWAY_CHAT_MUST_BE_POST")
        used = set(_placeholders(self.request_template))
        if "model" not in used or not used & {"messages", "prompt"} or "max_output_tokens" not in used:
            raise ValueError("GATEWAY_TEMPLATE_REQUIRES_MODEL_INPUT_AND_BUDGET")
        if len(self.error_codes) > 32 or any(len(key) > 80 for key in self.error_codes):
            raise ValueError("GATEWAY_ERROR_CODES_TOO_LARGE")
        return self


def _placeholders(value: Any, depth: int = 0) -> list[str]:
    if depth > 8:
        raise ValueError("GATEWAY_TEMPLATE_TOO_DEEP")
    if isinstance(value, dict):
        return [name for item in value.values() for name in _placeholders(item, depth + 1)]
    if isinstance(value, list):
        return [name for item in value for name in _placeholders(item, depth + 1)]
    if isinstance(value, str) and "{{" in value:
        match = _PLACEHOLDER.fullmatch(value)
        if match is None or match.group(1) not in PLACEHOLDERS:
            raise ValueError("GATEWAY_TEMPLATE_PLACEHOLDER_UNKNOWN")
        return [match.group(1)]
    return []


def fill_template(value: Any, fields: dict[str, Any]) -> Any:
    """The request body: each ``{{name}}`` string replaced by ``fields[name]`` (type-preserving)."""
    if isinstance(value, dict):
        return {key: fill_template(item, fields) for key, item in value.items()}
    if isinstance(value, list):
        return [fill_template(item, fields) for item in value]
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match is not None:
            return fields[match.group(1)]
    return value


class Timeouts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_seconds: float = Field(default=3.0, gt=0, le=30)
    read_seconds: float = Field(default=60.0, gt=0, le=600)
    probe_seconds: float = Field(default=5.0, gt=0, le=30)


class RetrySpec(BaseModel):
    """Retries apply ONLY to reads (health, discovery) and to chat calls carrying an idempotency key the gateway
    is declared to honour. Exponential backoff with jitter, bounded by attempts and the caller's deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=5)
    base_delay_seconds: float = Field(default=0.2, ge=0.01, le=5)
    max_delay_seconds: float = Field(default=2.0, ge=0.01, le=30)
    max_retry_after_seconds: float = Field(default=5.0, ge=0, le=60)


class CircuitSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_threshold: int = Field(default=3, ge=1, le=20)
    open_seconds: float = Field(default=30.0, ge=0.1, le=3600)


class RateLimitHeaders(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    retry_after: str = "retry-after"
    remaining: str | None = None
    reset: str | None = None

    @field_validator("retry_after", "remaining", "reset")
    @classmethod
    def header(cls, value: str | None) -> str | None:
        if value is not None and not _HEADER.fullmatch(value):
            raise ValueError("GATEWAY_HEADER_NAME_INVALID")
        return value


class IdempotencySpec(BaseModel):
    """``honoured`` is a declaration by the gateway's owner that a repeated request with the same key has no second
    side effect. Only then may PADOS re-send a chat call whose outcome is unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    header_name: str | None = None
    honoured: bool = False

    @model_validator(mode="after")
    def header_when_honoured(self) -> IdempotencySpec:
        if self.honoured and (self.header_name is None or not _HEADER.fullmatch(self.header_name)
                              or self.header_name.casefold() in _RESERVED_HEADERS):
            raise ValueError("GATEWAY_IDEMPOTENCY_HEADER_REQUIRED")
        return self


class GatewayPolicy(BaseModel):
    """What may be sent. Defaults are closed: PUBLIC data only and no execution (health and discovery only).
    RESTRICTED data is never released to a remote gateway, whatever is configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_classification: DataClassification = DataClassification.PUBLIC
    execution_enabled: bool = False

    @field_validator("max_classification")
    @classmethod
    def never_restricted(cls, value: DataClassification) -> DataClassification:
        if value is DataClassification.RESTRICTED:
            raise ValueError("GATEWAY_RESTRICTED_NEVER_RELEASED")
        return value


def gateway_base_url(value: str) -> str | None:
    """``https://`` (or ``http://`` on loopback only), no credentials, query or fragment; ``None`` means refused."""
    try:
        url = httpx.URL(value)
        port = url.port
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if url.userinfo or url.query or url.fragment or not url.host or (port is not None and not 1 <= port <= 65_535):
        return None
    if url.scheme == "https":
        return str(url).rstrip("/")
    if url.scheme == "http" and is_loopback_host(url.host):
        return str(url).rstrip("/")
    return None


def is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class GatewayContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = GATEWAY_CONTRACT_SCHEMA
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,39}$")
    display_name: str = Field(default="", max_length=80)
    base_url: str
    # Optional private CA bundle (absolute path) for a gateway with an internal CA. Verification is never disabled.
    tls_ca_file: str | None = Field(default=None, max_length=400)
    auth: GatewayAuth
    health: HealthSpec
    discovery: DiscoverySpec
    chat: ChatSpec
    timeouts: Timeouts = Field(default_factory=Timeouts)
    retry: RetrySpec = Field(default_factory=RetrySpec)
    circuit: CircuitSpec = Field(default_factory=CircuitSpec)
    rate_limit_headers: RateLimitHeaders = Field(default_factory=RateLimitHeaders)
    idempotency: IdempotencySpec = Field(default_factory=IdempotencySpec)
    max_context_tokens: int = Field(default=8192, ge=256, le=2_000_000)
    policy: GatewayPolicy = Field(default_factory=GatewayPolicy)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != GATEWAY_CONTRACT_SCHEMA:
            raise ValueError("GATEWAY_CONTRACT_SCHEMA_INVALID")
        return value

    @field_validator("base_url")
    @classmethod
    def secure_base_url(cls, value: str) -> str:
        validated = gateway_base_url(value)
        if validated is None:
            raise ValueError("GATEWAY_BASE_URL_MUST_BE_HTTPS")
        return validated

    @field_validator("tls_ca_file")
    @classmethod
    def absolute_ca(cls, value: str | None) -> str | None:
        if value is not None and ("\x00" in value or not Path(value).is_absolute()):
            raise ValueError("GATEWAY_TLS_CA_FILE_MUST_BE_ABSOLUTE")
        return value

    @property
    def is_loopback(self) -> bool:
        return is_loopback_host(httpx.URL(self.base_url).host)


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (str(key), *_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [value] if isinstance(value, str) else []


def parse_gateway_contract(document: object) -> GatewayContract:
    """Validate a contract document; any credential-looking value anywhere refuses the whole contract."""
    if not isinstance(document, dict):
        raise ContractError("GATEWAY_CONTRACT_INVALID")
    if any(looks_like_secret(text) for text in _strings(document)):
        raise ContractError("GATEWAY_CONTRACT_CONTAINS_SECRET")
    try:
        return GatewayContract.model_validate(document)
    except ValueError as exc:
        codes = re.findall(r"GATEWAY_[A-Z_]+|SECRET_REFERENCE_REQUIRED", str(exc))
        raise ContractError(codes[0] if codes else "GATEWAY_CONTRACT_INVALID") from None


def load_gateway_contract(path: str | Path) -> GatewayContract:
    """Read the private contract file (bounded size, JSON only)."""
    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size > MAX_CONTRACT_BYTES:
            raise ContractError("GATEWAY_CONTRACT_UNREADABLE")
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError("GATEWAY_CONTRACT_UNREADABLE") from None
    return parse_gateway_contract(document)
