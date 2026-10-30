# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 4: ReaDiscoveryProvider.discover() must enforce the expected runtime version itself.

health() already checks the version, but discover() may be called directly. An incompatible REA runtime
must not have its target opened or its discovery tool invoked, and no candidate evidence is accepted.
"""
from __future__ import annotations

from loop_support import FakeTransport, ok_results, session_document

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import (
    DiscoveryRequest,
    FailureCode,
    ProviderStatus,
)
from pados.enterprise.classification import DataClassification


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(request_id="req-1", project_id="proj-a", work_contract_id="wc-1",
                            goal_ref="goal-1", required_capability="BINARY_INSPECTION", target_ref="target:app",
                            classification=DataClassification.INTERNAL)


def test_discover_refuses_incompatible_runtime_without_opening_target() -> None:
    transport = FakeTransport(ok_results(session_document(version="6.4.0")))
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="9.9.9")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.DEGRADED
    assert outcome.failure is FailureCode.INCOMPATIBLE_VERSION
    assert outcome.runtime_version == "6.4.0"
    assert not outcome.evidence and outcome.capability_supported is False
    assert "open_binary" not in transport.calls  # target was never opened
    assert transport.closed


def test_discover_allows_matching_runtime_version() -> None:
    transport = FakeTransport(ok_results(session_document(version="6.4.0")))
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.AVAILABLE
    assert outcome.failure is None
    assert "open_binary" in transport.calls  # compatible runtime proceeds to open the target


def test_discover_without_expected_version_does_not_gate_on_version() -> None:
    transport = FakeTransport(ok_results(session_document(version="6.4.0")))
    provider = ReaDiscoveryProvider(lambda: transport)  # no expected version configured
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.AVAILABLE and outcome.failure is None
