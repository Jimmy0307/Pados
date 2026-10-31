# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 6: the store reapplies data-minimization to candidates on load.

A candidate row can satisfy the model while violating the persistence guard (for example a ``description``
carrying a credential-shaped value). The minimization check enforced on write is reapplied on load, so a
tampered candidate section is refused rather than returned as healthy.
"""
from __future__ import annotations

import json
from pathlib import Path

from loop_support import (
    CAPABILITY_ID,
    DISCOVERY_ADAPTER,
    DISCOVERY_CAPABILITY,
    FakeTransport,
    contract,
    grant,
    ok_results,
    target,
)

from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import DiscoveryRequest
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.store import LoopStore, LoopStoreError
from pados.enterprise.classification import DataClassification
import pytest


def _valid_store(tmp_path: Path) -> Path:
    path = tmp_path / "loop.json"
    store = LoopStore(path)
    request = DiscoveryRequest(request_id="req-1", project_id="proj-a", work_contract_id="wc-1",
                               goal_ref="goal-1", required_capability=CAPABILITY_ID, target_ref="target:app",
                               classification=DataClassification.INTERNAL)
    DiscoveryService(ReaDiscoveryProvider(lambda: FakeTransport(ok_results())), store,
                     capability=DISCOVERY_CAPABILITY, adapter=DISCOVERY_ADAPTER).discover(
        request, target=target(tmp_path), contract=contract(), grant=grant())
    return path


def test_credential_shaped_candidate_description_is_refused_on_load(tmp_path: Path) -> None:
    path = _valid_store(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    key = next(iter(data["candidates"]))
    data["candidates"][key]["description"] = "api_key=abcd1234efgh5678"  # model-valid, minimization-invalid
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LoopStoreError):
        LoopStore(path).candidates()


def test_valid_candidate_still_loads(tmp_path: Path) -> None:
    store = LoopStore(_valid_store(tmp_path))
    assert len(store.candidates()) == 1
