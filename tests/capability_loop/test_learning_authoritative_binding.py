# SPDX-License-Identifier: AGPL-3.0-only
"""LearningService authorizes follow-ups from the externally anchored binding authority (end to end).

A valid binding with a valid current grant passes; a revoked current grant fails closed; a tampered binding
log makes the authority unverifiable and fails closed; a foreign provider response is rejected; an unknown
session is refused. The authoritative binding -- never the mutable progress row -- governs.
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
from pados.capability_loop.gate import GateCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop_runtime.binding import GENESIS, _BindingRecord, anchored_session_binding_authority
from pados.capability_loop.binding_authority import GovernedSessionBinding
from pados.capability_loop.receipts import OperationResult, verify_chain
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
_STATE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                "progress": 100})}


def _service(tmp_path: Path, http: FakeHttp) -> tuple[LearningService, Path]:
    log = tmp_path / "log" / "binding.jsonl"
    authority = anchored_session_binding_authority(log, tmp_path / "anchor")
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                              capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=authority)
    return service, log


def _created(service: LearningService) -> None:
    assert service.create_session(learning_request(), contract=contract(), grant=grant(),
                                  classification=DataClassification.INTERNAL).result is OperationResult.RECORDED


def test_valid_binding_and_valid_grant_pass(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    service, _ = _service(tmp_path, http)
    _created(service)
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.ALLOWED and receipt.result is OperationResult.RECORDED
    assert receipt.policy_decision_hash is not None


def test_revoked_current_grant_fails_closed(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    service, _ = _service(tmp_path, http)
    _created(service)
    # The binding is valid, but the current grant no longer authorizes the capability.
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant(capabilities=[]))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.GRANT_REFUSED
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls


def test_tampered_binding_log_is_unverifiable_and_fails_closed(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    service, log = _service(tmp_path, http)
    _created(service)
    # Whole-file rewrite of the binding log (recomputed chain) while the independent anchor is untouched.
    tampered = _BindingRecord.seal(
        GovernedSessionBinding(provider_id="openmaic.adaptive_learning", external_session_ref=SESSION,
                               project_id="proj-evil", work_contract_id="wc-1",
                               classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
                               created_at="2026-10-14T00:00:00+00:00"),
        seq=1, prev_hash=GENESIS).model_dump(mode="json")
    log.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_BINDING_UNVERIFIABLE
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls


def test_foreign_provider_response_session_is_rejected(tmp_path: Path) -> None:
    foreign = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": "job-999", "step": "completed",
                                                                     "progress": 100})}
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **foreign})
    service, _ = _service(tmp_path, http)
    _created(service)
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert verify_chain(service._store.receipts())


def test_unknown_session_is_refused(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
    service, _ = _service(tmp_path, http)
    _created(service)
    receipt = service.session_state(session_ref="job-unknown", contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_NOT_BOUND
