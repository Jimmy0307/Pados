# SPDX-License-Identifier: AGPL-3.0-only
"""Concurrent reuse of a session id cannot hijack ownership: the authority binding is immutable.

Two services sharing one authority (as two processes would share one authoritative binding log) can never
both own the same session id. The second, concurrent bind of the same id to a different project fails closed,
so there is no resolve-then-record ownership race to exploit.
"""
from __future__ import annotations

from pathlib import Path

from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    binding_authority,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.gate import GateCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
_STATE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                "progress": 100})}


def _service(store: LoopStore, http: FakeHttp, authority: object) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store, capability=LEARNING_CAPABILITY,
                           adapter=LEARNING_ADAPTER, authority=authority)


def test_concurrent_reuse_is_refused_and_owner_can_poll(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loop.json")
    authority = binding_authority()  # shared, as two processes share one authoritative binding log
    http_a = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    http_b = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
    service_a = _service(store, http_a, authority)
    service_b = _service(store, http_b, authority)

    assert service_a.create_session(learning_request(), contract=contract(), grant=grant(),
                                    classification=DataClassification.INTERNAL).result is OperationResult.RECORDED

    # Project B, concurrently, reuses the same session id against the shared authority: refused at bind.
    receipt_b = service_b.create_session(
        learning_request(project_id="proj-b", work_contract_id="wc-2", request_id="req-b"),
        contract=contract(project_id="proj-b", work_contract_id="wc-2"),
        grant=grant(project_id="proj-b", work_contract_id="wc-2"),
        classification=DataClassification.INTERNAL)
    assert receipt_b.result is OperationResult.REFUSED_BY_POLICY
    assert receipt_b.policy_code == GateCode.SESSION_BIND_REFUSED

    # Project A (the owner) can still poll; the progress projection stays project A.
    poll = service_a.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert poll.result is OperationResult.RECORDED
    assert store.progress()[SESSION]["project_id"] == "proj-a"
