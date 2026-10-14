# SPDX-License-Identifier: AGPL-3.0-only
"""The mutable progress row is an ADVISORY_PROJECTION_ONLY, never an authorization source.

Tampering the progress row cannot change authorization: a follow-up resolves the original governed context
from the authoritative, externally anchored binding, so a tampered project in the row is ignored (and
overwritten from the authority), while a caller claiming the tampered project is still refused.
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
    binding_authority,
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


def _service(store: LoopStore, http: FakeHttp, authority: object) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), store, capability=LEARNING_CAPABILITY,
                           adapter=LEARNING_ADAPTER, authority=authority)


def _created(tmp_path: Path) -> tuple[Path, FakeHttp, LoopStore, object]:
    path = tmp_path / "loop.json"
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    store = LoopStore(path)
    auth = binding_authority()
    _service(store, http, auth).create_session(learning_request(), contract=contract(), grant=grant(),
                                               classification=DataClassification.INTERNAL)
    return path, http, store, auth


def _tamper(path: Path, field: str, value: object) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["progress"][SESSION][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def test_untouched_poll_is_recorded(tmp_path: Path) -> None:
    path, http, store, auth = _created(tmp_path)
    receipt = _service(store, http, auth).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED


def test_progress_tamper_does_not_change_authority(tmp_path: Path) -> None:
    path, http, store, auth = _created(tmp_path)
    _tamper(path, "project_id", "proj-evil")  # advisory projection tamper
    # Authorization uses the authoritative binding (proj-a), not the tampered row: the poll still succeeds.
    receipt = _service(store, http, auth).session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert store.progress()[SESSION]["project_id"] == "proj-a"  # row overwritten from the authority


def test_caller_claiming_the_tampered_project_is_refused(tmp_path: Path) -> None:
    path, http, store, auth = _created(tmp_path)
    _tamper(path, "project_id", "proj-evil")
    # A caller whose contract claims the tampered project does not match the authoritative binding.
    receipt = _service(store, http, auth).session_state(
        session_ref=SESSION, contract=contract(project_id="proj-evil"), grant=grant(project_id="proj-evil"))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls
