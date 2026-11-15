# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-only provider setup (``pados.providers.setup.v1``): connection settings and narrowing policy per provider.

One private JSON file under the runtime directory (``PADOS_PROVIDERS_SETUP``, default ``runtime/local/providers.json``)
holds, per provider: kind, enabled flag, endpoint (local runtimes) or private gateway contract path, timeout, context
window, the capability requirement it is meant for, project scope, data classification ceiling, resource limits and the
NAME of the environment variable that holds its key. It never holds a key value: a credential-looking value anywhere
refuses the write. It is written atomically with owner-only permissions under the store lock, with an optimistic
``revision`` so two editors never overwrite each other.

This file is not a model catalog, router or authority. Models and measured capabilities stay in the operator
``ModelCatalog``; authorization stays with the compute-node registrations. The setup can only NARROW: a disabled
provider or a project outside its scope is refused by the consumer binding
(``pados.providers.execution_binding``), and nothing here can make a model usable.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pados.compute_node.config import private_http_url
from pados.connectivity.storage import atomic_write_json, store_lock
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import looks_like_secret

SETUP_SCHEMA = "pados.providers.setup.v1"
MAX_SETUP_BYTES = 256 * 1024
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
LOCAL_KINDS = ("ollama", "llama_cpp", "llama_cpp_native", "openai_compatible", "mlx_openai_compatible")
GATEWAY_KIND = "enterprise_gateway"
ProviderKind = Literal["ollama", "llama_cpp", "llama_cpp_native", "openai_compatible", "mlx_openai_compatible",
                       "enterprise_gateway"]
Requirement = Literal["STRUCTURED_OUTPUT", "TEXT_SUMMARY"]


class SetupError(Exception):
    """A refused setup change: one closed code and its HTTP status."""

    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class ResourceLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_calls: int = Field(default=1, ge=1, le=16)
    max_output_tokens: int = Field(default=1024, ge=1, le=32_768)


class ProviderSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,39}$")
    kind: ProviderKind
    display_name: str = Field(default="", max_length=80)
    enabled: bool = False
    endpoint: str | None = Field(default=None, max_length=200)
    contract_path: str | None = Field(default=None, max_length=400)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    context_window: int | None = Field(default=None, ge=256, le=2_000_000)
    capability_requirement: Requirement | None = None
    project_ids: tuple[str, ...] = Field(default=(), max_length=64)
    max_classification: DataClassification = DataClassification.PUBLIC
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    auth_env: str | None = None  # the NAME of an environment variable; resolved as secretref://env/<NAME>

    @field_validator("auth_env")
    @classmethod
    def env_name(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME.fullmatch(value):
            raise ValueError("PROVIDER_AUTH_ENV_NAME_INVALID")
        return value

    @field_validator("project_ids")
    @classmethod
    def projects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"\*|[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", item) for item in value):
            raise ValueError("PROVIDER_PROJECT_ID_INVALID")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def kind_fields(self) -> ProviderSettings:
        if self.kind == GATEWAY_KIND:
            if self.contract_path is None or "\x00" in self.contract_path or not Path(self.contract_path).is_absolute():
                raise ValueError("PROVIDER_GATEWAY_CONTRACT_PATH_REQUIRED")
            if self.endpoint is not None or self.auth_env is not None:
                raise ValueError("PROVIDER_GATEWAY_ENDPOINT_AND_AUTH_BELONG_TO_THE_CONTRACT")
            if self.max_classification is DataClassification.RESTRICTED:
                raise ValueError("PROVIDER_RESTRICTED_NEVER_RELEASED")
            return self
        if self.contract_path is not None:
            raise ValueError("PROVIDER_CONTRACT_PATH_ONLY_FOR_GATEWAY")
        if self.endpoint is None or private_http_url(self.endpoint) is None:
            raise ValueError("PROVIDER_ENDPOINT_MUST_BE_PRIVATE")
        return self

    @property
    def auth_ref(self) -> str | None:
        return f"secretref://env/{self.auth_env}" if self.auth_env else None

    def admits_project(self, project_id: str) -> bool:
        return "*" in self.project_ids or project_id in self.project_ids


class ProvidersSetup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SETUP_SCHEMA
    revision: int = Field(default=0, ge=0)
    providers: tuple[ProviderSettings, ...] = Field(default=(), max_length=32)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != SETUP_SCHEMA:
            raise ValueError("PROVIDERS_SETUP_SCHEMA_INVALID")
        return value

    @model_validator(mode="after")
    def unique(self) -> ProvidersSetup:
        ids = [item.provider_id for item in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("PROVIDER_ID_DUPLICATE")
        return self

    def get(self, provider_id: str) -> ProviderSettings | None:
        return next((item for item in self.providers if item.provider_id == provider_id), None)


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (str(key), *_strings(item))]
    if isinstance(value, list | tuple):
        return [s for item in value for s in _strings(item)]
    return [value] if isinstance(value, str) else []


def _closed_code(exc: ValidationError) -> str:
    codes = re.findall(r"PROVIDERS?_[A-Z_]+", str(exc))
    return codes[0] if codes else "PROVIDER_SETTINGS_INVALID"


def parse_settings(document: dict[str, Any]) -> ProviderSettings:
    if any(looks_like_secret(text) for text in _strings(document)):
        raise SetupError("PROVIDER_SETTINGS_CONTAIN_KEY_VALUE")
    try:
        return ProviderSettings.model_validate(document)
    except ValidationError as exc:
        raise SetupError(_closed_code(exc)) from None


class ProvidersSetupStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> ProvidersSetup:
        """The persisted setup; a missing file is an empty setup, anything unreadable fails closed."""
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return ProvidersSetup()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETUP_BYTES:
            raise SetupError("PROVIDERS_SETUP_INVALID", 503)
        try:
            document = json.loads(self.path.read_bytes())
            if not isinstance(document, dict) or any(looks_like_secret(text) for text in _strings(document)):
                raise ValueError("PROVIDERS_SETUP_INVALID")
            return ProvidersSetup.model_validate(document)
        except (OSError, UnicodeDecodeError, ValueError):
            raise SetupError("PROVIDERS_SETUP_INVALID", 503) from None

    def update(self, expected_revision: int, change: Callable[[ProvidersSetup], ProvidersSetup]) -> ProvidersSetup:
        """Reload, check the revision, apply ``change`` and persist atomically as one locked transaction."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with store_lock(self.path):
            current = self.load()
            if current.revision != expected_revision:
                raise SetupError("PROVIDERS_SETUP_REVISION_CONFLICT", 409)
            changed = change(current)
            updated = ProvidersSetup(revision=current.revision + 1, providers=changed.providers)
            atomic_write_json(self.path, updated.model_dump(mode="json"))
            return updated


def upsert(setup: ProvidersSetup, settings: ProviderSettings) -> ProvidersSetup:
    others = tuple(item for item in setup.providers if item.provider_id != settings.provider_id)
    return setup.model_copy(update={"providers": (*others, settings)})


def set_enabled(setup: ProvidersSetup, provider_id: str, enabled: bool) -> ProvidersSetup:
    current = setup.get(provider_id)
    if current is None:
        raise SetupError("PROVIDER_NOT_CONFIGURED", 404)
    return upsert(setup, current.model_copy(update={"enabled": enabled}))
