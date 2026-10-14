# SPDX-License-Identifier: AGPL-3.0-only
"""Round-2 Finding 2: the follow-up authorization binding must be tamper-evident.

Follow-up authorization must not trust a mutable progress row as its source of truth. The binding
(session_ref, project, work contract, classification, capability gap, provider) is anchored by a digest
recorded in the hash-chained creation receipt. If the advisory progress row is mutated, its recomputed
digest no longer matches any creation receipt and the follow-up fails closed -- a valid current grant alone
cannot make a tampered binding pass. No Core semantics change; the anchor lives in the capability_loop
receipt chain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
_STATE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                "progress": 100})}


def _http() -> FakeHttp:
    return FakeHttp({**HEALTH_OK, **generate_ok(), **_STATE})


def _service(store: LoopStore, http: FakeHttp) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)


def _create(path: Path, http: FakeHttp) -> LoopStore:
    store = LoopStore(path)
    _service(store, http).create_session(learning_request(), contract=contract(), grant=grant(),
                                         classification=DataClassification.INTERNAL)
    return store


def _tamper(path: Path, field: str, value: object) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["progress"][SESSION][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def test_untouched_binding_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    http = _http()
    store = _create(path, http)
    receipt = _service(store, http).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED


def test_mutated_project_fails_closed_even_with_matching_grant(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    http = _http()
    store = _create(path, http)
    _tamper(path, "project_id", "proj-evil")
    # A fully valid grant for the tampered project must NOT make the tampered binding pass.
    receipt = _service(store, http).session_state(
        session_ref=SESSION, contract=contract(project_id="proj-evil"), grant=grant(project_id="proj-evil"))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls


@pytest.mark.parametrize("field,value", [
    ("work_contract_id", "wc-evil"),
    ("classification", "RESTRICTED"),
    ("capability_gap_ref", "gap:evil"),
    ("provider_id", "evil.provider"),
])
def test_mutated_binding_field_fails_closed(tmp_path: Path, field: str, value: str) -> None:
    path = tmp_path / "loop.json"
    http = _http()
    store = _create(path, http)
    _tamper(path, field, value)
    receipt = _service(store, http).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls
