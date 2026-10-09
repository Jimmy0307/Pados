# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Enterprise Capability Pack status for the existing PADOS doctor.

``PADOS_ENTERPRISE_CONFIG`` points to a JSON file declaring installed
capability manifests; unset means the extension is not configured, which never
affects Core readiness. Implementations are resolved only from the
``pados.enterprise`` package (no arbitrary imports from configuration), and
SecretRefs are checked for resolvability without exposing any value.
"""
from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pados.enterprise.manifest import CapabilityHealth, EnterpriseCapabilityManifest, ExecutionMode
from pados.enterprise.registry import CapabilityRegistry
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretResolver

CONFIG_SCHEMA = "pados.enterprise.config.v1"
_IMPLEMENTATION_PREFIX = "pados.enterprise."
_STATE = {
    CapabilityHealth.READY: "ENTERPRISE_CAPABILITY_READY",
    CapabilityHealth.NOT_CONFIGURED: "ENTERPRISE_CAPABILITY_NOT_CONFIGURED",
    CapabilityHealth.UNAVAILABLE: "ENTERPRISE_CAPABILITY_UNAVAILABLE",
    CapabilityHealth.UNAUTHORIZED: "ENTERPRISE_CAPABILITY_UNAUTHORIZED",
    CapabilityHealth.DEGRADED: "ENTERPRISE_CAPABILITY_DEGRADED",
}


def load_registry(config_path: str, *, resolver: SecretResolver | None = None) -> tuple[CapabilityRegistry, dict[str, Any]]:
    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError("ENTERPRISE_CONFIG_SCHEMA_INVALID")
    authorized_refs = set(raw.get("authorized_secret_refs", []))
    allow_remote = raw.get("allow_remote_egress") is True
    resolver = resolver or EnvironmentSecretResolver()
    registry = CapabilityRegistry()
    overrides: dict[str, tuple[CapabilityHealth, str]] = {}
    for entry in raw.get("capabilities", []):
        manifest = EnterpriseCapabilityManifest.model_validate(entry)
        implementation = _implementation(manifest.implementation)
        registry.register(manifest, implementation)
        refs = set(manifest.secret_refs)
        if manifest.execution_mode is ExecutionMode.REMOTE_HTTP and not allow_remote:
            overrides[manifest.capability_id] = (CapabilityHealth.UNAUTHORIZED, "REMOTE_EGRESS_NOT_APPROVED")
        elif not refs <= authorized_refs:
            overrides[manifest.capability_id] = (CapabilityHealth.UNAUTHORIZED, "SECRET_REFERENCE_NOT_AUTHORIZED")
        elif any(resolver.resolve(ref) is None for ref in sorted(refs)):
            overrides[manifest.capability_id] = (CapabilityHealth.UNAVAILABLE, "SECRET_REFERENCE_UNAVAILABLE")
    return registry, {"overrides": overrides}


def _implementation(reference: str | None) -> object | None:
    if not reference:
        return None
    module_name, _, symbol = reference.partition(":")
    if not module_name.startswith(_IMPLEMENTATION_PREFIX) or not symbol.isidentifier():
        raise ValueError("ENTERPRISE_IMPLEMENTATION_NOT_ALLOWED")
    try:
        factory = getattr(importlib.import_module(module_name), symbol)
        instance: object = factory()
        return instance
    except (ImportError, AttributeError, TypeError):
        return None


def enterprise_status(config_path: str, *, core_ready: bool, resolver: SecretResolver | None = None) -> dict[str, Any]:
    core = "CORE_READY" if core_ready else "CORE_NOT_READY"
    base = {"core": core, "core_readiness_affected": False}
    if not config_path:
        return {**base, "status": "ENTERPRISE_EXTENSION_NOT_CONFIGURED", "capabilities": []}
    try:
        registry, meta = load_registry(config_path, resolver=resolver)
    except (OSError, ValueError, ValidationError) as exc:
        plain = isinstance(exc, ValueError) and not isinstance(exc, (ValidationError, json.JSONDecodeError))
        error = str(exc) if plain else type(exc).__name__
        return {**base, "status": "ENTERPRISE_EXTENSION_INVALID", "reason": error[:128], "capabilities": []}
    capabilities = []
    for row in registry.describe():
        health = CapabilityHealth(row["health"])
        reason: str | None = None
        override = meta["overrides"].get(row["capability_id"])
        if override is not None:
            health, reason = override
        capabilities.append({**row, "health": health.value, "state": _STATE[health], "reason": reason})
    states = {row["health"] for row in capabilities}
    if not capabilities:
        status = "ENTERPRISE_EXTENSION_NOT_CONFIGURED"
    elif states == {CapabilityHealth.READY.value}:
        status = "ENTERPRISE_EXTENSION_READY"
    else:
        status = "ENTERPRISE_EXTENSION_DEGRADED"
    return {**base, "status": status, "capabilities": capabilities}
