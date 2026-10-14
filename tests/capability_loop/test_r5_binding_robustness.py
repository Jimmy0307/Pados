# SPDX-License-Identifier: AGPL-3.0-only
"""Round-5: an orphaned binding fails closed, and the binding log's byte bound fits its record cap.

R5-F1: a binding whose creation receipt was never committed (e.g. a crash after bind, before record) does
not authorize follow-ups -- the creation must be durably committed in the receipt chain first.
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
from pados.capability_loop.gate import GateCode
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


def test_orphaned_binding_without_a_committed_creation_fails_closed(tmp_path: Path) -> None:
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

    # An orphan: a binding exists in the authority, but no creation receipt was ever recorded.
    authority.bind(GovernedSessionBinding(
        provider_id=OPENMAIC_PROVIDER_ID, external_session_ref=ORPHAN, project_id="proj-a",
        work_contract_id="wc-1", classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds")))
    receipt = service.session_state(session_ref=ORPHAN, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_CREATION_UNCOMMITTED
    assert ("GET", f"/api/generate-classroom/{ORPHAN}") not in http.calls  # provider never polled


def test_binding_log_byte_bound_accommodates_the_record_cap(tmp_path: Path) -> None:
    auth = anchored_session_binding_authority(tmp_path / "binding.jsonl", tmp_path / "anchor")
    # The readable byte bound is at least the record cap times the per-line cap: the log can never exceed the
    # read limit before hitting the record cap.
    assert auth._file.max_bytes >= auth._max * auth._file.max_line_bytes
    # A real record fits within the per-line cap, so appends never exceed it.
    auth.bind(GovernedSessionBinding(
        provider_id=OPENMAIC_PROVIDER_ID, external_session_ref="job-size", project_id="proj-a",
        work_contract_id="wc-1", classification=DataClassification.RESTRICTED,
        capability_gap_ref="gap:some_long_capability_gap_reference_name", created_at="2026-10-14T00:00:00+00:00"))
    line = (tmp_path / "binding.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert len(line.encode("utf-8")) < auth._file.max_line_bytes
