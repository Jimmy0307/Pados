# SPDX-License-Identifier: AGPL-3.0-only
"""Round-8: REA rechecks the version in the POST-OPEN evidence document, not just the first session call.

A runtime that restarts between the first session call and the post-open evidence call could return
incompatible evidence; discovery must reject it and never label evidence with a stale version.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loop_support import session_document

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import (
    DiscoveryRequest,
    FailureCode,
    ProviderStatus,
)
from pados.enterprise.classification import DataClassification


def _doc_without_version() -> dict[str, Any]:
    return {"result": {"capabilities": {}, "tool_availability": {"groups": [{"names": ["x"], "available": True}]},
                       "server_identity": {"package": {"name": "rea-agents"},
                                           "catalog": {"digests": {"combined_sha256": "a" * 64}}}}}


class _RestartTransport:
    """Returns successive session documents for successive SESSION_TOOL calls (a runtime restart mid-session)."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._i = 0
        self.calls: list[str] = []
        self.closed = False

    def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        self.calls.append(name)
        if name == "open_binary":
            return {"structuredContent": {"result": {}}}
        doc = self._docs[min(self._i, len(self._docs) - 1)]
        self._i += 1
        return {"structuredContent": doc}

    def close(self) -> None:
        self.closed = True


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(request_id="req-1", project_id="proj-a", work_contract_id="wc-1",
                            goal_ref="goal-1", required_capability="BINARY_INSPECTION", target_ref="target:app",
                            classification=DataClassification.INTERNAL)


def test_post_open_incompatible_version_is_rejected() -> None:
    transport = _RestartTransport([session_document(version="6.4.0"), session_document(version="9.9.9")])
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.failure is FailureCode.INCOMPATIBLE_VERSION
    assert outcome.runtime_version == "9.9.9"  # labelled with the version actually in the evidence document


def test_post_open_version_change_rejected_even_when_unpinned() -> None:
    transport = _RestartTransport([session_document(version="6.4.0"), session_document(version="9.9.9")])
    provider = ReaDiscoveryProvider(lambda: transport)  # no pin
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.failure is FailureCode.INCOMPATIBLE_VERSION


def test_post_open_missing_version_rejected_when_pinned() -> None:
    transport = _RestartTransport([session_document(version="6.4.0"), _doc_without_version()])
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.failure is FailureCode.INCOMPATIBLE_VERSION


def test_consistent_version_across_both_documents_is_accepted() -> None:
    transport = _RestartTransport([session_document(version="6.4.0"), session_document(version="6.4.0")])
    provider = ReaDiscoveryProvider(lambda: transport, expected_version="6.4.0")
    outcome = provider.discover(_request(), target_path="/authorized/app.bin")
    assert outcome.status is ProviderStatus.AVAILABLE and outcome.failure is None
    assert outcome.runtime_version == "6.4.0"
