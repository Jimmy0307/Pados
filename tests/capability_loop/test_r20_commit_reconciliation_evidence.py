# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 Codex review of 46fb250 [learning.py:152]: reconcile a failed commit against THIS receipt's evidence.

Two concurrent creations can receive the same external session and governed context: both stage (idempotent while
pending) and write distinct creation receipts. Once one commits, the other's commit is refused
(``BINDING_ALREADY_COMMITTED``: different evidence). Reconciling by governed context alone reported that second
receipt as accepted too, although the authority was finalized against the other receipt. Reconciliation must be
evidence-bound: only an idempotent re-commit with this very receipt hash proves this creation is the committed one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
from pados.capability_loop.binding_authority import GovernedSessionBinding
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import anchored_session_binding_authority
from pados.enterprise.classification import DataClassification

PROVIDER = "openmaic.adaptive_learning"
SESSION = "job-race"


def test_losing_concurrent_creation_is_not_reported_as_accepted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    store = LoopStore(tmp_path / "loop.json")
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20, job_id=SESSION)})
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store, capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER, authority=authority)
    real_commit = authority.commit
    results: dict[str, Any] = {}

    def interleaved(binding: GovernedSessionBinding, *, evidence_ref: str | None) -> Any:
        if "winner" not in results:
            # Creation 1 has staged and written its receipt; creation 2 now runs to completion and commits first.
            monkeypatch.setattr(authority, "commit", real_commit)
            results["winner"] = service.create_session(
                learning_request(request_id="req-2"), contract=contract(), grant=grant(),
                classification=DataClassification.INTERNAL)
            monkeypatch.setattr(authority, "commit", interleaved)
        return real_commit(binding, evidence_ref=evidence_ref)

    monkeypatch.setattr(authority, "commit", interleaved)
    loser = service.create_session(learning_request(request_id="req-1"), contract=contract(), grant=grant(),
                                   classification=DataClassification.INTERNAL)
    monkeypatch.undo()
    winner = results["winner"]

    assert winner.result is OperationResult.RECORDED
    assert loser.result is OperationResult.REFUSED_BY_POLICY  # never a second accepted creation
    assert loser.policy_code == "SESSION_BIND_FINALIZE_FAILED"
    assert "BINDING_ALREADY_COMMITTED" in loser.limitations
    # The authority was finalized against exactly the winner's creation receipt.
    records = [json.loads(line) for line in (tmp_path / "log" / "binding.jsonl").read_text(
        encoding="utf-8").splitlines()]
    committed = [r for r in records if r["state"] == "COMMITTED"]
    assert len(committed) == 1
    recorded = [r for r in store.receipts() if r.result is OperationResult.RECORDED]
    assert committed[0]["evidence_ref"] == winner.receipt_hash
    assert committed[0]["evidence_ref"] in {r.receipt_hash for r in recorded}
    assert authority.resolve(PROVIDER, SESSION) is not None  # the winner's session keeps working
