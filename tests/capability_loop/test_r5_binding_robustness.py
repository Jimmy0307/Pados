# SPDX-License-Identifier: AGPL-3.0-only
"""Round-5 / Round-11: binding authority is the sole commit, and the binding log's byte bound fits its cap.

R11-F2 (supersedes the original R5-F1): authorization depends only on the authoritative binding, never on the
DELETABLE, non-authoritative LoopStore receipt chain. A binding present in the authority therefore resolves
and authorizes a follow-up even when the operational store holds no creation receipt -- deleting that
operational evidence must never change effective authority. (The earlier ``SESSION_CREATION_UNCOMMITTED`` gate
is removed: it wrongly tied authority to deletable state and would have denied valid sessions.)
R5-F2: the binding log's readable byte bound accommodates the full record cap, so the log can never exceed
the read limit before the record cap and brick existing sessions.
"""
from __future__ import annotations

from datetime import UTC, datetime
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

from pados.capability_loop.adapters.openmaic_http import OPENMAIC_PROVIDER_ID, OpenMaicLearningProvider
from pados.capability_loop.binding_authority import GovernedSessionBinding
from pados.capability_loop.learning import LearningService
from pados.capability_loop_runtime.binding import (
    InMemorySessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"
ORPHAN = "job-orphan"
_STATE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {"jobId": SESSION, "step": "completed",
                                                                "progress": 100}),
          ("GET", f"/api/generate-classroom/{ORPHAN}"): (200, {"jobId": ORPHAN, "step": "completed",
                                                               "progress": 100})}


def test_binding_in_the_authority_authorizes_a_followup_without_a_store_receipt(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION), **_STATE})
    authority = InMemorySessionBindingAuthority()
    store = LoopStore(tmp_path / "loop.json")
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), store, capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER, authority=authority)
    # A normal session: binding AND creation receipt are both committed -> a poll works.
    service.create_session(learning_request(), contract=contract(), grant=grant(),
                           classification=DataClassification.INTERNAL)
    assert service.session_state(session_ref=SESSION, contract=contract(),
                                 grant=grant()).result is OperationResult.RECORDED

    # A binding exists in the AUTHORITY, but the deletable LoopStore holds no creation receipt for it. Because
    # the authoritative binding IS the commit, the follow-up still resolves and the provider is polled:
    # deleting the non-authoritative operational receipt must never deny a validly bound session.
    authority.bind(GovernedSessionBinding(
        provider_id=OPENMAIC_PROVIDER_ID, external_session_ref=ORPHAN, project_id="proj-a",
        work_contract_id="wc-1", classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds")))
    receipt = service.session_state(session_ref=ORPHAN, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.RECORDED
    assert ("GET", f"/api/generate-classroom/{ORPHAN}") in http.calls  # provider WAS polled


def test_binding_log_byte_bound_accommodates_the_record_cap(tmp_path: Path) -> None:
    log = tmp_path / "log" / "binding.jsonl"
    auth = anchored_session_binding_authority(log, tmp_path / "anchor")
    # The readable byte bound is at least the record cap times the per-line cap: the log can never exceed the
    # read limit before hitting the record cap.
    assert auth._file.max_bytes >= auth._max * auth._file.max_line_bytes
    # A real record fits within the per-line cap, so appends never exceed it.
    auth.bind(GovernedSessionBinding(
        provider_id=OPENMAIC_PROVIDER_ID, external_session_ref="job-size", project_id="proj-a",
        work_contract_id="wc-1", classification=DataClassification.RESTRICTED,
        capability_gap_ref="gap:some_long_capability_gap_reference_name", created_at="2026-10-14T00:00:00+00:00"))
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert len(line.encode("utf-8")) < auth._file.max_line_bytes
