# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 3: a follow-up is anchored to the LATEST creation of a reused session id.

If a session id is reused (project B re-creates the same id after project A), replaying project A's old
progress row must not re-authorize polling the now-B session with project A's grant. The binding is anchored
to the digest of the latest creation receipt that owns the session id, so an earlier owner's row fails
closed even though its digest still exists somewhere in history.
"""
from __future__ import annotations

import json
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
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"


def _service(store: LoopStore, http: FakeHttp) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)


def test_replayed_old_owner_row_fails_closed_after_id_reuse(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
    store = LoopStore(path)
    service = _service(store, http)

    # Project A creates session SESSION, then we snapshot A's progress row.
    assert service.create_session(
        learning_request(project_id="proj-a", work_contract_id="wc-1", request_id="req-a"),
        contract=contract(project_id="proj-a", work_contract_id="wc-1"),
        grant=grant(project_id="proj-a", work_contract_id="wc-1"),
        classification=DataClassification.INTERNAL).result is OperationResult.RECORDED
    row_a = json.loads(path.read_text(encoding="utf-8"))["progress"][SESSION]

    # Project B re-creates the SAME session id, overwriting the progress row with B's binding.
    assert service.create_session(
        learning_request(project_id="proj-b", work_contract_id="wc-2", request_id="req-b"),
        contract=contract(project_id="proj-b", work_contract_id="wc-2"),
        grant=grant(project_id="proj-b", work_contract_id="wc-2"),
        classification=DataClassification.INTERNAL).result is OperationResult.RECORDED

    # Replay A's old row and try to poll with A's (still valid) grant.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["progress"][SESSION] = row_a
    path.write_text(json.dumps(data), encoding="utf-8")
    receipt = service.session_state(
        session_ref=SESSION, contract=contract(project_id="proj-a", work_contract_id="wc-1"),
        grant=grant(project_id="proj-a", work_contract_id="wc-1"))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY  # earlier owner no longer owns the id
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls


def test_current_owner_poll_still_works(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION),
                     ("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                          "progress": 100})})
    store = LoopStore(path)
    service = _service(store, http)
    service.create_session(learning_request(), contract=contract(), grant=grant(),
                           classification=DataClassification.INTERNAL)
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
