# SPDX-License-Identifier: AGPL-3.0-only
"""REA capability-discovery adapter (external MCP runtime, morluto/rea; MIT).

REA is an optional, user-installed runtime reached over stdio MCP (``npx rea-agents mcp``). It is not
PADOS authority: this adapter only asks it what a target is and what can be observed, normalizes the
answer into ``DiscoveryOutcome`` / ``DiscoveryEvidence``, and records the provider's evidence digest.
REA-specific response schemas never leave this module.

Fail closed: not configured -> UNAVAILABLE; spawn/handshake/timeout/oversize/malformed -> DEGRADED with a
normalized ``FailureCode``. REA runs foreign code and is not a sandbox, so this adapter is reached only
after the PADOS discovery gate authorized the target; the gate, not REA, owns authorization.

REA marks target-derived content ``_meta.provenance.untrusted_content``; normalized evidence always carries
``untrusted_content=True`` regardless, because a discovery result is never an instruction to PADOS.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pados.capability_loop.contracts import (
    Confidence,
    DiscoveryEvidence,
    DiscoveryOutcome,
    DiscoveryRequest,
    FailureCode,
    ProviderHealth,
    ProviderStatus,
)
from pados.capability_loop.mcp_stdio import McpError, McpTransport
from pados.execution.context import canonical_hash

REA_PROVIDER_ID = "rea.capability_discovery"
SESSION_TOOL = "binary_session"
OPEN_TOOL = "open_binary"
DISCOVER_OPERATION = "INSPECT_RUNTIME_CATALOG"
DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_LIMIT = 16

# Normalized reasons a REA tool reports a capability as unavailable -> a BLOCKED-style limitation code.
_UNAVAILABLE_REASON = {
    "client_capability_missing", "target_required", "provider_missing", "provider_unavailable",
    "target_unsupported", "unsupported_host",
}


class ReaDiscoveryProvider:
    """``CapabilityDiscoveryProvider`` backed by a REA stdio MCP runtime."""

    provider_id = REA_PROVIDER_ID

    def __init__(self, transport_factory: Callable[[], McpTransport] | None, *,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 expected_version: str | None = None) -> None:
        # transport_factory is None when REA is not configured: the provider is then UNAVAILABLE.
        self._factory = transport_factory
        self._timeout = timeout_seconds
        self._expected_version = expected_version

    # -- health -------------------------------------------------------------
    def health(self) -> ProviderHealth:
        if self._factory is None:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.UNAVAILABLE,
                                  code=FailureCode.PROVIDER_NOT_CONFIGURED.value)
        try:
            transport = self._factory()
        except McpError as exc:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED, code=exc.code)
        try:
            session = self._session(transport)
            version = _runtime_version(session)
            if self._expected_version is not None and version is not None and version != self._expected_version:
                return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED,
                                      code=FailureCode.INCOMPATIBLE_VERSION.value, runtime_version=version)
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE,
                                  code=None, runtime_version=version)
        except McpError as exc:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED, code=exc.code)
        finally:
            _safe_close(transport)

    # -- discovery ----------------------------------------------------------
    def discover(self, request: DiscoveryRequest, *, target_path: str) -> DiscoveryOutcome:
        if self._factory is None:
            return self._failed(FailureCode.PROVIDER_NOT_CONFIGURED, ProviderStatus.UNAVAILABLE)
        try:
            transport = self._factory()
        except McpError as exc:
            return self._failed(_failure_for(exc), ProviderStatus.DEGRADED)
        try:
            session = self._session(transport)
            version = _runtime_version(session)
            self._open(transport, target_path)
            evidence_doc = transport.call_tool(SESSION_TOOL, {}, timeout_seconds=self._timeout)
            return self._normalize(evidence_doc, version)
        except McpError as exc:
            return self._failed(_failure_for(exc), ProviderStatus.DEGRADED)
        finally:
            _safe_close(transport)

    # -- internals ----------------------------------------------------------
    def _session(self, transport: McpTransport) -> Mapping[str, Any]:
        result = transport.call_tool(SESSION_TOOL, {}, timeout_seconds=self._timeout)
        session = _structured(result)
        if session is None:
            raise McpError("MCP_MALFORMED")
        return session

    def _open(self, transport: McpTransport, target_path: str) -> None:
        result = transport.call_tool(OPEN_TOOL, {"path": target_path}, timeout_seconds=self._timeout)
        if result.get("isError"):
            raise McpError("MCP_RPC_ERROR")

    def _normalize(self, result: Mapping[str, Any], version: str | None) -> DiscoveryOutcome:
        if result.get("isError"):
            return self._failed(FailureCode.PROVIDER_ERROR, ProviderStatus.DEGRADED, version)
        session = _structured(result)
        if session is None:
            return self._failed(FailureCode.MALFORMED_RESPONSE, ProviderStatus.DEGRADED, version)
        limitations: list[str] = []
        supported = False
        for group in _groups(session):
            if group.get("available") is True:
                supported = True
            else:
                reason = group.get("reason")
                if isinstance(reason, str) and reason in _UNAVAILABLE_REASON:
                    limitations.append(f"REA_{reason.upper()}")
        digest = canonical_hash(_canonical_session(session))
        evidence = DiscoveryEvidence(
            evidence_digest=digest,
            provider_evidence_ref=_evidence_ref(session),
            operation=DISCOVER_OPERATION,
            confidence=Confidence.OBSERVED,
            limitations=tuple(dict.fromkeys(limitations))[:_MAX_LIMIT],
            untrusted_content=True)
        return DiscoveryOutcome(
            provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, failure=None,
            evidence=(evidence,), limitations=tuple(dict.fromkeys(limitations))[:_MAX_LIMIT],
            capability_supported=supported, runtime_version=version, partial=False)

    def _failed(self, failure: FailureCode, status: ProviderStatus,
                version: str | None = None) -> DiscoveryOutcome:
        return DiscoveryOutcome(provider_id=self.provider_id, status=status, failure=failure,
                                evidence=(), limitations=(), capability_supported=False,
                                runtime_version=version, partial=False)


def _structured(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    content = result.get("structuredContent")
    if isinstance(content, Mapping):
        inner = content.get("result")
        if isinstance(inner, Mapping):
            return inner
        return content
    return None


def _groups(session: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    availability = session.get("tool_availability")
    if not isinstance(availability, Mapping):
        return []
    groups = availability.get("groups")
    if not isinstance(groups, list):
        return []
    return [group for group in groups if isinstance(group, Mapping)]


def _runtime_version(session: Mapping[str, Any]) -> str | None:
    identity = session.get("server_identity")
    if isinstance(identity, Mapping):
        package = identity.get("package")
        if isinstance(package, Mapping):
            version = package.get("version")
            if isinstance(version, str) and version:
                return version[:40]
    return None


def _evidence_ref(session: Mapping[str, Any]) -> str | None:
    identity = session.get("server_identity")
    if isinstance(identity, Mapping):
        catalog = identity.get("catalog")
        if isinstance(catalog, Mapping):
            digests = catalog.get("digests")
            if isinstance(digests, Mapping):
                combined = digests.get("combined_sha256")
                if isinstance(combined, str) and combined:
                    return combined[:96]
    return None


def _canonical_session(session: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded, provider-neutral projection of the session for the evidence digest."""
    groups = [{"available": bool(group.get("available")),
               "reason": group.get("reason") if isinstance(group.get("reason"), str) else None}
              for group in _groups(session)]
    return {"operation": DISCOVER_OPERATION, "version": _runtime_version(session),
            "evidence_ref": _evidence_ref(session), "groups": groups}


def _failure_for(exc: McpError) -> FailureCode:
    return {
        "MCP_SPAWN_FAILED": FailureCode.PROVIDER_ABSENT,
        "MCP_TIMEOUT": FailureCode.TIMEOUT,
        "MCP_CLOSED": FailureCode.CONNECTION_REFUSED,
        "MCP_MALFORMED": FailureCode.MALFORMED_RESPONSE,
        "MCP_TOO_LARGE": FailureCode.RESPONSE_TOO_LARGE,
        "MCP_RPC_ERROR": FailureCode.PROVIDER_ERROR,
    }.get(exc.code, FailureCode.PROVIDER_ERROR)


def _safe_close(transport: McpTransport) -> None:
    try:
        transport.close()
    except (McpError, OSError):
        pass
