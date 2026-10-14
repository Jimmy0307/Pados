# SPDX-License-Identifier: AGPL-3.0-only
"""Round-2 Finding 1: a follow-up must answer about the session it asked for.

If the provider's response carries a different session_ref than the one requested, that foreign session must
never inherit the requested session's project / grant / authorization binding. The response is rejected
before any store or binding update; the original binding is untouched and the foreign session is never
authorized.
"""
from __future__ import annotations

from pathlib import Path

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
from pados.capability_loop.contracts import FailureCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult, verify_chain
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
FOREIGN = "job-999"
# The runtime answers a GET for SESSION with a body describing a DIFFERENT session (FOREIGN).
_FOREIGN_RESPONSE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": FOREIGN, "step": "completed",
                                                                          "progress": 100})}
_MATCHING_RESPONSE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                           "progress": 100})}


def _service(store: LoopStore, http: FakeHttp) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)


def _created(store: LoopStore, http: FakeHttp) -> None:
    assert _service(store, http).create_session(
        learning_request(), contract=contract(), grant=grant(),
        classification=DataClassification.INTERNAL).result is OperationResult.RECORDED


def test_matching_session_response_is_accepted(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(), **_MATCHING_RESPONSE})
    store = LoopStore(tmp_path / "loop.json")
    _created(store, http)
    receipt = _service(store, http).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED


def test_foreign_session_in_poll_is_rejected(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(), **_FOREIGN_RESPONSE})
    store = LoopStore(tmp_path / "loop.json")
    _created(store, http)
    receipt = _service(store, http).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    progress = store.progress()
    assert FOREIGN not in progress              # the foreign session is never authorized / recorded
    assert progress[SESSION]["project_id"] == "proj-a"  # the original binding is untouched
    assert verify_chain(store.receipts())


def test_foreign_session_in_assessment_is_rejected(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(), **_FOREIGN_RESPONSE})
    store = LoopStore(tmp_path / "loop.json")
    _created(store, http)
    receipt = _service(store, http).assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    assert FOREIGN not in store.progress()
