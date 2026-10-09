# SPDX-License-Identifier: AGPL-3.0-only
"""Installed enterprise capabilities. Installed is not authorized."""
from __future__ import annotations

from typing import Any, Protocol

from pados.enterprise.manifest import CapabilityHealth, CapabilityType, EnterpriseCapabilityManifest


class HealthReporting(Protocol):
    def health(self) -> CapabilityHealth: ...


class CapabilityRegistry:
    def __init__(self) -> None:
        self._manifests: dict[str, EnterpriseCapabilityManifest] = {}
        self._implementations: dict[str, object] = {}

    def register(self, manifest: EnterpriseCapabilityManifest, implementation: object | None = None) -> None:
        if manifest.capability_id in self._manifests:
            raise ValueError("CAPABILITY_ALREADY_REGISTERED")
        self._manifests[manifest.capability_id] = manifest
        if implementation is not None:
            self._implementations[manifest.capability_id] = implementation

    def get(self, capability_id: str) -> EnterpriseCapabilityManifest:
        try:
            return self._manifests[capability_id]
        except KeyError as exc:
            raise PermissionError("CAPABILITY_NOT_INSTALLED") from exc

    def implementation(self, capability_id: str) -> object:
        self.get(capability_id)
        try:
            return self._implementations[capability_id]
        except KeyError as exc:
            raise PermissionError("CAPABILITY_IMPLEMENTATION_UNAVAILABLE") from exc

    def manifests(self) -> list[EnterpriseCapabilityManifest]:
        return [self._manifests[key] for key in sorted(self._manifests)]

    def mcp_capabilities(self) -> frozenset[str]:
        return frozenset(m.grant_capability for m in self._manifests.values()
                         if m.capability_type is CapabilityType.MCP_TOOL)

    def health(self, capability_id: str) -> CapabilityHealth:
        manifest = self.get(capability_id)
        implementation = self._implementations.get(capability_id)
        if implementation is None:
            return CapabilityHealth.UNAVAILABLE
        probe = getattr(implementation, "health", None)
        observed = probe() if callable(probe) else CapabilityHealth.READY
        if not isinstance(observed, CapabilityHealth):
            return CapabilityHealth.UNAVAILABLE
        # A manifest may declare itself degraded; it can never upgrade the probe.
        if manifest.health is CapabilityHealth.DEGRADED and observed is CapabilityHealth.READY:
            return CapabilityHealth.DEGRADED
        return observed

    def describe(self) -> list[dict[str, Any]]:
        return [{
            "capability_id": m.capability_id, "capability_type": m.capability_type.value,
            "version": m.version, "execution_mode": m.execution_mode.value,
            "max_classification": m.max_classification.value,
            "health": self.health(m.capability_id).value,
            "secret_refs": list(m.secret_refs),
        } for m in self.manifests()]
