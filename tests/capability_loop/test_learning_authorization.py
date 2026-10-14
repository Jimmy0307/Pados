# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 2: follow-up learning operations must re-run PADOS authorization, not trust the session.

``session_state`` and ``assessment`` resolve the governed session binding recorded at creation, verify the
supplied contract matches the bound project/work, re-run the authoritative gate (grant + policy), and only
then call the provider. A guessed or cross-project session, or a revoked grant, fails closed before the
provider is touched, and an allowed follow-up always records a real policy decision hash.
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
from pados.capability_loop.receipts import OperationResult, verify_chain
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
_STATE_ROUTE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                      "progress": 100})}


def _http() -> FakeHttp:
    return FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20), **_STATE_ROUTE})


def _service(store: LoopStore, http: FakeHttp, authority: object) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=authority)


def _created(store: LoopStore, http: FakeHttp, authority: object) -> None:
    receipt = _service(store, http, authority).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.RECORDED


def _get_calls(http: FakeHttp) -> list[tuple[str, str]]:
    return [call for call in http.calls if call[0] == "GET" and SESSION in call[1]]


# -- authorized follow-ups -------------------------------------------------
def test_authorized_poll_records_real_policy_decision_hash(tmp_path: Path) -> None:
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.policy_code == GateCode.ALLOWED and receipt.result is OperationResult.RECORDED
    assert receipt.policy_decision_hash is not None and len(receipt.policy_decision_hash) == 64
    assert verify_chain(store.receipts())


def test_authorized_assessment_is_allowed(tmp_path: Path) -> None:
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).assessment(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert receipt.policy_decision_hash is not None


# -- fail closed -----------------------------------------------------------
def test_unknown_session_is_refused_before_provider(tmp_path: Path) -> None:
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).session_state(
        session_ref="job-guessed", contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_NOT_BOUND
    assert _get_calls(http) == []  # provider never polled for a session we did not bind


def test_wrong_project_is_refused(tmp_path: Path) -> None:
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).session_state(
        session_ref=SESSION, contract=contract(project_id="proj-other"), grant=grant(project_id="proj-other"))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_PROJECT_MISMATCH
    assert _get_calls(http) == []


def test_missing_grant_is_refused(tmp_path: Path) -> None:
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).session_state(
        session_ref=SESSION, contract=contract(), grant=grant(capabilities=[]))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.GRANT_REFUSED
    assert _get_calls(http) == []


def test_revoked_capability_grant_is_refused(tmp_path: Path) -> None:
    # The grant now authorizes a different capability than the session's: follow-up must fail closed.
    http = _http()
    store = LoopStore(tmp_path / "loop.json")
    auth = binding_authority()
    _created(store, http, auth)
    receipt = _service(store, http, auth).session_state(
        session_ref=SESSION, contract=contract(required_capabilities=["CAPABILITY:OTHER_THING"]),
        grant=grant(capabilities=["CAPABILITY:OTHER_THING"]))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert _get_calls(http) == []
