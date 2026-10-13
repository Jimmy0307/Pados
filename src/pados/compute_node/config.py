# SPDX-License-Identifier: AGPL-3.0-only
"""Node-side configuration (``pados.compute-node.config.v1``)."""
from __future__ import annotations

import ipaddress
import platform as host_platform
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.compute_node.manifest import NodeCapability, NodeLimits
from pados.enterprise.classification import DataClassification
from pados.enterprise.compute import Accelerator, NodePlatform, ResourceClass
from pados.enterprise.secrets import is_secret_ref

NODE_CONFIG_SCHEMA = "pados.compute-node.config.v1"


class BackendConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,39}$")
    kind: str = Field(pattern=r"^(deterministic|openai_compatible|llama_cpp|llama_cpp_native|mlx_openai_compatible|ollama)$")
    endpoint: str | None = None
    credential_ref: str | None = None
    # Operator-configured directory of model files whose digest PADOS derives from the bytes (only for runtimes that
    # report a model path but no digest, i.e. ``llama_cpp_native``). Read-only; absolute.
    artifact_root: str | None = Field(default=None, max_length=400)

    @field_validator("endpoint")
    @classmethod
    def private_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validated = private_http_url(value)
        if validated is None:
            raise ValueError("BACKEND_ENDPOINT_MUST_BE_PRIVATE_WITHOUT_CREDENTIALS")
        return validated  # the client is built from this parsed form, not the raw string

    @field_validator("credential_ref")
    @classmethod
    def secret_ref(cls, value: str | None) -> str | None:
        if value is not None and not is_secret_ref(value):
            raise ValueError("SECRET_REFERENCE_REQUIRED")
        return value

    @model_validator(mode="after")
    def artifact_root_scope(self) -> BackendConfig:
        if self.artifact_root is not None:
            if self.kind != "llama_cpp_native":
                raise ValueError("ARTIFACT_ROOT_UNSUPPORTED_FOR_BACKEND_KIND")
            if "\x00" in self.artifact_root or not Path(self.artifact_root).is_absolute():
                raise ValueError("ARTIFACT_ROOT_MUST_BE_ABSOLUTE")
        return self


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    backend_model: str = Field(min_length=1, max_length=160)
    family: str
    version: str
    quantization: str | None = None
    context_limit_tokens: int = Field(ge=1)
    tasks: tuple[NodeCapability, ...] = Field(min_length=1)
    resource_class: ResourceClass
    privacy_class: DataClassification
    digest: str | None = None
    aliases: tuple[str, ...] = ()


class NodeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = NODE_CONFIG_SCHEMA
    node_name: str = Field(min_length=1, max_length=80)
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    allow_lan: bool = False
    advertised_endpoint: str | None = None
    state_dir: Path
    credential_ref: str
    privacy_class: DataClassification = DataClassification.INTERNAL
    resource_class: ResourceClass = ResourceClass.CPU_LIGHT
    accelerators: tuple[Accelerator, ...] = (Accelerator.CPU,)
    capabilities: tuple[NodeCapability, ...] = Field(min_length=1)
    limits: NodeLimits = Field(default_factory=NodeLimits)
    backends: tuple[BackendConfig, ...] = Field(min_length=1)
    models: tuple[ModelConfig, ...] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != NODE_CONFIG_SCHEMA:
            raise ValueError("NODE_CONFIG_SCHEMA_INVALID")
        return value

    @field_validator("credential_ref")
    @classmethod
    def credential(cls, value: str) -> str:
        if not is_secret_ref(value):
            raise ValueError("SECRET_REFERENCE_REQUIRED")
        return value

    @property
    def endpoint(self) -> str:
        return self.advertised_endpoint or f"http://{self.bind_host}:{self.port}"


# Explicit allowlist (RFC 1918, link-local, IPv6 ULA and link-local). ``ipaddress.is_private``
# is not used: it also admits 6to4/Teredo/NAT64 transition prefixes that embed public IPv4
# addresses, plus reserved/broadcast/benchmark ranges, and its membership differs across
# CPython patch releases. IPv4-mapped addresses are normalized only after the LAN opt-in check.
# The non-routable RFC 5737 / RFC 3849 documentation ranges stay accepted: shipped example
# registries use them as LAN placeholders.
_LAN_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))


def is_private_host(host: str, *, allow_lan: bool) -> bool:
    """Plain loopback always; mapped IPv4 and LAN ranges require ``allow_lan``.
    Wildcard binds and public addresses are refused: V1 is never exposed to the Internet."""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        if not allow_lan:
            return False
        address = address.ipv4_mapped
    if address.is_unspecified:
        return False
    if address.is_loopback:
        return True
    if not allow_lan:
        return False
    return any(address.version == net.version and address in net for net in _LAN_NETWORKS)


def private_http_url(value: str) -> str | None:
    """Parse ``value`` the way the HTTP client will and return it only if it is a private
    http(s) base URL: no userinfo, query or fragment, a valid optional port, and a host on the
    private allowlist. ``None`` means refused."""
    try:
        url = httpx.URL(value)
        port = url.port
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if url.scheme not in {"http", "https"} or url.userinfo or url.query or url.fragment or not url.host:
        return None
    if port is not None and not 1 <= port <= 65_535:
        return None
    if not is_private_host(url.host, allow_lan=True):
        return None
    return str(url)


def validate_bind(config: NodeConfig) -> None:
    if not is_private_host(config.bind_host, allow_lan=config.allow_lan):
        raise PermissionError("NODE_BIND_HOST_NOT_PRIVATE")


def detect_platform() -> tuple[NodePlatform, str]:
    system = host_platform.system().lower()
    mapped = {"windows": NodePlatform.WINDOWS, "darwin": NodePlatform.MACOS}.get(system, NodePlatform.LINUX)
    return mapped, (host_platform.machine() or "unknown").lower()
