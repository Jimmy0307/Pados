# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R18 (independent review) MEDIUM/LOW follow-ups.

* A provider session reported FAILED is never RECORDED/accepted -- not at creation and not on a poll (the
  whole-success contract); it is a PROVIDER_ERROR, not a malformed answer.
* A finalize that raised AFTER its COMMITTED record was anchored is reported as the success it is (the binding is
  resolvable and its creation receipt exists), not as a SESSION_BIND_FINALIZE_FAILED refusal.
* Both authority implementations validate ``evidence_ref`` identically (a SHA-256 or None).
* Filesystem/lock I/O failures are reported as BINDING_LOG_UNAVAILABLE, not as log corruption.
"""
from __future__ import annotations

from datetime import UTC, datetime
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
from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop.contracts import FailureCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import (
    InMemorySessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification

PROVIDER = "openmaic.adaptive_learning"
SESSION = "job-r18"


def _binding() -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=SESSION, project_id="proj-a", work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _state_route(step: str) -> dict[tuple[str, str], Any]:
    return {("GET", f"/api/generate-classroom/{SESSION}"): (200, {
        "jobId": SESSION, "status": "running", "step": step, "progress": 50})}


def _service(tmp_path: Path, http: FakeHttp, authority: Any) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), LoopStore(tmp_path / "loop.json"),
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=authority)


def test_polled_failed_session_is_not_accepted(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20, job_id=SESSION),
                     **_state_route("failed")})
    service = _service(tmp_path, http, InMemorySessionBindingAuthority())
    created = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert created.result is OperationResult.RECORDED
    before = LoopStore(tmp_path / "loop.json").progress()
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.PROVIDER_ERROR
    assert receipt.accepted_into_loop is False
    assert LoopStore(tmp_path / "loop.json").progress() == before


def test_failed_session_at_creation_is_a_provider_error(tmp_path: Path) -> None:
    authority = InMemorySessionBindingAuthority()
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="failed", progress=100, job_id=SESSION)})
    receipt = _service(tmp_path, http, authority).create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.PROVIDER_ERROR
    assert authority.resolve(PROVIDER, SESSION) is None


def test_finalize_that_landed_before_raising_is_reported_as_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    real_commit = authority.commit
    calls = {"n": 0}

    def landed_then_raised(binding: GovernedSessionBinding, *, evidence_ref: str | None) -> Any:
        calls["n"] += 1
        proof = real_commit(binding, evidence_ref=evidence_ref)  # the COMMITTED record IS anchored ...
        if calls["n"] == 1:
            raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE")  # ... but the first call reports a failure
        return proof  # the evidence-bound reconciliation re-commit is idempotent, like the real authority

    monkeypatch.setattr(authority, "commit", landed_then_raised)
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20, job_id=SESSION)})
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store, capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER, authority=authority)
    receipt = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.RECORDED  # truthful: the session IS authorized
    assert authority.resolve(PROVIDER, SESSION) is not None
    assert [r.result for r in store.receipts()] == [OperationResult.RECORDED]  # no contradicting refusal


@pytest.mark.parametrize("kind", ["anchored", "in_memory"])
@pytest.mark.parametrize("evidence", ["not-a-digest", "A" * 64, "a" * 63, ""])
def test_both_authorities_validate_evidence_refs_identically(tmp_path: Path, kind: str, evidence: str) -> None:
    authority: Any = (anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
                      if kind == "anchored" else InMemorySessionBindingAuthority())
    authority.stage(_binding())
    with pytest.raises(BindingAuthorityError, match="BINDING_EVIDENCE_REF_INVALID"):
        authority.commit(_binding(), evidence_ref=evidence)
    assert authority.resolve(PROVIDER, SESSION) is None


def test_io_failure_is_reported_as_unavailable_not_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")

    def io_error() -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(authority._file, "read_payload", io_error)
    with pytest.raises(BindingAuthorityError, match="BINDING_LOG_UNAVAILABLE"):
        authority.resolve(PROVIDER, SESSION)
