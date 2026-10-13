# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only rejection authority for rejected-artifact payload purge.

A caller cannot assert that an artifact was rejected: purge planning and
application both ask a ``RejectionAuthority`` port, which derives rejection
evidence from authoritative state or answers ``None``. The DB-backed
implementation reads the P6.19B proof tables read-only and is conservative:
an object counts as rejected-and-unreferenced only when

* it is a ``RESULT_PAYLOAD`` object and at least one result produced it;
* every result producing or referencing it ended in a terminal non-accepted
  status (``FAILED`` / ``INVALID_SCHEMA``); a ``SUCCEEDED`` result may still be
  awaiting a human decision, so it is never treated as rejected;
* no ``state_proof_commits`` row exists for any of those results (any status);
* no ``MATERIALIZED`` projection exists for any of those results;
* the durable product read index does not reference it.

Semantics (documented, not assumed): in PADOS a ``FAILED`` or ``INVALID_SCHEMA``
result *is* the authoritative validation rejection. ``StateProofService`` records
the result only after execution and validation have finished, with its final
status, and never commits or materializes a non-``SUCCEEDED`` result. Result rows
are immutable (trigger ``state_proof_results_immutable`` rejects UPDATE/DELETE), so
a row's ``created_at`` is the instant its terminal status was decided: there is
no later status transition. ``created_at == terminal decision instant`` is a PADOS
invariant guaranteed by insert-once (the result is inserted only after its final
status is known) plus the immutability trigger. The authority verifies that the
trigger actually covers ``state_proof_results`` in the current schema as a BEFORE
ROW trigger on both UPDATE and DELETE, enabled in normal operation and executing
``reject_state_proof_immutable_mutation``; without that, or without a timezone-aware ``created_at`` on
every rejecting result, the rejection time is unknown and the answer is ``None``
(``REJECTION_TIME_UNKNOWN``). The rejection instant is the latest terminal
timestamp of the rejecting results, normalized to UTC.

Anything else, including any database error, is ``None`` (refuse). This module
never writes to the proof database.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

REJECTED = "REJECTED"
TERMINAL_NON_ACCEPTED = frozenset({"FAILED", "INVALID_SCHEMA"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RejectionEvidence:
    """Authority-derived evidence; never constructed from caller-supplied claims."""

    artifact_hash: str
    status: str
    decided_at: str
    receipt_ref: str
    decision_evidence_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {"artifact_hash": self.artifact_hash, "status": self.status, "decided_at": self.decided_at,
                "receipt_ref": self.receipt_ref, "decision_evidence_ref": self.decision_evidence_ref}


class RejectionAuthority(Protocol):
    """Read-only authority port consulted at plan time and again at apply time."""

    def rejection_evidence(self, artifact_hash: str) -> RejectionEvidence | None:
        """Evidence that ``artifact_hash`` is rejected and unreferenced, else ``None``."""

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        """True when accepted/committed/materialized state references the artifact (fail closed)."""

    def refusal_code(self, artifact_hash: str) -> str:
        """Machine-readable reason ``rejection_evidence`` is ``None`` (reporting only)."""


class _Connection(Protocol):
    def execute(self, statement: Any, parameters: Mapping[str, Any] | None = None) -> Any: ...


class _Connectable(Protocol):
    def connect(self) -> Any: ...


_BEGIN_READ_ONLY = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
_OBJECT = text("SELECT id, object_kind FROM state_proof_objects WHERE content_hash = :content_hash")
_RESULTS = text(
    "SELECT id, status, created_at, "
    "(output_object_id = CAST(:object_id AS uuid)) AS produced "
    "FROM state_proof_results "
    "WHERE output_object_id = CAST(:object_id AS uuid) OR input_manifest_object_id = CAST(:object_id AS uuid)"
)
_COMMITS = text("SELECT count(*) AS n FROM state_proof_commits WHERE result_id = ANY(CAST(:result_ids AS uuid[]))")
_PROJECTIONS = text(
    "SELECT count(*) AS n FROM state_proof_projections "
    "WHERE result_id = ANY(CAST(:result_ids AS uuid[])) AND status = 'MATERIALIZED'"
)
# The immutability guard must really cover the queried table: a BEFORE, FOR EACH ROW
# trigger on state_proof_results in the current schema, firing on both UPDATE and DELETE
# (pg_trigger.tgtype bits: ROW=1, BEFORE=2, DELETE=8, UPDATE=16), enabled in normal
# operation (tgenabled 'O' origin/local or 'A' always; 'R' replica-only and 'D' disabled
# do not count) and executing the migration's reject_state_proof_immutable_mutation().
_IMMUTABLE_RESULTS = text(
    "SELECT count(*) AS n FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
    "JOIN pg_proc p ON p.oid = t.tgfoid "
    "WHERE c.relname = 'state_proof_results' AND ns.nspname = current_schema() "
    "AND t.tgname = 'state_proof_results_immutable' AND NOT t.tgisinternal "
    "AND t.tgenabled IN ('O', 'A') "
    "AND (t.tgtype::int & 1) = 1 AND (t.tgtype::int & 2) = 2 "
    "AND (t.tgtype::int & 8) = 8 AND (t.tgtype::int & 16) = 16 "
    "AND p.proname = 'reject_state_proof_immutable_mutation'"
)
_READ_INDEX = text(
    "SELECT count(*) AS n FROM p6_21h_product_read_index WHERE context_object_id = CAST(:object_id AS uuid) "
    "OR latest_snapshot_object_id = CAST(:object_id AS uuid) OR final_report_object_id = CAST(:object_id AS uuid)"
)


@dataclass(frozen=True)
class _Assessment:
    exists: bool
    referenced: bool
    evidence: RejectionEvidence | None
    code: str = "ARTIFACT_NOT_PROVEN_REJECTED"


def _count(connection: _Connection, statement: Any, params: Mapping[str, Any]) -> int:
    row = connection.execute(statement, params).mappings().first()
    return int(row["n"]) if row is not None else 0


def _utc(value: object) -> datetime | None:
    """Timezone-aware instant normalized to UTC; naive or unparsable values are unknown."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    return None


def _assess(connection: _Connection, artifact_hash: str) -> _Assessment:
    connection.execute(_BEGIN_READ_ONLY)
    obj = connection.execute(_OBJECT, {"content_hash": artifact_hash}).mappings().first()
    if obj is None:
        return _Assessment(False, False, None, "OBJECT_UNKNOWN")
    object_id = str(obj["id"])
    results = [dict(r) for r in connection.execute(_RESULTS, {"object_id": object_id}).mappings()]
    ids = sorted(str(r["id"]) for r in results)
    statuses = {str(r["status"]) for r in results}
    referenced = (
        bool(statuses - TERMINAL_NON_ACCEPTED)
        or (bool(ids) and _count(connection, _COMMITS, {"result_ids": ids}) > 0)
        or (bool(ids) and _count(connection, _PROJECTIONS, {"result_ids": ids}) > 0)
        or _count(connection, _READ_INDEX, {"object_id": object_id}) > 0
    )
    if referenced:
        return _Assessment(True, True, None, "PAYLOAD_REFERENCED_BY_ACCEPTED_STATE")
    producers = [r for r in results if r.get("produced")]
    if str(obj["object_kind"]) != "RESULT_PAYLOAD" or not producers or len(producers) != len(results):
        return _Assessment(True, False, None, "NOT_A_REJECTED_RESULT_PAYLOAD")
    if _count(connection, _IMMUTABLE_RESULTS, {}) < 1:
        return _Assessment(True, False, None, "REJECTION_TIME_UNKNOWN")
    instants = [(_utc(r["created_at"]), str(r["id"]), str(r["status"])) for r in producers]
    if any(moment is None for moment, _, _ in instants):
        return _Assessment(True, False, None, "REJECTION_TIME_UNKNOWN")
    decided, result_id, status = max((m, i, st) for m, i, st in instants if m is not None)
    return _Assessment(True, False, RejectionEvidence(
        artifact_hash=artifact_hash, status=REJECTED, decided_at=decided.isoformat(),
        receipt_ref=f"state_proof_results:{result_id}",
        decision_evidence_ref=f"state_proof_results:{result_id}:{status}"), "")


class ProofRejectionAuthority:
    """Conservative, read-only rejection authority over the P6.19B proof database."""

    def __init__(self, engine: _Connectable) -> None:
        self._engine = engine

    def _assessment(self, artifact_hash: str) -> _Assessment | None:
        if not _SHA256.fullmatch(artifact_hash):
            return None
        try:
            with self._engine.connect() as connection:
                return _assess(connection, artifact_hash)
        except (SQLAlchemyError, OSError, KeyError, TypeError, ValueError):
            return None

    def rejection_evidence(self, artifact_hash: str) -> RejectionEvidence | None:
        assessment = self._assessment(artifact_hash)
        return None if assessment is None else assessment.evidence

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        assessment = self._assessment(artifact_hash)
        return True if assessment is None else assessment.referenced

    def refusal_code(self, artifact_hash: str) -> str:
        assessment = self._assessment(artifact_hash)
        return "AUTHORITY_UNAVAILABLE" if assessment is None else assessment.code


class StaticRejectionAuthority:
    """In-memory authority for tests and simulations (explicit evidence map)."""

    def __init__(self, evidence: Sequence[RejectionEvidence] = (), referenced: Sequence[str] = ()) -> None:
        self.evidence = {e.artifact_hash: e for e in evidence}
        self.referenced = set(referenced)

    def rejection_evidence(self, artifact_hash: str) -> RejectionEvidence | None:
        return None if artifact_hash in self.referenced else self.evidence.get(artifact_hash)

    def referenced_by_accepted_state(self, artifact_hash: str) -> bool:
        return artifact_hash in self.referenced

    def refusal_code(self, artifact_hash: str) -> str:
        if artifact_hash in self.referenced:
            return "PAYLOAD_REFERENCED_BY_ACCEPTED_STATE"
        return "" if artifact_hash in self.evidence else "ARTIFACT_NOT_PROVEN_REJECTED"
