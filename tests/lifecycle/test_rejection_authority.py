"""ProofRejectionAuthority against stub rows (no DB). Live verification needs the integrator's DB gate."""
import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from pados.lifecycle.rejection import ProofRejectionAuthority, RejectionEvidence

HASH = hashlib.sha256(b"payload").hexdigest()
OBJECT_ID = "11111111-1111-1111-1111-111111111111"
RESULT_ID = "22222222-2222-2222-2222-222222222222"
DECIDED = datetime(2026, 9, 1, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> "_Rows":
        return self

    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self) -> Any:
        return iter(self.rows)


class _Connection:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.statements: list[str] = []

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Rows:
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("SET TRANSACTION"):
            return _Rows([])
        if "FROM state_proof_objects" in sql:
            obj = self.state.get("object")
            return _Rows([obj] if obj and params and params["content_hash"] == HASH else [])
        if "FROM state_proof_results" in sql:
            return _Rows(self.state.get("results", []))
        if "FROM state_proof_commits" in sql:
            return _Rows([{"n": self.state.get("commits", 0)}])
        if "FROM state_proof_projections" in sql:
            return _Rows([{"n": self.state.get("projections", 0)}])
        if "FROM pg_trigger" in sql:
            return _Rows([{"n": self.state.get("immutable_trigger", 1)}])
        if "FROM p6_21h_product_read_index" in sql:
            return _Rows([{"n": self.state.get("read_index", 0)}])
        raise AssertionError(sql)


class _Engine:
    def __init__(self, state: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.state = state or {}
        self.fail = fail
        self.connections: list[_Connection] = []

    def connect(self) -> _Connection:
        if self.fail:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        connection = _Connection(self.state)
        self.connections.append(connection)
        return connection


def _rejected_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "object": {"id": OBJECT_ID, "object_kind": "RESULT_PAYLOAD"},
        "results": [{"id": RESULT_ID, "status": "FAILED", "created_at": DECIDED, "produced": True}],
    }
    state.update(overrides)
    return state


def test_cr02_f03_failed_unreferenced_result_payload_is_rejected() -> None:
    engine = _Engine(_rejected_state())
    authority = ProofRejectionAuthority(engine)
    evidence = authority.rejection_evidence(HASH)
    assert evidence == RejectionEvidence(HASH, "REJECTED", DECIDED.isoformat(), f"state_proof_results:{RESULT_ID}",
                                         f"state_proof_results:{RESULT_ID}:FAILED")
    assert authority.referenced_by_accepted_state(HASH) is False
    for connection in engine.connections:  # read-only: never a write statement
        assert connection.statements[0].startswith("SET TRANSACTION") and "READ ONLY" in connection.statements[0]
        assert all(s.startswith(("SELECT", "SET TRANSACTION")) for s in connection.statements)


@pytest.mark.parametrize(
    ("state", "referenced"),
    [
        ({"object": None}, False),
        (_rejected_state(commits=1), True),
        (_rejected_state(projections=1), True),
        (_rejected_state(read_index=1), True),
        (_rejected_state(results=[{"id": RESULT_ID, "status": "SUCCEEDED", "created_at": DECIDED,
                                   "produced": True}]), True),
        (_rejected_state(results=[{"id": RESULT_ID, "status": "FAILED", "created_at": DECIDED, "produced": True},
                                  {"id": "r2", "status": "SUCCEEDED", "created_at": DECIDED, "produced": True}]),
         True),
        (_rejected_state(results=[{"id": RESULT_ID, "status": "FAILED", "created_at": DECIDED,
                                   "produced": False}]), False),
        (_rejected_state(results=[]), False),
        (_rejected_state(object={"id": OBJECT_ID, "object_kind": "INPUT_MANIFEST"}), False),
        (_rejected_state(results=[{"id": RESULT_ID, "status": "INVALID_SCHEMA", "created_at": datetime(2026, 9, 1),
                                   "produced": True}]), False),
    ],
    ids=["missing", "committed", "materialized", "read_index", "succeeded", "mixed", "input_manifest_only",
         "no_producer", "not_result_payload", "naive_timestamp"],
)
def test_anything_but_rejected_and_unreferenced_is_refused(state: dict[str, Any], referenced: bool) -> None:
    authority = ProofRejectionAuthority(_Engine(state))
    assert authority.rejection_evidence(HASH) is None
    assert authority.referenced_by_accepted_state(HASH) is referenced


def test_database_errors_and_bad_hashes_fail_closed() -> None:
    failing = ProofRejectionAuthority(_Engine(fail=True))
    assert failing.rejection_evidence(HASH) is None
    assert failing.referenced_by_accepted_state(HASH) is True
    engine = _Engine(_rejected_state())
    assert ProofRejectionAuthority(engine).rejection_evidence("not-a-hash") is None
    assert engine.connections == []
