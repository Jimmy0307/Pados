# SPDX-License-Identifier: AGPL-3.0-only
"""Round-4 Finding 2: ownership is re-checked atomically with the record (resolve-then-record race).

If a session id is concurrently reused after a follow-up resolves its binding but before it records, the
record must fail closed instead of overwriting the progress row with the now-stale binding. We force that
interleaving deterministically: the provider call, mid-operation, lets another project re-create the same
session id; the follow-up's atomic ownership precondition then refuses the write.
"""
from __future__ import annotations

from collections.abc import Callable
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
from pados.capability_loop.contracts import ProviderHealth, ProviderStatus
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import (
    LearningOutcome,
    LearningRequest,
    LearningSession,
    LearningSessionStatus,
    ModelRouting,
)
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.enterprise.classification import DataClassification

SESSION = "job-123"


def _session() -> LearningSession:
    return LearningSession(provider_id=OPENMAIC_PROVIDER_ID, session_ref=SESSION,
                           status=LearningSessionStatus.READY,
                           model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED, materials_bound=0,
                           progress_percent=100)


class _HookedProvider:
    """Creates SESSION for project A; on a later poll it runs a hook (a concurrent takeover) mid-call."""

    provider_id = OPENMAIC_PROVIDER_ID

    def __init__(self, on_poll: Callable[[], None]) -> None:
        self._on_poll = on_poll

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE)

    def create_session(self, request: LearningRequest) -> LearningOutcome:
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=_session())

    def session_state(self, session_ref: str) -> LearningOutcome:
        self._on_poll()  # another project re-creates the same session id between resolve and record
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=_session())

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:  # pragma: no cover
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=_session())


def test_concurrent_reuse_during_poll_fails_closed(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loop.json")

    def takeover() -> None:
        # Project B re-creates the SAME session id, becoming the latest owner and overwriting the row.
        http_b = FakeHttp({**HEALTH_OK, **generate_ok(job_id=SESSION)})
        LearningService(OpenMaicLearningProvider(ENDPOINT, http_b), store,
                        capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER).create_session(
            learning_request(project_id="proj-b", work_contract_id="wc-2", request_id="req-b"),
            contract=contract(project_id="proj-b", work_contract_id="wc-2"),
            grant=grant(project_id="proj-b", work_contract_id="wc-2"),
            classification=DataClassification.INTERNAL)

    service_a = LearningService(_HookedProvider(takeover), store,
                                capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)
    service_a.create_session(learning_request(), contract=contract(), grant=grant(),
                             classification=DataClassification.INTERNAL)

    receipt = service_a.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY  # A's stale binding is refused at record time
    # The progress row was NOT overwritten back to project A by the losing follow-up.
    assert store.progress()[SESSION]["project_id"] == "proj-b"
