# SPDX-License-Identifier: AGPL-3.0-only
"""Round-12: reconcile an ambiguous append, require whole-success before binding, pin provider identity.

R12-F1: if an append makes the record visible on disk before raising, the log is reconciled back to the
pre-append state so it never leads the un-advanced anchor (which would brick every later bind/resolve).
R12-F2: a model-valid but explicitly incomplete creation outcome (partial, or non-AVAILABLE with no failure)
does NOT create a durable authoritative binding.
R12-F3: a follow-up response attributed to another provider is rejected even when its session ref matches.
R12-F4: an idempotent in-memory rebind returns the ORIGINAL assigned sequence, matching the anchored log.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from loop_learning_support import (
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    contract,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OPENMAIC_PROVIDER_ID
from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop.contracts import FailureCode, ProviderStatus
from pados.capability_loop.learning import LearningService
from pados.capability_loop.learning_contracts import (
    LearningOutcome,
    LearningSession,
    LearningSessionStatus,
    ModelRouting,
)
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import (
    InMemorySessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification

PROVIDER = OPENMAIC_PROVIDER_ID
SESSION = "job-1"
FOREIGN = "rogue.provider"


def _binding(session: str = SESSION) -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id="proj-a", work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _session(provider: str = PROVIDER, ref: str = SESSION) -> LearningSession:
    return LearningSession(provider_id=provider, session_ref=ref, status=LearningSessionStatus.READY,
                           model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED, materials_bound=0,
                           progress_percent=100)


def _outcome(*, status: ProviderStatus = ProviderStatus.AVAILABLE, failure: FailureCode | None = None,
             partial: bool = False, provider: str = PROVIDER, ref: str = SESSION) -> LearningOutcome:
    return LearningOutcome(provider_id=provider, status=status, failure=failure, partial=partial,
                           session=_session(provider, ref))


class _StubProvider:
    """A minimal LearningProvider returning scripted outcomes, so incomplete/foreign answers can be exercised."""

    def __init__(self, *, create: LearningOutcome, state: LearningOutcome | None = None) -> None:
        self._create = create
        self._state = state

    @property
    def provider_id(self) -> str:
        return PROVIDER

    def health(self) -> Any:  # not used by create_session / session_state under test
        raise NotImplementedError

    def create_session(self, request: Any) -> LearningOutcome:
        return self._create

    def session_state(self, session_ref: str) -> LearningOutcome:
        assert self._state is not None
        return self._state

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:
        raise NotImplementedError


def _service(tmp_path: Path, provider: _StubProvider) -> tuple[LearningService, InMemorySessionBindingAuthority]:
    authority = InMemorySessionBindingAuthority()
    service = LearningService(provider, LoopStore(tmp_path / "loop.json"), capability=LEARNING_CAPABILITY,
                              adapter=LEARNING_ADAPTER, authority=authority)
    return service, authority


# -- R12-F1: an ambiguous append is reconciled, not left leading the anchor --
def test_partially_visible_append_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    log = tmp_path / "log" / "binding.jsonl"
    real_append = auth._file.append_line

    def leaky(record: Any) -> None:
        real_append(record)  # the bytes land on disk ...
        raise OSError("fsync failed after the write was visible")  # ... but the call then fails

    monkeypatch.setattr(auth._file, "append_line", leaky)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.bind(_binding())
    assert excinfo.value.code == "BINDING_LOG_WRITE_FAILED"
    # The log was reconciled back: it does not sit one entry ahead of the (never-advanced) anchor.
    assert log.read_text(encoding="utf-8").strip() == ""

    # Recovery: with the append working again, binding and resolving still succeed (no permanent bricking).
    monkeypatch.setattr(auth._file, "append_line", real_append)
    auth.bind(_binding())
    assert auth.resolve(PROVIDER, SESSION) is not None


# -- R12-F2: an incomplete creation outcome never binds --------------------
def test_partial_creation_outcome_does_not_bind(tmp_path: Path) -> None:
    provider = _StubProvider(create=_outcome(partial=True), state=_outcome())
    service, authority = _service(tmp_path, provider)
    receipt = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert authority.resolve(PROVIDER, SESSION) is None  # no durable binding was created
    # A follow-up finds no binding, confirming nothing was authorized.
    assert service.session_state(session_ref=SESSION, contract=contract(),
                                 grant=grant()).policy_code == "SESSION_NOT_BOUND"


def test_non_available_creation_outcome_does_not_bind(tmp_path: Path) -> None:
    provider = _StubProvider(create=_outcome(status=ProviderStatus.DEGRADED))
    service, authority = _service(tmp_path, provider)
    receipt = service.create_session(learning_request(), contract=contract(), grant=grant(),
                                     classification=DataClassification.INTERNAL)
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert authority.resolve(PROVIDER, SESSION) is None


# -- R12-F3: a foreign-provider follow-up response is rejected --------------
def test_followup_response_from_another_provider_is_rejected(tmp_path: Path) -> None:
    # Create under a whole-success outcome (binds), but the state poll returns a response attributed to a
    # DIFFERENT provider with the same session ref; it must not be recorded under the bound provider.
    provider = _StubProvider(create=_outcome(),
                             state=_outcome(provider=FOREIGN, ref=SESSION))
    service, _ = _service(tmp_path, provider)
    service.create_session(learning_request(), contract=contract(), grant=grant(),
                           classification=DataClassification.INTERNAL)
    receipt = service.session_state(session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE


# -- R12-F4: idempotent in-memory rebind returns the original sequence ------
def test_idempotent_inmemory_rebind_returns_original_sequence() -> None:
    authority = InMemorySessionBindingAuthority()
    proof_a = authority.bind(_binding("job-a"))
    proof_b = authority.bind(_binding("job-b"))
    proof_a_again = authority.bind(_binding("job-a"))  # identical rebind of A
    # Sequences count transitions like the anchored log (stage + commit per bind).
    assert proof_b.sequence > proof_a.sequence
    assert proof_a_again.sequence == proof_a.sequence  # the original sequence, not the latest counter
