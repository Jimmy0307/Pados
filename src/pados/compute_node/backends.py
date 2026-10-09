# SPDX-License-Identifier: AGPL-3.0-only
"""Node-local inference backends.

The generic interface is an OpenAI-compatible local endpoint (llama.cpp
server, vLLM, MLX OpenAI-compatible servers, Ollama's ``/v1``). Small native
Ollama and llama.cpp-server adapters and a deterministic backend (no model;
reproducible tests and development nodes) implement the same protocol. No vendor or model name is
hard-coded: models come from node configuration and backend discovery.

Measured execution configuration: ``run`` optionally receives the generic ``ExecutionConfig`` the capability was
measured under (context window, reasoning mode, sampling, output budget, output constraint). Each adapter
translates it into its own request and refuses (``EXECUTION_CONFIG_UNVERIFIABLE:<field>``) any setting it cannot
express; the configuration it put into the runtime request is echoed in
``BackendOutput.runtime["execution_requested"]``. That echo proves what was REQUESTED, not what the runtime did:
behaviour that can be observed in the response is checked as well (a reasoning phase although it was disabled
-> ``EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode``; an answer cut off by the output budget ->
``OUTPUT_TRUNCATED``; no visible answer -> ``EMPTY_COMPLETION``). Without a configuration the adapters keep their
previous request shape.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from pados.compute_node.artifact_identity import LocalArtifactIdentity
from pados.compute_node.manifest import NodeCapability
from pados.corpus.ingestion import deterministic_embedding
from pados.enterprise.secrets import SecretValue, redact
from pados.resources.capability_profile import ExecutionConfig, OutputConstraint, ReasoningMode


class BackendError(RuntimeError):
    """Fail-closed backend error with a stable code (never provider text). ``detail`` holds only bounded,
    non-sensitive runtime counters and enum-like labels (e.g. ``done_reason``, token counts), never content."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})


_DETAIL_KEYS = ("done_reason", "eval_count", "prompt_eval_count", "thinking_chars", "content_chars",
                "runtime_context_window", "measured_context_window")
_DETAIL_LABEL = re.compile(r"^[a-z_]{1,32}$")


def safe_detail(values: dict[str, Any]) -> dict[str, Any]:
    """Counters and short lowercase labels only: no text produced by the model or the runtime."""
    detail: dict[str, Any] = {}
    for key in _DETAIL_KEYS:
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            detail[key] = value
        elif isinstance(value, str) and _DETAIL_LABEL.fullmatch(value):
            detail[key] = value
    return detail


@dataclass(frozen=True)
class DiscoveredModel:
    backend_model: str
    digest: str | None = None
    family: str | None = None
    quantization: str | None = None


@dataclass(frozen=True)
class BackendOutput:
    content: str
    runtime: dict[str, Any]


class InferenceBackend(Protocol):
    backend_id: str
    kind: str

    def health(self) -> bool: ...

    def discover(self) -> list[DiscoveredModel]: ...

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float,
            execution: ExecutionConfig | None = None) -> BackendOutput: ...


Messages = list[dict[str, str]]


def _messages(item: dict[str, Any]) -> Messages:
    raw = item.get("messages")
    if raw is None and isinstance(item.get("text"), str):
        raw = [{"role": "user", "content": item["text"]}]
    if not isinstance(raw, list) or not raw:
        raise BackendError("INPUT_INVALID")
    messages: Messages = []
    for message in raw:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"} \
                or not isinstance(message.get("content"), str):
            raise BackendError("INPUT_INVALID")
        messages.append({"role": str(message["role"]), "content": str(message["content"])})
    return messages


def _response_schema(item: dict[str, Any], execution: ExecutionConfig | None) -> dict[str, Any] | None:
    """The caller's JSON schema when the configuration demands schema-constrained output (required then)."""
    if execution is None or execution.output_constraint is OutputConstraint.UNCONSTRAINED:
        return None
    schema = item.get("response_schema")
    if not isinstance(schema, dict) or not schema:
        raise BackendError("INPUT_INVALID")
    return schema


def schema_sha256(schema: dict[str, Any]) -> str:
    """Identity of the caller's output schema (recorded; the measured condition binds the constraint KIND)."""
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _check_budget(max_output_tokens: int, execution: ExecutionConfig | None) -> None:
    if execution is not None and execution.max_output_tokens != max_output_tokens:
        raise BackendError("EXECUTION_CONFIG_BUDGET_CONFLICT")


_WORD = re.compile(r"[\w-]+", re.UNICODE)


class DeterministicBackend:
    """Reproducible development backend: no model weights, no network.

    LOCAL_INFERENCE returns a deterministic extractive completion, EMBEDDING
    uses the existing PADOS deterministic embedding, CLASSIFICATION applies the
    caller's keyword labels. ``simulate_latency_ms`` exists only here, for
    timeout and cancellation tests.
    """

    kind = "deterministic"
    MODEL = "pados-deterministic-v1"

    def __init__(self, backend_id: str = "deterministic") -> None:
        self.backend_id = backend_id

    def health(self) -> bool:
        return True

    def discover(self) -> list[DiscoveredModel]:
        return [DiscoveredModel(self.MODEL, digest=hashlib.sha256(self.MODEL.encode()).hexdigest()[:12],
                                family="pados-deterministic")]

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float,
            execution: ExecutionConfig | None = None) -> BackendOutput:
        if backend_model != self.MODEL:
            raise BackendError("MODEL_NOT_FOUND")
        _check_budget(max_output_tokens, execution)
        _response_schema(item, execution)  # no model, so every setting applies trivially; a schema is still required
        delay = float(item.get("simulate_latency_ms", 0)) / 1000
        if delay:
            time.sleep(min(delay, timeout_seconds + 1))
        if capability is NodeCapability.EMBEDDING:
            content = json.dumps({"embedding": deterministic_embedding(str(item.get("text", "")))})
        elif capability is NodeCapability.CLASSIFICATION:
            labels = item.get("labels")
            if not isinstance(labels, dict) or not labels:
                raise BackendError("INPUT_INVALID")
            words = {w.casefold() for w in _WORD.findall(str(item.get("text", "")))}
            scores = {str(label): len(words & {str(k).casefold() for k in keys})
                      for label, keys in sorted(labels.items()) if isinstance(keys, list)}
            best = max(sorted(scores), key=lambda label: scores[label])
            content = json.dumps({"label": best, "scores": scores}, sort_keys=True)
        elif capability is NodeCapability.LOCAL_INFERENCE:
            prompt = "\n".join(m["content"] for m in _messages(item) if m["role"] != "system")
            tokens = _WORD.findall(prompt)[:max_output_tokens]
            digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
            content = f"Deterministic local completion {digest}: " + " ".join(tokens)
        else:
            raise BackendError("CAPABILITY_UNSUPPORTED")
        runtime: dict[str, Any] = {"backend": self.kind, "backend_model": backend_model}
        if execution is not None:
            runtime["execution_requested"] = execution.model_dump(mode="json")
        return BackendOutput(content, runtime)


Transport = Callable[[str, str, dict[str, Any] | None, float], tuple[int, Any]]


def http_transport(credential: SecretValue | None = None) -> Transport:
    """Default JSON transport; the credential is used only inside the call."""

    def call(method: str, url: str, body: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
        headers = {"authorization": f"Bearer {credential.reveal()}"} if credential is not None else {}
        try:
            response = httpx.request(method, url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            raise BackendError("BACKEND_TIMEOUT") from None
        except httpx.HTTPError as exc:
            # Only the exception class is kept; messages may echo URLs or headers.
            raise BackendError(redact(f"ENDPOINT_FAILURE:{type(exc).__name__}")) from None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    return call


class OpenAICompatibleBackend:
    """Generic OpenAI-compatible local endpoint (``/v1/models``, ``/v1/chat/completions``,
    ``/v1/embeddings``)."""

    kind = "openai_compatible"

    def __init__(self, backend_id: str, endpoint: str, *, transport: Transport | None = None,
                 kind: str = "openai_compatible", probe_timeout: float = 3.0) -> None:
        self.backend_id = backend_id
        self.endpoint = endpoint.rstrip("/")
        self.kind = kind
        self._call = transport or http_transport()
        self._probe_timeout = probe_timeout

    def health(self) -> bool:
        try:
            status, _ = self._call("GET", f"{self.endpoint}/v1/models", None, self._probe_timeout)
        except BackendError:
            return False
        return status == 200

    def discover(self) -> list[DiscoveredModel]:
        status, body = self._call("GET", f"{self.endpoint}/v1/models", None, self._probe_timeout)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise BackendError("BACKEND_SCHEMA_INVALID")
        return [DiscoveredModel(str(m["id"])) for m in body["data"] if isinstance(m, dict) and m.get("id")]

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float,
            execution: ExecutionConfig | None = None) -> BackendOutput:
        if execution is not None:
            # The generic OpenAI-compatible API has no portable per-request context window or reasoning switch:
            # a measured configuration can be neither applied nor confirmed, so it is refused (never assumed).
            raise BackendError("EXECUTION_CONFIG_UNVERIFIABLE:context_window")
        if capability is NodeCapability.EMBEDDING:
            status, body = self._call("POST", f"{self.endpoint}/v1/embeddings",
                                      {"model": backend_model, "input": str(item.get("text", ""))}, timeout_seconds)
            _raise_for(status, body)
            try:
                vector = [float(v) for v in body["data"][0]["embedding"]]
            except (KeyError, IndexError, TypeError, ValueError):
                raise BackendError("BACKEND_SCHEMA_INVALID") from None
            return BackendOutput(json.dumps({"embedding": vector}), {"backend": self.kind, "backend_model": backend_model})
        if capability is not NodeCapability.LOCAL_INFERENCE:
            raise BackendError("CAPABILITY_UNSUPPORTED")
        status, body = self._call("POST", f"{self.endpoint}/v1/chat/completions", {
            "model": backend_model, "messages": _messages(item), "max_tokens": max_output_tokens,
            "temperature": 0, "stream": False}, timeout_seconds)
        _raise_for(status, body)
        try:
            content = str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise BackendError("BACKEND_SCHEMA_INVALID") from None
        if not content.strip():
            # e.g. a reasoning model that spent the whole budget before answering
            raise BackendError("EMPTY_COMPLETION")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return BackendOutput(content, {"backend": self.kind, "backend_model": backend_model,
                                       "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens")
                                                 if isinstance(usage.get(k), int)}})


def _raise_for(status: int, body: Any) -> None:
    if status == 200:
        return
    text = json.dumps(body).lower() if body is not None else ""
    if status == 404 or ("model" in text and "not found" in text):
        raise BackendError("MODEL_NOT_FOUND")
    raise BackendError(f"BACKEND_HTTP_{status}")


class OllamaBackend:
    """Native Ollama adapter (``/api/version``, ``/api/tags``, ``/api/chat``, ``/api/embed``).
    Optional: the OpenAI-compatible backend remains the generic interface."""

    kind = "ollama"

    def __init__(self, backend_id: str, endpoint: str, *, transport: Transport | None = None,
                 probe_timeout: float = 3.0) -> None:
        self.backend_id = backend_id
        self.endpoint = endpoint.rstrip("/")
        self._call = transport or http_transport()
        self._probe_timeout = probe_timeout

    def health(self) -> bool:
        try:
            status, body = self._call("GET", f"{self.endpoint}/api/version", None, self._probe_timeout)
        except BackendError:
            return False
        return status == 200 and isinstance(body, dict) and "version" in body

    def discover(self) -> list[DiscoveredModel]:
        status, body = self._call("GET", f"{self.endpoint}/api/tags", None, self._probe_timeout)
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("models"), list):
            raise BackendError("BACKEND_SCHEMA_INVALID")
        found = []
        for model in body["models"]:
            if not isinstance(model, dict) or not model.get("name"):
                continue
            raw_details = model.get("details")
            details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
            digest = str(model.get("digest") or "") or None
            found.append(DiscoveredModel(str(model["name"]), digest=digest, family=details.get("family"),
                                         quantization=details.get("quantization_level")))
        return found

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float,
            execution: ExecutionConfig | None = None) -> BackendOutput:
        if capability is NodeCapability.EMBEDDING:
            if execution is not None:
                raise BackendError("EXECUTION_CONFIG_UNVERIFIABLE:capability")
            status, body = self._call("POST", f"{self.endpoint}/api/embed",
                                      {"model": backend_model, "input": str(item.get("text", ""))}, timeout_seconds)
            _raise_for(status, body)
            try:
                vector = [float(v) for v in body["embeddings"][0]]
            except (KeyError, IndexError, TypeError, ValueError):
                raise BackendError("BACKEND_SCHEMA_INVALID") from None
            return BackendOutput(json.dumps({"embedding": vector}), {"backend": self.kind, "backend_model": backend_model})
        if capability is not NodeCapability.LOCAL_INFERENCE:
            raise BackendError("CAPABILITY_UNSUPPORTED")
        _check_budget(max_output_tokens, execution)
        status, body = self._call("POST", f"{self.endpoint}/api/chat", ollama_chat_body(
            backend_model, _messages(item), max_output_tokens=max_output_tokens, execution=execution,
            schema=_response_schema(item, execution)), timeout_seconds)
        _raise_for(status, body)
        try:
            message = body["message"]
            content = str(message["content"] or "")
            thinking = str(message.get("thinking") or "")
        except (KeyError, TypeError, AttributeError):
            raise BackendError("BACKEND_SCHEMA_INVALID") from None
        counters = {**body, "thinking_chars": len(thinking), "content_chars": len(content)}
        if execution is not None and execution.reasoning_mode is ReasoningMode.DISABLED and thinking:
            # a reasoning phase ran although the measured configuration disables it: the setting did not hold
            raise BackendError("EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode", safe_detail(counters))
        if not content.strip():
            raise BackendError("EMPTY_COMPLETION", safe_detail(counters))
        if execution is not None and body.get("done_reason") == "length":
            # the output budget ended the answer: a cut-off answer is not the measured capability's output
            raise BackendError("OUTPUT_TRUNCATED", safe_detail(counters))
        runtime: dict[str, Any] = {"backend": self.kind, "backend_model": backend_model, **safe_detail(
            {key: body.get(key) for key in ("done_reason", "eval_count", "prompt_eval_count")})}
        if isinstance(body.get("total_duration"), int):
            runtime["total_duration"] = body["total_duration"]
        if execution is not None:
            runtime["execution_requested"] = execution.model_dump(mode="json")
            schema = _response_schema(item, execution)
            if schema is not None:
                runtime["response_schema_sha256"] = schema_sha256(schema)
        return BackendOutput(content, runtime)


def ollama_chat_body(backend_model: str, messages: Messages, *, max_output_tokens: int,
                     execution: ExecutionConfig | None, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Generic execution configuration -> Ollama ``/api/chat`` request (the only place its field names appear).

    Field placement follows the installed runtime's API exactly as the benchmark that produced the evidence used
    it (``pados.local_intelligence.benchmark.OllamaClient.chat``): ``think`` and ``format`` at the top level;
    ``num_ctx``, ``num_predict``, ``temperature`` and ``seed`` under ``options``."""
    if execution is None:
        return {"model": backend_model, "messages": messages, "stream": False,
                "options": {"num_predict": max_output_tokens, "temperature": 0}}
    options: dict[str, Any] = {"num_ctx": execution.context_window, "num_predict": execution.max_output_tokens,
                               "temperature": execution.temperature}
    if execution.seed is not None:
        options["seed"] = execution.seed
    body: dict[str, Any] = {"model": backend_model, "messages": messages, "stream": False,
                            "think": execution.reasoning_mode is ReasoningMode.ENABLED, "options": options}
    if schema is not None:
        body["format"] = schema
    return body


LLAMA_CPP_NATIVE = "llama_cpp_native"
_BUILD = re.compile(r"^b([0-9]{1,7})(?:-([0-9a-f]{4,40}))?$")
_THINK_BLOCK = re.compile(r"<think>(.*?)(?:</think>|$)", re.DOTALL)


@dataclass(frozen=True)
class LlamaCppServerState:
    """What a llama.cpp server reports about itself (runtime-specific parsing lives here and in its runtime probe).

    Fields used (llama.cpp ``tools/server``; see docs/architecture/STAGE_C_MULTI_RUNTIME_FABRIC.md for which were
    verified against a real server and which are assumptions):
    ``GET /v1/models`` -> ``data[].id`` (the ``--alias``, else the model path);
    ``GET /props`` -> ``default_generation_settings.n_ctx`` (per-slot context window, fixed at server start),
    ``model_path``, ``total_slots``, ``build_info`` (``b<build>-<commit>``)."""

    model_ids: tuple[str, ...]
    context_window: int | None
    model_path: str | None
    version: str | None
    total_slots: int | None
    props_available: bool


def llama_cpp_version(build_info: object) -> str | None:
    """``b6123-1a2b3c4`` -> ``6123+1a2b3c4`` (a PADOS version label); anything else -> None (unknown)."""
    if not isinstance(build_info, str):
        return None
    match = _BUILD.fullmatch(build_info.strip())
    if match is None:
        return None
    return match.group(1) + (f"+{match.group(2)}" if match.group(2) else "")


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def llama_cpp_server_state(models: object, props: object) -> LlamaCppServerState:
    """Parse ``/v1/models`` (required) and ``/props`` (None when unavailable). Raises ``BACKEND_SCHEMA_INVALID``."""
    if not isinstance(models, dict) or not isinstance(models.get("data"), list):
        raise BackendError("BACKEND_SCHEMA_INVALID")
    ids = tuple(str(m["id"]) for m in models["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)
                and m["id"])
    if not isinstance(props, dict):
        return LlamaCppServerState(ids, None, None, None, None, False)
    settings = props.get("default_generation_settings")
    context = _positive_int(settings.get("n_ctx")) if isinstance(settings, dict) else None
    path = props.get("model_path")
    return LlamaCppServerState(ids, context, path if isinstance(path, str) and path else None,
                               llama_cpp_version(props.get("build_info")), _positive_int(props.get("total_slots")),
                               True)


class LlamaCppServerBackend:
    """Native llama.cpp server adapter (``llama-server``) that CAN honour a measured execution configuration.

    Translation (adapter-only; see ``llama_cpp_chat_body``): reasoning mode -> ``chat_template_kwargs.enable_thinking``,
    schema-constrained output -> ``response_format`` ``json_schema``, budget -> ``max_tokens``, sampling ->
    ``temperature``/``seed``. The context window is NOT a request parameter on this runtime: it is fixed when the
    server starts, so it is VERIFIED instead (``/props`` per-slot context == the measured window) immediately before
    and after the call; unknown -> ``EXECUTION_CONFIG_UNVERIFIABLE:context_window``, different ->
    ``EXECUTION_CONFIG_MISMATCH:context_window``, changed during the call -> ``RUNTIME_CHANGED_DURING_EXECUTION``. The
    server serves the model it was started with whatever ``model`` the request names, so the served model is
    verified too (``MODEL_NOT_FOUND``; several served models -> the window is not attributable -> UNVERIFIABLE).

    Observable checks: reasoning text (``reasoning_content`` or an inline ``<think>`` block) although reasoning was
    disabled -> ``EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode``; ``finish_reason=length`` -> ``OUTPUT_TRUNCATED``; no
    visible answer -> ``EMPTY_COMPLETION``.

    Artifact identity: the server reports a model PATH, not a digest. With an operator-configured ``artifact_root``
    the digest is derived from the file's bytes (``pados.compute_node.artifact_identity``); without one, or for any
    file it cannot verify, discovery reports no digest and a digest-pinned model is never available.

    The identity is re-verified for every BOUND call (red-team F-1), immediately before and after the call: the file
    the server reports (``/props`` ``model_path``) must lie inside ``artifact_root`` and its PADOS-derived manifest
    digest must equal the digest pinned for the requested model (``pinned_digests``, from node configuration). No
    identity source or no pin -> ``MODEL_ARTIFACT_NOT_PRESENT``; a file that cannot be verified (outside the root,
    unreadable, not GGUF) -> ``MODEL_ARTIFACT_UNVERIFIABLE``; another file -> ``MODEL_ARTIFACT_MISMATCH``; a change
    observed after the call -> ``RUNTIME_CHANGED_DURING_EXECUTION``. No output is returned in any of these cases."""

    kind = LLAMA_CPP_NATIVE

    def __init__(self, backend_id: str, endpoint: str, *, transport: Transport | None = None,
                 probe_timeout: float = 3.0, artifact_root: str | None = None,
                 pinned_digests: Mapping[str, str | None] | None = None) -> None:
        self.backend_id = backend_id
        self.endpoint = endpoint.rstrip("/")
        self._call = transport or http_transport()
        self._probe_timeout = probe_timeout
        self._identity = LocalArtifactIdentity(artifact_root) if artifact_root else None
        # backend model -> pinned manifest digest (None: declared without a pin, or with conflicting pins)
        self._pinned = dict(pinned_digests or {})

    def health(self) -> bool:
        try:
            status, body = self._call("GET", f"{self.endpoint}/health", None, self._probe_timeout)
        except BackendError:
            return False
        return status == 200 and isinstance(body, dict) and body.get("status") == "ok"

    def state(self) -> LlamaCppServerState:
        status, models = self._call("GET", f"{self.endpoint}/v1/models", None, self._probe_timeout)
        if status != 200:
            raise BackendError("BACKEND_SCHEMA_INVALID")
        try:
            props_status, props = self._call("GET", f"{self.endpoint}/props", None, self._probe_timeout)
        except BackendError:
            props_status, props = 0, None
        return llama_cpp_server_state(models, props if props_status == 200 else None)

    def discover(self) -> list[DiscoveredModel]:
        state = self.state()
        digest = None
        if self._identity is not None and len(state.model_ids) == 1:
            artifact = self._identity.identify(state.model_path)
            digest = artifact.manifest_digest if artifact is not None else None
        return [DiscoveredModel(name, digest=digest) for name in state.model_ids]

    def _verified_window(self, backend_model: str, execution: ExecutionConfig) -> tuple[str | None, int, str]:
        state = self.state()
        if backend_model not in state.model_ids:
            raise BackendError("MODEL_NOT_FOUND")
        if len(state.model_ids) != 1 or state.context_window is None:
            raise BackendError("EXECUTION_CONFIG_UNVERIFIABLE:context_window")
        if state.context_window != execution.context_window:
            raise BackendError("EXECUTION_CONFIG_MISMATCH:context_window", {
                "runtime_context_window": state.context_window, "measured_context_window": execution.context_window})
        return state.model_path, state.context_window, self._verified_artifact(backend_model, state.model_path)

    def _verified_artifact(self, backend_model: str, model_path: str | None) -> str:
        """The pinned manifest digest, returned only when the served file (inside ``artifact_root``) hashes to it."""
        pinned = self._pinned.get(backend_model)
        if self._identity is None or not pinned:
            raise BackendError("MODEL_ARTIFACT_NOT_PRESENT")
        artifact = self._identity.identify(model_path)
        if artifact is None:
            raise BackendError("MODEL_ARTIFACT_UNVERIFIABLE")
        if not hmac.compare_digest(artifact.manifest_digest.encode(), pinned.encode()):
            raise BackendError("MODEL_ARTIFACT_MISMATCH")
        return artifact.manifest_digest

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float,
            execution: ExecutionConfig | None = None) -> BackendOutput:
        if capability is NodeCapability.EMBEDDING:
            if execution is not None:
                raise BackendError("EXECUTION_CONFIG_UNVERIFIABLE:capability")
            return OpenAICompatibleBackend(self.backend_id, self.endpoint, transport=self._call, kind=self.kind).run(
                capability, backend_model, item, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds)
        if capability is not NodeCapability.LOCAL_INFERENCE:
            raise BackendError("CAPABILITY_UNSUPPORTED")
        _check_budget(max_output_tokens, execution)
        messages, schema = _messages(item), _response_schema(item, execution)
        served = self._verified_window(backend_model, execution) if execution is not None else None
        status, body = self._call("POST", f"{self.endpoint}/v1/chat/completions", llama_cpp_chat_body(
            backend_model, messages, max_output_tokens=max_output_tokens, execution=execution, schema=schema),
            timeout_seconds)
        _raise_for(status, body)
        try:
            choice = body["choices"][0]
            message = choice["message"]
            content = str(message.get("content") or "")
            thinking = str(message.get("reasoning_content") or "")
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BackendError("BACKEND_SCHEMA_INVALID") from None
        inline = "".join(_THINK_BLOCK.findall(content))
        visible = _THINK_BLOCK.sub("", content)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        counters = {"done_reason": finish, "eval_count": usage.get("completion_tokens"),
                    "prompt_eval_count": usage.get("prompt_tokens"),
                    "thinking_chars": len(thinking) + len(inline.strip()), "content_chars": len(visible)}
        if execution is not None and execution.reasoning_mode is ReasoningMode.DISABLED \
                and (thinking.strip() or inline.strip()):
            raise BackendError("EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode", safe_detail(counters))
        if not visible.strip():
            raise BackendError("EMPTY_COMPLETION", safe_detail(counters))
        if execution is not None and finish == "length":
            raise BackendError("OUTPUT_TRUNCATED", safe_detail(counters))
        runtime: dict[str, Any] = {"backend": self.kind, "backend_model": backend_model, **safe_detail(
            {key: counters[key] for key in ("done_reason", "eval_count", "prompt_eval_count")})}
        if execution is not None:
            try:
                after: tuple[str | None, int, str] | None =self._verified_window(backend_model, execution)
            except BackendError:
                after = None
            if served is None or after != served:
                raise BackendError("RUNTIME_CHANGED_DURING_EXECUTION")  # restarted, swapped or now unverifiable
            runtime["execution_requested"] = execution.model_dump(mode="json")
            runtime["context_window_verified"] = served[1]
            runtime["artifact_digest_verified"] = served[2]
            if schema is not None:
                runtime["response_schema_sha256"] = schema_sha256(schema)
        # an (empty) inline reasoning block is runtime framing, not part of the answer
        return BackendOutput(visible.strip() if "<think>" in content else content, runtime)


def llama_cpp_chat_body(backend_model: str, messages: Messages, *, max_output_tokens: int,
                        execution: ExecutionConfig | None, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Generic execution configuration -> llama.cpp server ``/v1/chat/completions`` request (the only place these
    field names appear). The context window has no request field on this runtime; the adapter verifies it."""
    if execution is None:
        return {"model": backend_model, "messages": messages, "max_tokens": max_output_tokens, "temperature": 0,
                "stream": False}
    body: dict[str, Any] = {
        "model": backend_model, "messages": messages, "stream": False, "max_tokens": execution.max_output_tokens,
        "temperature": execution.temperature,
        "chat_template_kwargs": {"enable_thinking": execution.reasoning_mode is ReasoningMode.ENABLED}}
    if execution.seed is not None:
        body["seed"] = execution.seed
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "pados_output", "schema": schema}}
    return body


def mlx_openai_compatible(backend_id: str, endpoint: str, *, transport: Transport | None = None) -> OpenAICompatibleBackend:
    """MLX_ADAPTER_CONTRACT_READY: a future Apple Silicon node exposes MLX models
    through an OpenAI-compatible server and is driven by the generic backend.
    Not live-validated in V1 (no Apple Silicon environment)."""
    return OpenAICompatibleBackend(backend_id, endpoint, transport=transport, kind="mlx_openai_compatible")


BACKEND_KINDS: dict[str, Callable[..., InferenceBackend]] = {
    "deterministic": lambda backend_id, **_: DeterministicBackend(backend_id),
    "openai_compatible": lambda backend_id, endpoint, **kw: OpenAICompatibleBackend(backend_id, endpoint, **kw),
    "mlx_openai_compatible": lambda backend_id, endpoint, **kw: mlx_openai_compatible(backend_id, endpoint, **kw),
    # llama.cpp server speaks the OpenAI-compatible API; the kind is a public label only.
    "llama_cpp": lambda backend_id, endpoint, **kw: OpenAICompatibleBackend(backend_id, endpoint, kind="llama_cpp", **kw),
    # The same server driven natively: verifies the context window and translates the measured configuration.
    LLAMA_CPP_NATIVE: lambda backend_id, endpoint, **kw: LlamaCppServerBackend(backend_id, endpoint, **kw),
    "ollama": lambda backend_id, endpoint, **kw: OllamaBackend(backend_id, endpoint, **kw),
}


def run_items(backend: InferenceBackend, capability: NodeCapability, backend_model: str,
              items: Sequence[dict[str, Any]], *, max_output_tokens: int, timeout_seconds: float,
              execution: ExecutionConfig | None = None) -> list[BackendOutput]:
    return [backend.run(capability, backend_model, item, max_output_tokens=max_output_tokens,
                        timeout_seconds=timeout_seconds, execution=execution) for item in items]
