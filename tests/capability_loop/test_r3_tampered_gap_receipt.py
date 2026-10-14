# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 5: a tampered binding with a Unicode gap must still fail closed, not raise.

A tampered ``capability_gap_ref`` carrying Unicode alphanumerics (e.g. ``gap:ééé``) passes the progress-row
schema check and reaches SESSION_BINDING_TAMPERED, but the derived capability code must stay ASCII so the
refusal receipt validates -- the follow-up returns a normal fail-closed refusal instead of raising.
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
from pados.capability_loop.receipts import OperationResult, is_valid_receipt_code
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"


def test_tampered_unicode_gap_refuses_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "loop.json"
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
    store = LoopStore(path)
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store,
                              capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)
    service.create_session(learning_request(), contract=contract(), grant=grant(),
                           classification=DataClassification.INTERNAL)

    # Tamper the bound gap with Unicode alphanumerics (a valid scalar string, so it passes progress schema).
    data = json.loads(path.read_text(encoding="utf-8"))
    data["progress"][SESSION]["capability_gap_ref"] = "gap:ééé"
    path.write_text(json.dumps(data), encoding="utf-8")

    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())  # must not raise
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert is_valid_receipt_code(receipt.capability_id)  # the receipt code stayed ASCII-valid
    assert ("GET", f"/api/generate-classroom/{SESSION}") not in http.calls
