# SPDX-License-Identifier: AGPL-3.0-only
"""Round-11: concurrency-safe in-memory bind, separated anchor layout, work-contract in the follow-up digest.

R11-F1: ``InMemorySessionBindingAuthority.bind`` serializes its check/increment/store, so a concurrent rebind
of one identity with conflicting context can never let two different bindings slip in.
R11-F3: the anchored factory refuses a colocated log/anchor layout (the anchor must not live in, contain, or
equal the log's directory) -- a shared write scope would let one rewrite restamp both and defeat anchoring.
R11-F4: the follow-up request digest includes the work contract, so two otherwise-identical follow-ups under
different contracts are distinguishable in the audit receipt.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    contract,
    grant,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import (
    InMemorySessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification

PROVIDER = "openmaic.adaptive_learning"


def _binding(work: str = "wc-1") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref="job-1", project_id="proj-a", work_contract_id=work,
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


# -- R11-F1: the in-memory bind is race-free -------------------------------
def test_concurrent_conflicting_binds_admit_exactly_one() -> None:
    authority = InMemorySessionBindingAuthority()
    workers = 24
    start = threading.Barrier(workers)
    successes: list[GovernedSessionBinding] = []
    conflicts = [0]
    lock = threading.Lock()  # guards the test's own result tallies only

    def attempt(index: int) -> None:
        # Each worker binds the SAME identity (job-1) with a DIFFERENT work contract -> different context hash.
        binding = _binding(work=f"wc-{index}")
        start.wait()
        try:
            authority.bind(binding)
        except BindingAuthorityError as exc:
            assert exc.code == "BINDING_CONFLICT"
            with lock:
                conflicts[0] += 1
        else:
            with lock:
                successes.append(binding)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one conflicting bind may win; every other attempt is refused, and the authority holds only the
    # single winning binding (a check-then-set race would have admitted two different contexts).
    assert len(successes) == 1
    assert conflicts[0] == workers - 1
    resolved = authority.resolve(PROVIDER, "job-1")
    assert resolved is not None and resolved.context_hash == successes[0].context_hash


# -- R11-F3: the anchored factory rejects a colocated anchor ---------------
def test_factory_rejects_anchor_inside_the_log_directory(tmp_path: Path) -> None:
    # anchor dir sits inside the log's own directory -> shared write scope -> refused.
    with pytest.raises(BindingAuthorityError) as excinfo:
        anchored_session_binding_authority(tmp_path / "binding.jsonl", tmp_path / "anchor")
    assert excinfo.value.code == "BINDING_ANCHOR_NOT_SEPARATED"


def test_factory_rejects_anchor_equal_to_the_log_directory(tmp_path: Path) -> None:
    with pytest.raises(BindingAuthorityError) as excinfo:
        anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "log")
    assert excinfo.value.code == "BINDING_ANCHOR_NOT_SEPARATED"


def test_factory_rejects_log_inside_the_anchor_directory(tmp_path: Path) -> None:
    with pytest.raises(BindingAuthorityError) as excinfo:
        anchored_session_binding_authority(tmp_path / "anchor" / "binding.jsonl", tmp_path / "anchor")
    assert excinfo.value.code == "BINDING_ANCHOR_NOT_SEPARATED"


def test_factory_accepts_separated_log_and_anchor(tmp_path: Path) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    auth.bind(_binding())
    assert auth.resolve(PROVIDER, "job-1") is not None


# -- R11-F4: the follow-up digest distinguishes work contracts -------------
def test_followup_digest_includes_the_work_contract(tmp_path: Path) -> None:
    # An unbound session is refused SESSION_NOT_BOUND, but the receipt still records the request digest. Two
    # follow-ups that differ ONLY in work_contract_id must produce different digests (the contract is bound in).
    http = FakeHttp({**HEALTH_OK})
    service = LearningService(OpenMaicLearningProvider(ENDPOINT, http), LoopStore(tmp_path / "loop.json"),
                              capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER,
                              authority=InMemorySessionBindingAuthority())
    first = service.session_state(session_ref="job-x", contract=contract(work_contract_id="wc-1"), grant=grant())
    second = service.session_state(session_ref="job-x", contract=contract(work_contract_id="wc-2"), grant=grant())
    assert first.result is OperationResult.REFUSED_BY_POLICY
    assert second.result is OperationResult.REFUSED_BY_POLICY
    assert first.request_digest != second.request_digest  # work contract is part of the digest
