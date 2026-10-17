# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 8: the store must validate every persisted section atomically before reporting healthy.

Loading validates the top-level document, the container types, every receipt (and the hash chain), every
capability candidate (including key/id agreement) and every progress row. Any corrupt or tampered section
fails closed with ``LoopStoreError``; the store never partially recovers or silently rewrites bad state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from loop_learning_support import (
    ENDPOINT,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    binding_authority,
    generate_ok,
    learning_request,
)
from loop_learning_support import contract as l_contract
from loop_learning_support import grant as l_grant
from loop_support import (
    DISCOVERY_ADAPTER,
    DISCOVERY_CAPABILITY,
    CAPABILITY_ID,
    FakeTransport,
    contract,
    grant,
    ok_results,
    target,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.adapters.rea_mcp import ReaDiscoveryProvider
from pados.capability_loop.contracts import DiscoveryRequest
from pados.capability_loop.discovery import DiscoveryService
from pados.capability_loop.learning import LearningService
from pados.capability_loop.store import LoopStore, LoopStoreError
from pados.enterprise.classification import DataClassification


def _valid_store(tmp_path: Path) -> Path:
    path = tmp_path / "loop.json"
    store = LoopStore(path)
    request = DiscoveryRequest(request_id="req-1", project_id="proj-a", work_contract_id="wc-1",
                               goal_ref="goal-1", required_capability=CAPABILITY_ID, target_ref="target:app",
                               classification=DataClassification.INTERNAL)
    DiscoveryService(ReaDiscoveryProvider(lambda: FakeTransport(ok_results())), store,
                     capability=DISCOVERY_CAPABILITY, adapter=DISCOVERY_ADAPTER).discover(
        request, target=target(tmp_path), contract=contract(), grant=grant())
    LearningService(OpenMaicLearningProvider(ENDPOINT, FakeHttp({**generate_ok()})), store,
                    capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=binding_authority()).create_session(
        learning_request(), contract=l_contract(), grant=l_grant(), classification=DataClassification.INTERNAL)
    return path


def _corrupt(path: Path, mutate) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    corrupt = path.with_name("corrupt.json")
    corrupt.write_text(json.dumps(data), encoding="utf-8")
    return corrupt


def test_valid_store_loads(tmp_path: Path) -> None:
    store = LoopStore(_valid_store(tmp_path))
    assert len(store.receipts()) == 2
    assert len(store.candidates()) == 1
    assert len(store.progress()) == 1


def _raises(path: Path) -> None:
    with pytest.raises(LoopStoreError):
        LoopStore(path).receipts()


def test_malformed_candidates_container_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["candidates"] = ["not", "a", "dict"]
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_invalid_candidate_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        key = next(iter(data["candidates"]))
        data["candidates"][key] = {"candidate_id": key, "unexpected": True}  # missing required fields
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_candidate_key_id_mismatch_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        key = next(iter(data["candidates"]))
        row = data["candidates"].pop(key)
        data["candidates"]["candidate:tampered"] = row  # key no longer equals candidate_id
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_malformed_progress_container_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["progress"] = [1, 2, 3]
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_invalid_progress_row_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        key = next(iter(data["progress"]))
        data["progress"][key] = {"nested": {"not": "scalar"}}  # a progress row must be flat scalars
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_out_of_range_progress_percent_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        key = next(iter(data["progress"]))
        data["progress"][key]["progress_percent"] = 9999
    _raises(_corrupt(_valid_store(tmp_path), mutate))


def test_tampered_receipt_chain_is_refused(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["receipts"][0]["project_id"] = "proj-tampered"
    _raises(_corrupt(_valid_store(tmp_path), mutate))
