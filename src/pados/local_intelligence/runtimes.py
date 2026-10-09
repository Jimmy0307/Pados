# SPDX-License-Identifier: AGPL-3.0-only
"""Local inference runtime discovery: what a runtime endpoint is, reports and supports (description only).

Runtime kinds are the Compute Node backend kinds (``pados.compute_node.backends.BACKEND_KINDS``); this module
adds no kind of its own and no authority. Discovery only probes LOOPBACK endpoints with bounded GETs; a remote
runtime reaches PADOS through Compute Node registration, never through discovery. A model a runtime lists is
``DISCOVERED`` and nothing more: it is not audited, benchmarked, registered, authorized or routable.

A missing or broken runtime is a described state (``UNREACHABLE``/``PROBE_FAILED``), never an exception, so
PADOS starts and plans without any local runtime.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.compute_node.artifact_identity import LocalArtifactIdentity
from pados.compute_node.backends import BACKEND_KINDS, LLAMA_CPP_NATIVE, BackendError, llama_cpp_server_state
from pados.connectivity.requirements import NetworkRequirement, requirement_for_endpoint
from pados.local_intelligence.probes import Basis, ProbeOutcome, ProbeRecord, ProbeSession, display_text
from pados.providers.discovery import DISCOVERY_MAX_BYTES, _bounded_local_response

RUNTIME_SCHEMA = "pados.local-intelligence.runtime.v1"
MAX_RUNTIME_MODELS = 200
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.-]{1,40})?$")
_DIGEST = re.compile(r"^(sha256:)?[0-9a-f]{12,64}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}$")


class RuntimeHealth(StrEnum):
    HEALTHY = "HEALTHY"
    UNREACHABLE = "UNREACHABLE"  # nothing answered (not installed, not running, refused)
    PROBE_FAILED = "PROBE_FAILED"  # answered, but not with a valid response of the expected kind
    NOT_PROBED = "NOT_PROBED"  # out of discovery scope (e.g. non-loopback endpoint)


class DiscoveryState(StrEnum):
    """The only lifecycle state discovery can assign. Later states are earned elsewhere, with evidence."""

    DISCOVERED = "DISCOVERED"


class FeatureSupport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supported: bool | None
    basis: Basis
    note: str | None = Field(default=None, max_length=160)


UNKNOWN_FEATURE = FeatureSupport(supported=None, basis=Basis.UNKNOWN)


class RuntimeModel(BaseModel):
    """A model as the runtime lists it. Size and labels are the runtime's own report (``UPSTREAM_FACT``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    digest: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    format: str | None = None
    state: DiscoveryState = DiscoveryState.DISCOVERED

    @field_validator("name")
    @classmethod
    def name_pattern(cls, value: str) -> str:
        if not _MODEL_NAME.fullmatch(value) or ".." in value:
            raise ValueError("RUNTIME_MODEL_NAME_INVALID")
        return value


class LoadedModel(BaseModel):
    """A model the runtime currently holds in memory (``/api/ps``-style report)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_MODEL_NAME.pattern)
    size_bytes: int | None = Field(default=None, ge=0)
    size_vram_bytes: int | None = Field(default=None, ge=0)
    context_length: int | None = Field(default=None, ge=1)


class RuntimeDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = RUNTIME_SCHEMA
    runtime_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    runtime_type: str
    endpoint: str = Field(max_length=200)
    network_requirement: NetworkRequirement
    local: bool
    health: RuntimeHealth
    failure_code: str | None = Field(default=None, max_length=120)
    version: str | None = None
    protocols: tuple[str, ...] = ()
    models: tuple[RuntimeModel, ...] = Field(default=(), max_length=MAX_RUNTIME_MODELS)
    loaded_models: tuple[LoadedModel, ...] = Field(default=(), max_length=MAX_RUNTIME_MODELS)
    streaming: FeatureSupport = UNKNOWN_FEATURE
    structured_output: FeatureSupport = UNKNOWN_FEATURE
    tool_use: FeatureSupport = UNKNOWN_FEATURE
    limits: dict[str, int] = Field(default_factory=dict)
    last_probe: datetime
    probes: tuple[ProbeRecord, ...] = ()
    basis: Basis = Basis.PADOS_MEASURED

    @field_validator("runtime_type")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in BACKEND_KINDS:
            raise ValueError("RUNTIME_TYPE_NOT_A_COMPUTE_NODE_BACKEND_KIND")
        return value

    @field_validator("version")
    @classmethod
    def version_pattern(cls, value: str | None) -> str | None:
        if value is not None and not _VERSION.fullmatch(value):
            raise ValueError("RUNTIME_VERSION_INVALID")
        return value

    @property
    def available(self) -> bool:
        return self.health is RuntimeHealth.HEALTHY


class FetchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Fetch = Callable[[str], bytes]  # GET a path on the runtime endpoint -> body (raises FetchError)


def loopback_endpoint(url: str) -> tuple[str, int] | None:
    """``(host, port)`` for a credential-free ``http://<loopback>:<port>`` URL, else None."""
    parsed = urlsplit(url)
    try:
        port = parsed.port or 0
    except ValueError:
        return None
    if (parsed.scheme != "http" or parsed.hostname not in _LOOPBACK or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not 1 <= port <= 65535):
        return None
    return parsed.hostname, port


def loopback_fetch(host: str, port: int) -> Fetch:
    """Bounded GET (size and deadline capped by ``pados.providers.discovery``) against a loopback endpoint."""

    def fetch(path: str) -> bytes:
        try:
            return _bounded_local_response(host, port, path)
        except TimeoutError:
            raise FetchError("TIMEOUT") from None
        except ConnectionRefusedError:
            raise FetchError("CONNECTION_REFUSED") from None
        except OSError as exc:
            text = str(exc)
            raise FetchError(f"HTTP_{text[5:8]}" if text.startswith("HTTP ") else "CONNECTION_FAILED") from None
        except ValueError:
            raise FetchError("RESPONSE_TOO_LARGE") from None

    return fetch


def _json(session: ProbeSession, probe_id: str, fetch: Fetch) -> tuple[dict[str, Any] | None, str | None]:
    started = session.clock()
    try:
        body = fetch(probe_id_path(probe_id))
    except FetchError as exc:
        outcome = ProbeOutcome.NOT_FOUND if exc.code in {"CONNECTION_REFUSED", "CONNECTION_FAILED"} else (
            ProbeOutcome.TIMED_OUT if exc.code == "TIMEOUT" else ProbeOutcome.FAILED)
        session.record(probe_id, outcome, started, code=exc.code)
        return None, exc.code
    if len(body) > DISCOVERY_MAX_BYTES:
        session.record(probe_id, ProbeOutcome.INVALID_OUTPUT, started, code="RESPONSE_TOO_LARGE")
        return None, "RESPONSE_TOO_LARGE"
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        session.record(probe_id, ProbeOutcome.INVALID_OUTPUT, started, code="RESPONSE_NOT_JSON", output=body)
        return None, "RESPONSE_NOT_JSON"
    if not isinstance(payload, dict):
        session.record(probe_id, ProbeOutcome.INVALID_OUTPUT, started, code="RESPONSE_NOT_OBJECT", output=body)
        return None, "RESPONSE_NOT_OBJECT"
    session.record(probe_id, ProbeOutcome.OK, started, output=body)
    return payload, None


_PROBE_PATHS = {
    "ollama.version": "/api/version", "ollama.tags": "/api/tags", "ollama.ps": "/api/ps",
    "openai.models": "/v1/models",
    "llama.models": "/v1/models", "llama.props": "/props",
}


def probe_id_path(probe_id: str) -> str:
    return _PROBE_PATHS[probe_id]


def _label(value: object) -> str | None:
    text = display_text(value, max_length=40)
    return text if text is not None and _LABEL.fullmatch(text) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool | None:
    if version is None:
        return None
    numbers = re.match(r"^([0-9]+(?:\.[0-9]+)*)", version)
    if numbers is None:
        return None
    parts = tuple(int(item) for item in numbers.group(1).split("."))
    return parts >= minimum


class RuntimeProbe(Protocol):
    runtime_type: str

    def describe(self, *, runtime_id: str, endpoint: str, fetch: Fetch, session: ProbeSession,
                 now: datetime) -> RuntimeDescriptor: ...


def _unreachable(runtime_id: str, runtime_type: str, endpoint: str, code: str, session: ProbeSession,
                 now: datetime, health: RuntimeHealth = RuntimeHealth.UNREACHABLE) -> RuntimeDescriptor:
    network = requirement_for_endpoint(endpoint)
    return RuntimeDescriptor(runtime_id=runtime_id, runtime_type=runtime_type, endpoint=endpoint,
                             network_requirement=network, local=network is NetworkRequirement.LOCAL_ONLY,
                             health=health, failure_code=code, last_probe=now, probes=tuple(session.records))


class OllamaRuntimeProbe:
    """Ollama's native API: ``/api/version``, ``/api/tags`` (installed models), ``/api/ps`` (loaded models).

    Runtime-level feature facts come from Ollama's published API (``UPSTREAM_FACT``): streaming responses,
    the ``format`` JSON-schema parameter (since 0.5.0) and the ``tools`` chat parameter (since 0.3.0). Whether a
    given MODEL honours them is a per-model measurement, not a runtime fact.
    """

    runtime_type = "ollama"

    def describe(self, *, runtime_id: str, endpoint: str, fetch: Fetch, session: ProbeSession,
                 now: datetime) -> RuntimeDescriptor:
        version_body, code = _json(session, "ollama.version", fetch)
        if version_body is None:
            health = RuntimeHealth.UNREACHABLE if code in {"CONNECTION_REFUSED", "CONNECTION_FAILED"} \
                else RuntimeHealth.PROBE_FAILED
            return _unreachable(runtime_id, self.runtime_type, endpoint, f"VERSION_{code}", session, now, health)
        raw_version = version_body.get("version")
        version = raw_version if isinstance(raw_version, str) and _VERSION.fullmatch(raw_version) else None
        if version is None:
            session.invalid("ollama.version", "VERSION_INVALID")
            return _unreachable(runtime_id, self.runtime_type, endpoint, "VERSION_INVALID", session, now,
                                RuntimeHealth.PROBE_FAILED)
        tags, code = _json(session, "ollama.tags", fetch)
        models: list[RuntimeModel] = []
        failure = None
        if tags is None or not isinstance(tags.get("models"), list):
            failure = f"TAGS_{code or 'MODELS_NOT_LIST'}"
            if tags is not None:
                session.invalid("ollama.tags", "MODELS_NOT_LIST")
        else:
            for item in tags["models"][:MAX_RUNTIME_MODELS]:
                model = _ollama_model(item)
                if model is not None:
                    models.append(model)
        loaded: list[LoadedModel] = []
        ps, _ = _json(session, "ollama.ps", fetch)
        if ps is not None and isinstance(ps.get("models"), list):
            for item in ps["models"][:MAX_RUNTIME_MODELS]:
                if isinstance(item, dict) and isinstance(item.get("name"), str) and _MODEL_NAME.fullmatch(item["name"]):
                    loaded.append(LoadedModel(name=item["name"], size_bytes=_int(item.get("size")),
                                              size_vram_bytes=_int(item.get("size_vram")),
                                              context_length=_int(item.get("context_length")) or None))
        network = requirement_for_endpoint(endpoint)
        structured = _version_at_least(version, (0, 5, 0))
        tools = _version_at_least(version, (0, 3, 0))
        return RuntimeDescriptor(
            runtime_id=runtime_id, runtime_type=self.runtime_type, endpoint=endpoint, network_requirement=network,
            local=network is NetworkRequirement.LOCAL_ONLY,
            health=RuntimeHealth.HEALTHY if failure is None else RuntimeHealth.PROBE_FAILED,
            failure_code=failure, version=version, protocols=("ollama", "openai_compatible"),
            models=tuple(sorted(models, key=lambda m: m.name)), loaded_models=tuple(sorted(loaded, key=lambda m: m.name)),
            streaming=FeatureSupport(supported=True, basis=Basis.UPSTREAM_FACT, note="Ollama API streams by default"),
            structured_output=FeatureSupport(supported=structured, basis=Basis.UPSTREAM_FACT,
                                             note="runtime accepts a JSON schema in 'format' from 0.5.0; per-model reliability is measured separately"),
            tool_use=FeatureSupport(supported=tools, basis=Basis.UPSTREAM_FACT,
                                    note="runtime accepts 'tools' from 0.3.0; only models with a tool template use them"),
            last_probe=now, probes=tuple(session.records))


def _ollama_model(item: object) -> RuntimeModel | None:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not _MODEL_NAME.fullmatch(item["name"]):
        return None
    raw_details = item.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    digest = item.get("digest")
    return RuntimeModel(
        name=item["name"], digest=digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else None,
        size_bytes=_int(item.get("size")), family=_label(details.get("family")),
        parameter_size=_label(details.get("parameter_size")), quantization=_label(details.get("quantization_level")),
        format=_label(details.get("format")))


class OpenAICompatibleRuntimeProbe:
    """Any OpenAI-compatible local server (llama.cpp server, vLLM, MLX servers): ``/v1/models`` only."""

    def __init__(self, runtime_type: str = "openai_compatible") -> None:
        self.runtime_type = runtime_type

    def describe(self, *, runtime_id: str, endpoint: str, fetch: Fetch, session: ProbeSession,
                 now: datetime) -> RuntimeDescriptor:
        body, code = _json(session, "openai.models", fetch)
        if body is None:
            health = RuntimeHealth.UNREACHABLE if code in {"CONNECTION_REFUSED", "CONNECTION_FAILED"} \
                else RuntimeHealth.PROBE_FAILED
            return _unreachable(runtime_id, self.runtime_type, endpoint, f"MODELS_{code}", session, now, health)
        data = body.get("data")
        if not isinstance(data, list):
            session.invalid("openai.models", "DATA_NOT_LIST")
            return _unreachable(runtime_id, self.runtime_type, endpoint, "MODELS_DATA_NOT_LIST", session, now,
                                RuntimeHealth.PROBE_FAILED)
        models = [RuntimeModel(name=item["id"]) for item in data[:MAX_RUNTIME_MODELS]
                  if isinstance(item, dict) and isinstance(item.get("id"), str) and _MODEL_NAME.fullmatch(item["id"])
                  and ".." not in item["id"]]
        network = requirement_for_endpoint(endpoint)
        return RuntimeDescriptor(
            runtime_id=runtime_id, runtime_type=self.runtime_type, endpoint=endpoint, network_requirement=network,
            local=network is NetworkRequirement.LOCAL_ONLY, health=RuntimeHealth.HEALTHY, protocols=("openai_compatible",),
            models=tuple(sorted(models, key=lambda m: m.name)), last_probe=now, probes=tuple(session.records))


class LlamaCppServerRuntimeProbe:
    """A llama.cpp server driven natively: ``/v1/models`` (served model ids) and ``/props`` (per-slot context window,
    model path, slots, build). Parsing is shared with the node adapter (``llama_cpp_server_state``).

    The server reports no content digest. A model's digest is reported ONLY when an operator-configured artifact
    root is set and the served file under it verifies (``LocalArtifactIdentity``: SHA-256 of the bytes, canonical
    manifest); otherwise it is ``None`` and a digest-pinned profile cannot bind (``MODEL_ARTIFACT_NOT_PRESENT``).
    Without ``/props`` the context window and build are unknown, so the runtime is ``PROBE_FAILED`` (never assumed).
    The served model is resident (the server loads it at start) and is reported with its context window."""

    runtime_type = LLAMA_CPP_NATIVE

    def __init__(self, artifact_root: str | None = None) -> None:
        self._identity = LocalArtifactIdentity(artifact_root) if artifact_root else None

    def describe(self, *, runtime_id: str, endpoint: str, fetch: Fetch, session: ProbeSession,
                 now: datetime) -> RuntimeDescriptor:
        body, code = _json(session, "llama.models", fetch)
        if body is None:
            health = RuntimeHealth.UNREACHABLE if code in {"CONNECTION_REFUSED", "CONNECTION_FAILED"} \
                else RuntimeHealth.PROBE_FAILED
            return _unreachable(runtime_id, self.runtime_type, endpoint, f"MODELS_{code}", session, now, health)
        props, props_code = _json(session, "llama.props", fetch)
        try:
            state = llama_cpp_server_state(body, props)
        except BackendError:
            session.invalid("llama.models", "DATA_NOT_LIST")
            return _unreachable(runtime_id, self.runtime_type, endpoint, "MODELS_DATA_NOT_LIST", session, now,
                                RuntimeHealth.PROBE_FAILED)
        names = sorted(name for name in state.model_ids if _MODEL_NAME.fullmatch(name) and ".." not in name)
        single = len(state.model_ids) == 1 and len(names) == 1
        artifact = self._identity.identify(state.model_path) if self._identity is not None and single else None
        models = tuple(RuntimeModel(name=name, digest=artifact.manifest_digest if artifact else None,
                                    size_bytes=artifact.size_bytes if artifact else None, format="gguf")
                       for name in names[:MAX_RUNTIME_MODELS])
        loaded = (LoadedModel(name=names[0], context_length=state.context_window),) if single and props else ()
        failure = None if props is not None else f"PROPS_{props_code}"
        if props is not None and state.context_window is None:
            failure = "PROPS_CONTEXT_WINDOW_UNKNOWN"
        limits = {key: value for key, value in (("context_window", state.context_window),
                                                ("parallel_slots", state.total_slots)) if value is not None}
        network = requirement_for_endpoint(endpoint)
        return RuntimeDescriptor(
            runtime_id=runtime_id, runtime_type=self.runtime_type, endpoint=endpoint, network_requirement=network,
            local=network is NetworkRequirement.LOCAL_ONLY,
            health=RuntimeHealth.HEALTHY if failure is None else RuntimeHealth.PROBE_FAILED, failure_code=failure,
            version=state.version, protocols=("openai_compatible", "llama_cpp_server"), models=models,
            loaded_models=loaded, limits=limits,
            streaming=FeatureSupport(supported=True, basis=Basis.UPSTREAM_FACT, note="server streams with stream=true"),
            structured_output=FeatureSupport(
                supported=True, basis=Basis.UPSTREAM_FACT,
                note="server accepts a JSON schema in response_format; per-model reliability is measured separately"),
            last_probe=now, probes=tuple(session.records))


RUNTIME_PROBES: Mapping[str, RuntimeProbe] = {
    "ollama": OllamaRuntimeProbe(),
    "openai_compatible": OpenAICompatibleRuntimeProbe(),
    "llama_cpp": OpenAICompatibleRuntimeProbe("llama_cpp"),
    LLAMA_CPP_NATIVE: LlamaCppServerRuntimeProbe(),
    "mlx_openai_compatible": OpenAICompatibleRuntimeProbe("mlx_openai_compatible"),
}


class RuntimeEndpoint(BaseModel):
    """One runtime endpoint to describe (from PADOS settings or operator configuration)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    runtime_type: str
    endpoint: str = Field(max_length=200)
    # Operator-configured directory of model files whose digest PADOS derives from the bytes (only for a runtime
    # that reports a model path but no digest). Read-only; absolute; never part of a descriptor.
    artifact_root: str | None = Field(default=None, max_length=400)

    @field_validator("runtime_type")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in BACKEND_KINDS:
            raise ValueError("RUNTIME_TYPE_NOT_A_COMPUTE_NODE_BACKEND_KIND")
        return value

    @model_validator(mode="after")
    def artifact_root_scope(self) -> RuntimeEndpoint:
        if self.artifact_root is not None:
            if self.runtime_type != LLAMA_CPP_NATIVE:
                raise ValueError("ARTIFACT_ROOT_UNSUPPORTED_FOR_RUNTIME_TYPE")
            if "\x00" in self.artifact_root or not Path(self.artifact_root).is_absolute():
                raise ValueError("ARTIFACT_ROOT_MUST_BE_ABSOLUTE")
        return self


def describe_runtime(target: RuntimeEndpoint, *, now: datetime, fetch: Fetch | None = None,
                     session: ProbeSession | None = None) -> RuntimeDescriptor:
    """Describe one runtime. Never raises for an absent/broken runtime; never probes beyond loopback."""
    session = session if session is not None else ProbeSession()
    if target.runtime_type not in RUNTIME_PROBES:
        return _unreachable(target.runtime_id, target.runtime_type, _safe_endpoint(target.endpoint),
                            "RUNTIME_TYPE_NOT_DISCOVERABLE", session, now, RuntimeHealth.NOT_PROBED)
    endpoint = _safe_endpoint(target.endpoint)
    loopback = loopback_endpoint(target.endpoint)
    if loopback is None:
        return _unreachable(target.runtime_id, target.runtime_type, endpoint, "DISCOVERY_LOOPBACK_ONLY", session, now,
                            RuntimeHealth.NOT_PROBED)
    probe = RUNTIME_PROBES[target.runtime_type]
    if target.artifact_root is not None:  # validated: only the runtime kind that needs a PADOS-derived digest
        probe = LlamaCppServerRuntimeProbe(target.artifact_root)
    return probe.describe(runtime_id=target.runtime_id, endpoint=endpoint,
                          fetch=fetch or loopback_fetch(*loopback), session=session, now=now)


def _safe_endpoint(url: str) -> str:
    """Endpoint for the record: scheme://host:port only (credentials, paths and queries are never recorded)."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = parsed.hostname or "invalid"
    host = f"[{host}]" if ":" in host else host
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "invalid"
    return f"{scheme}://{host}" + (f":{port}" if port else "")


def default_runtime_endpoints(ollama_base_url: str) -> tuple[RuntimeEndpoint, ...]:
    """The runtimes PADOS settings point at. Only Ollama has a settings binding today."""
    return (RuntimeEndpoint(runtime_id="ollama-local", runtime_type="ollama", endpoint=ollama_base_url),)
