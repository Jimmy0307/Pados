# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import http.client
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from pados.config import Settings

MOCK_MODEL_CHOICES = ("mock", "mock-review-alt")

DISCOVERY_TIMEOUT_SECONDS = 5.0
DISCOVERY_MAX_BYTES = 1_000_000


def _bounded_local_response(host: str, port: int, path: str = "/api/tags") -> bytes:
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    connection = http.client.HTTPConnection(host, port, timeout=1.0)
    try:
        try:
            connection.connect()
        except TimeoutError:
            # Nothing accepted the loopback connection in time. Windows retries a refused loopback SYN for ~2 s
            # instead of failing at once, so this is "nothing answered", not a slow server.
            raise ConnectionError("loopback connect timed out") from None
        connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status >= 400:
            raise OSError(f"HTTP {response.status}")
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > DISCOVERY_MAX_BYTES):
            raise ValueError("invalid discovery response size")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("discovery deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(min(1.0, remaining))
            chunk = response.read(min(65_536, DISCOVERY_MAX_BYTES - total + 1))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > DISCOVERY_MAX_BYTES:
                raise ValueError("discovery response too large")
            chunks.append(chunk)
    finally:
        connection.close()


def discover_ollama_models(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(base_url)
    try:
        port = parsed.port or 11434
    except ValueError:
        port = 0
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or bool(parsed.query)
        or bool(parsed.fragment)
        or not 1 <= port <= 65535
    ):
        return {
            "configured": True,
            "available": False,
            "models": [],
            "reason": "OLLAMA_LOCAL_ENDPOINT_REQUIRED",
        }
    try:
        if transport is None:
            content = _bounded_local_response(parsed.hostname, port)
        else:
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            with httpx.Client(timeout=3.0, transport=transport, trust_env=False) as client:
                response = client.get(f"http://{host}:{port}/api/tags")
                response.raise_for_status()
                content = response.content
                if len(content) > DISCOVERY_MAX_BYTES:
                    raise ValueError("discovery response too large")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise TypeError("discovery response must be an object")
        raw_models = payload.get("models", [])
        if not isinstance(raw_models, list):
            raise TypeError("models must be a list")
    except (httpx.HTTPError, OSError, TimeoutError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
        return {
            "configured": True,
            "available": False,
            "models": [],
            "reason": f"DISCOVERY_FAILED_{type(exc).__name__.upper()}",
        }
    models = [
        str(item.get("name", ""))[:200]
        for item in raw_models[:100]
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    return {
        "configured": True,
        "available": bool(models),
        "models": models,
        "reason": None if models else "NO_MODELS_REPORTED",
    }


def ai_runtime_catalog(settings: Settings, *, discover: bool) -> dict[str, Any]:
    definitions = [
        ("thinking", settings.chat_provider, settings.chat_model),
        ("fast", settings.fast_provider, settings.fast_model),
        ("embedding", settings.embed_provider, settings.embed_model),
        ("executor", settings.executor_provider, settings.executor_model),
    ]
    slots: list[dict[str, Any]] = []
    provider_discovery: dict[str, dict[str, Any]] = {}
    ollama_discovery = (
        discover_ollama_models(settings.ollama_base_url)
        if discover and any(provider.strip().casefold() == "ollama" for _, provider, _ in definitions)
        else None
    )
    for slot_id, provider, model in definitions:
        normalized = provider.strip().casefold()
        enabled = bool(normalized)
        configured = enabled and (normalized == "mock" or bool(model.strip()))
        discovery: dict[str, Any] = {
            "configured": configured,
            "available": True if normalized == "mock" else None,
            "models": list(MOCK_MODEL_CHOICES)
            if normalized == "mock"
            else ([model] if model else []),
            "reason": None if normalized == "mock" else "DISCOVERY_NOT_REQUESTED",
        }
        if normalized == "ollama" and discover:
            discovery = dict(ollama_discovery or {})
            configured = bool(model.strip())
            discovery["configured"] = configured
        slot_available = discovery["available"]
        slot_reason = discovery["reason"]
        if normalized == "ollama" and discover and configured:
            slot_available = model in discovery.get("models", [])
            if not slot_available:
                slot_reason = "CONFIGURED_MODEL_NOT_DISCOVERED"
        if normalized:
            provider_discovery[normalized] = discovery
        slots.append(
            {
                "id": slot_id,
                "enabled": enabled,
                "provider": normalized,
                "model": model,
                "configured": configured,
                "available": slot_available,
                "validated": slot_available if discover or normalized == "mock" else None,
                "source": "PADOS settings environment binding",
                "last_validation_result": slot_reason or "AVAILABLE",
            }
        )
    return {"slots": slots, "providers": provider_discovery, "sensitive_config": "MASKED_NOT_EXPOSED"}
