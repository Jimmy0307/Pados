"""Concurrent CAS and DB-read audit tamper detection on the live proof substrate."""
from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from pados.config import get_settings
from pados.db import get_engine
from pados.state_proof import ObjectStore, StateProofService, repository
from pados.state_proof.audit_chain import verify_audit_chain
from tests.integration.test_p6_19b_state_substrate import CountingProvider, _run


@pytest.fixture
def proof_service(tmp_path: Path):
    engine = get_engine(get_settings())
    yield StateProofService(engine, ObjectStore(tmp_path / "objects"))
    engine.dispose()


def _commit(service: StateProofService, scope_id: str, result: dict, **extra: object) -> dict:
    return service.commit_result(
        result_id=str(result["id"]), scope_id=scope_id, expected_version=0,
        candidate_ref=str(result["id"]), manifest_hash=str(result["input_manifest_hash"]), **extra,
    )


def test_concurrent_candidates_on_one_scope_version_commit_exactly_once(
    proof_service: StateProofService,
) -> None:
    scope_id = f"race-{uuid4()}"
    candidates = [
        _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)["result"]
        for _ in range(4)
    ]
    barrier = threading.Barrier(len(candidates))
    outcomes: list[dict] = []
    lock = threading.Lock()

    def race(result: dict) -> None:
        barrier.wait()
        outcome = _commit(proof_service, scope_id, result)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=race, args=(result,)) for result in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    statuses = sorted(str(outcome["status"]) for outcome in outcomes)
    assert statuses == ["COMMITTED", "STALE_CANDIDATE", "STALE_CANDIDATE", "STALE_CANDIDATE"]
    with proof_service._session() as session:
        head = repository.get_scope_head(session, scope_id)
    assert head is not None and int(head["current_version"]) == 1


def test_audit_chain_read_from_database_detects_tampering(proof_service: StateProofService) -> None:
    scope_id = f"tamper-{uuid4()}"
    for _ in range(2):
        result = _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)["result"]
        with proof_service._session() as session:
            version = int(repository.get_scope_head(session, scope_id)["current_version"])
        proof_service.commit_result(
            result_id=str(result["id"]), scope_id=scope_id, expected_version=version,
            candidate_ref=str(result["id"]), manifest_hash=str(result["input_manifest_hash"]),
        )
    with proof_service._session() as session:
        records = repository.semantic_audit_records(session, scope_id)
    assert len(records) == 2
    assert verify_audit_chain(records) is True
    assert proof_service.verify_semantic_audit_chain(scope_id) is True

    altered_reason = [dict(records[0], reason="tampered"), dict(records[1])]
    dropped_link = [dict(records[1])]
    reordered = [dict(records[1]), dict(records[0])]
    for tampered in (altered_reason, dropped_link, reordered):
        assert verify_audit_chain(tampered) is False
