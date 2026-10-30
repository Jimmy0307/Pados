# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 2: a pinned REA runtime that omits its version is incompatible, not allowed.

When ``expected_version`` is configured and the runtime omits/malforms its reported version, compatibility
cannot be established, so discovery must fail closed (INCOMPATIBLE_VERSION) without opening the target.
"""
from __future__ import annotations

from typing import Any

from loop_support import FakeTransport, ok_results

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import (
    DiscoveryRequest,
    FailureCode,
    ProviderStatus,
)
from pados.enterprise.classification import DataClassification


def _doc_without_version() -> dict[str, Any]:
    # A session document whose server_identity.package omits the version.
    return {"result": {"capabilities": {}, "tool_availability": {"groups": [{"names": ["x"], "available": True}]},
                       "server_identity": {"package": {"name": "rea-agents"},
                                           "catalog": {"digests": {"combined_sha256": "a" * 64}}}}}


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(request_id="req-1", project_id="proj-a", work_contract_id="wc-1",
                            goal_ref="goal-1", required_capability="BINARY_INSPECTION", target_ref="target:app",
                            classification=DataClassification.INTERNAL)


def test_discover_rejects_runtime_that_omits_version_when_pinned() -> None:
    transport = FakeTransport(ok_results(_doc_without_version()))
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.DEGRADED
    assert outcome.failure is FailureCode.INCOMPATIBLE_VERSION
    assert "open_binary" not in transport.calls  # target never opened


def test_health_reports_incompatible_when_version_missing_and_pinned() -> None:
    transport = FakeTransport(ok_results(_doc_without_version()))
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    health = provider.health()
    assert health.status is ProviderStatus.DEGRADED
    assert health.code == FailureCode.INCOMPATIBLE_VERSION.value


def test_unpinned_runtime_without_version_still_proceeds() -> None:
    transport = FakeTransport(ok_results(_doc_without_version()))
    provider = ReaDiscoveryProvider(lambda: transport)  # no expected_version
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.AVAILABLE and outcome.failure is None
