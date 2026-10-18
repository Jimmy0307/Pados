# SPDX-License-Identifier: AGPL-3.0-only
"""Round-16 regressions for the latest-head Codex P2 findings."""
from __future__ import annotations

from pathlib import Path

import pytest

from loop_support import DISCOVERY_ADAPTER, DISCOVERY_CAPABILITY, PROJECT, WORK, contract, grant, target

from pados.capability_loop.contracts import (
    DiscoveryOutcome,
    DiscoveryRequest,
    FailureCode,
    ProviderHealth,
    ProviderStatus,
)
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification
from pados.learning import store as learning_store
from pados.learning.store import JsonlFile

PROVIDER_ID = "rea.capability_discovery"


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        request_id="req-r16",
        project_id=PROJECT,
        work_contract_id=WORK,
        goal_ref="goal-r16",
        required_capability="BINARY_INSPECTION",
        target_ref="target:app",
        classification=DataClassification.INTERNAL,
    )


class _EvidenceFreeProvider:
    provider_id = PROVIDER_ID

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def discover(self, request: DiscoveryRequest, *, target_path: str) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            provider_id=self.provider_id,
            status=ProviderStatus.AVAILABLE,
            failure=None,
            evidence=(),
            capability_supported=True,
            partial=False,
        )


def test_supported_discovery_without_evidence_is_malformed(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loop.json")
    service = DiscoveryService(
        _EvidenceFreeProvider(), store, capability=DISCOVERY_CAPABILITY, adapter=DISCOVERY_ADAPTER
    )

    receipt = service.discover(_request(), target=target(tmp_path), contract=contract(), grant=grant())

    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    assert not receipt.accepted_into_loop
    assert receipt.state_after is None
    assert store.candidates() == {}


def test_first_jsonl_append_fsync_failure_removes_uncommitted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "new" / "log.jsonl"
    path.parent.mkdir(parents=True)
    file = JsonlFile(path)

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(learning_store, "fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="simulated directory fsync failure"):
        file.append_line({"event": "first"})

    assert not path.exists()
