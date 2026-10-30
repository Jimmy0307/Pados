# SPDX-License-Identifier: AGPL-3.0-only
"""Reusing a session id for a different governed context is refused by the authority (no rebind).

Once an external session id is bound to a project, a second create that reuses the same id for a different
project fails closed at bind time, so the original owner keeps the session and no replay can hand it to
another project.
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


def test_reuse_for_a_different_project_is_refused_and_owner_retained(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    service = _service(store, http, auth)

    assert service.create_session(
        learning_request(project_id="proj-a", work_contract_id="wc-1", request_id="req-a"),
        contract=contract(project_id="proj-a", work_contract_id="wc-1"),
        grant=grant(project_id="proj-a", work_contract_id="wc-1"),
        classification=DataClassification.INTERNAL).result is OperationResult.RECORDED

    # Project B reuses the SAME external session id for a different project: the authority refuses the bind.
    reuse = service.create_session(
        learning_request(project_id="proj-b", work_contract_id="wc-2", request_id="req-b"),
        contract=contract(project_id="proj-b", work_contract_id="wc-2"),
        grant=grant(project_id="proj-b", work_contract_id="wc-2"),
        classification=DataClassification.INTERNAL)
    assert reuse.result is OperationResult.REFUSED_BY_POLICY
    assert reuse.policy_code == GateCode.SESSION_BIND_REFUSED

    # The original owner still owns the session.
    poll = service.session_state(session_ref=SESSION, contract=contract(project_id="proj-a", work_contract_id="wc-1"),
                                 grant=grant(project_id="proj-a", work_contract_id="wc-1"))
    assert poll.result is OperationResult.RECORDED

    # Project B cannot poll it even with B's valid grant (not the bound project).
    hijack = service.session_state(session_ref=SESSION, contract=contract(project_id="proj-b", work_contract_id="wc-2"),
                                   grant=grant(project_id="proj-b", work_contract_id="wc-2"))
    assert hijack.result is OperationResult.REFUSED_BY_POLICY


def test_second_creation_of_a_committed_session_is_refused_not_reaccepted(tmp_path: Path) -> None:
    # R17: a committed binding is finalized against exactly one creation receipt. A provider that hands the
    # same session id to a second creation (even with an identical governed context) is refused rather than
    # accepted twice; the original committed binding keeps working.
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    service = _service(store, http, auth)
    first = service.create_session(learning_request(request_id="req-a"), contract=contract(), grant=grant(),
                                   classification=DataClassification.INTERNAL)
    assert first.result is OperationResult.RECORDED
    again = service.create_session(learning_request(request_id="req-a-again"), contract=contract(),
                                   grant=grant(), classification=DataClassification.INTERNAL)
    assert again.result is OperationResult.REFUSED_BY_POLICY
    assert again.policy_code == GateCode.SESSION_BIND_REFUSED
    assert auth.resolve(OpenMaicLearningProvider.provider_id, SESSION) is not None
