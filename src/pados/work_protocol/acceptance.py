# SPDX-License-Identifier: AGPL-3.0-only
"""Governed verification of a composed ResultCandidate and its ResultReceipt manifest.

Verification is deterministic and evidence-bound: it recomputes coverage and
composition from the exact CapabilityResults, re-checks every sealed
ExecutionReceipt and artifact digest, and compares the candidate's expected
ScopeVersion with the authoritative current one. Model judgement may be
attached as advisory evidence; it never changes the verdict.

Nothing here commits. A PASSED verification only makes a candidate eligible for
a fresh human approval; acceptance still goes through ResultReceipt ->
StateProofService.commit_result (the single CAS).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pados.execution.context import canonical_hash
from pados.work_protocol.composition import compose_results
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.models import (
    CapabilityResult,
    CoverageMap,
    CoverageStatus,
    GoalRelationType,
    ResultCandidate,
    TaskEnvelope,
    VerificationStatus,
)
from pados.work_protocol.residual import create_residual_tasks

VERIFICATION_SCHEMA = "pados.result-candidate-verification.v1"
RECEIPT_SCHEMA = "pados.result-receipt.v1"
VERIFIER_ID = "pados.work_protocol.acceptance.v1"
_MAX_RECEIPT_ANSWER_CHARS = 18_000


class VerificationCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check: str
    passed: bool
    code: str | None = None


class AdvisoryEvidence(BaseModel):
    """Model or human judgement recorded as evidence only (never a verdict)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=80)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority: Literal["EVIDENCE_ONLY"] = "EVIDENCE_ONLY"


class CandidateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.result-candidate-verification.v1"] = "pados.result-candidate-verification.v1"
    protocol_version: str = "1.0"
    verifier: str = VERIFIER_ID
    candidate_id: str
    candidate_provenance_hash: str
    task_id: str
    task_envelope_hash: str
    coverage_hash: str
    expected_scope_version_ref: str
    observed_scope_version: int | None
    result_hashes: tuple[str, ...]
    execution_receipt_digests: tuple[str, ...]
    artifact_digests: tuple[str, ...]
    checks: tuple[VerificationCheck, ...]
    status: VerificationStatus
    advisory_evidence: tuple[AdvisoryEvidence, ...] = ()
    semantic_authority: Literal[False] = False

    @property
    def digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return tuple(str(item.code) for item in self.checks if not item.passed)


def _goal_satisfied(task: TaskEnvelope, coverage: CoverageMap) -> dict[str, bool]:
    """COMPLETE, or every PARENT_OF child is satisfied (residual completion)."""
    status = {entry.goal_id: entry.status for entry in coverage.entries}
    children: dict[str, list[str]] = {}
    for relation in task.goal_relations:
        if relation.relation is GoalRelationType.PARENT_OF:
            children.setdefault(relation.source_goal_id, []).append(relation.target_goal_id)
    memo: dict[str, bool] = {}

    def satisfied(goal_id: str) -> bool:
        if goal_id not in memo:
            memo[goal_id] = False  # cycles are already refused by the TaskEnvelope
            own = status.get(goal_id) is CoverageStatus.COMPLETE
            kids = children.get(goal_id, [])
            memo[goal_id] = own or (bool(kids) and all(satisfied(kid) for kid in kids))
        return memo[goal_id]

    return {goal.goal_id: satisfied(goal.goal_id) for goal in task.normalized_goals}


def _receipt_intact(receipt: Mapping[str, Any]) -> str | None:
    payload = {key: value for key, value in receipt.items() if key not in {"receipt_id", "content_hash"}}
    digest = canonical_hash(payload)
    if receipt.get("content_hash") != digest or receipt.get("receipt_id") != f"execution-sha256:{digest}":
        return None
    return digest


def verify_result_candidate(
    task: TaskEnvelope,
    results: tuple[CapabilityResult, ...],
    coverage: CoverageMap,
    candidate: ResultCandidate,
    *,
    execution_receipts: Mapping[str, Mapping[str, Any]],
    artifact_digests: Mapping[str, str],
    current_scope_version: int | None,
    advisory_evidence: Sequence[AdvisoryEvidence] = (),
) -> CandidateVerification:
    """Return a deterministic verdict; every failure is a named, fail-closed check."""
    checks: list[VerificationCheck] = []

    def record(name: str, ok: bool, code: str) -> None:
        checks.append(VerificationCheck(check=name, passed=ok, code=None if ok else code))

    # 1. Candidate is exactly the composition of these results (no substitution).
    try:
        rebuilt_coverage = build_coverage(task, results)
        recomposed = compose_results(task, results, coverage)
        same = (
            rebuilt_coverage.content_hash == coverage.content_hash
            and recomposed.candidate_id == candidate.candidate_id
            and recomposed.provenance_hash == candidate.provenance_hash
            and recomposed.model_copy(update={"verification_status": candidate.verification_status}).content_hash
            == candidate.model_copy(update={"result_receipt_ref": None}).content_hash
        )
    except ValueError:
        same = False
    record("CANDIDATE_RECOMPOSES_FROM_RESULTS", same, "CANDIDATE_NOT_DERIVED_FROM_RESULTS")
    record(
        "TASK_BINDING",
        candidate.task_id == task.task_id
        and candidate.project_ref == task.project_ref
        and candidate.classification is task.classification
        and candidate.scope_version_ref == task.scope_version_ref,
        "CANDIDATE_TASK_BINDING_MISMATCH",
    )
    # 2. Required goals complete (directly or through completed residual children).
    satisfied = _goal_satisfied(task, coverage)
    rejected = {entry.goal_id for entry in coverage.entries if entry.status is CoverageStatus.REJECTED}
    record(
        "REQUIRED_GOALS_COMPLETE",
        all(ok or goal_id in rejected for goal_id, ok in satisfied.items()),
        "REQUIRED_GOAL_INCOMPLETE",
    )
    try:
        remaining = create_residual_tasks(task, coverage, results)
    except ValueError:
        remaining = (None,)  # type: ignore[assignment]
    record("NO_RESIDUAL_WORK_REMAINS", not remaining, "RESIDUAL_WORK_REMAINS")
    record("NO_POLICY_REFUSAL", all(item.policy_refusal is None for item in results), "POLICY_REFUSAL_PRESENT")
    record("NO_UNRESOLVED_CONFLICT", not candidate.conflicts, "COMPOSITION_CONFLICT_UNRESOLVED")
    # 3. Execution receipts exist, are intact, and are bound to this task.
    receipts_ok = bool(results)
    receipt_digests: list[str] = []
    for result in results:
        sealed = execution_receipts.get(result.execution_receipt_ref.receipt_id)
        digest = None if sealed is None else _receipt_intact(sealed)
        binding = {} if sealed is None else sealed.get("execution_binding") or {}
        if (
            digest is None
            or sealed is None
            or digest != result.execution_receipt_ref.digest
            or sealed.get("ok") is not True
            or sealed.get("semantic_authority") is not False
            or not isinstance(binding, Mapping)
            or binding.get("task_id") != task.task_id
            or binding.get("goal_id") != result.goal_id
            or binding.get("project_ref") != task.project_ref
            or binding.get("classification") != task.classification.value
            or binding.get("scope_version_ref") != task.scope_version_ref
        ):
            receipts_ok = False
        else:
            receipt_digests.append(digest)
    record("EXECUTION_RECEIPTS_INTACT", receipts_ok, "EXECUTION_RECEIPT_MISSING_OR_TAMPERED")
    # 4. Artifacts present with the declared digests (recomputed from bytes by the caller).
    declared = {artifact.artifact_id: artifact.digest for result in results for artifact in result.artifacts}
    artifacts_ok = all(artifact_digests.get(key) == value for key, value in declared.items())
    record("ARTIFACT_DIGESTS_MATCH", artifacts_ok, "ARTIFACT_MISSING_OR_DIGEST_MISMATCH")
    # 5. Provenance and classification.
    provenance_ok = all(
        result.provenance.resource_id == result.resource_id
        and task.content_hash in result.provenance.input_digests
        for result in results
    )
    record("PROVENANCE_BOUND", provenance_ok, "PROVENANCE_MISSING_OR_UNBOUND")
    classification_ok = all(
        result.authorization.project_ref == task.project_ref
        and result.authorization.classification.rank <= task.classification.rank
        and all(task.classification.permits(item.classification) for item in result.artifacts)
        for result in results
    )
    record("PROJECT_CLASSIFICATION_CONSISTENT", classification_ok, "CLASSIFICATION_OR_PROJECT_MISMATCH")
    # 6. Not stale against the authoritative ScopeVersion.
    record(
        "SCOPE_VERSION_CURRENT",
        current_scope_version is not None and f"scope:{current_scope_version}" == task.scope_version_ref,
        "SCOPE_VERSION_STALE_OR_UNAVAILABLE",
    )
    status = VerificationStatus.PASSED if all(item.passed for item in checks) else VerificationStatus.FAILED
    return CandidateVerification(
        candidate_id=candidate.candidate_id,
        candidate_provenance_hash=candidate.provenance_hash,
        task_id=task.task_id,
        task_envelope_hash=task.content_hash,
        coverage_hash=coverage.content_hash,
        expected_scope_version_ref=task.scope_version_ref,
        observed_scope_version=current_scope_version,
        result_hashes=tuple(result.content_hash for result in results),
        execution_receipt_digests=tuple(receipt_digests),
        artifact_digests=tuple(sorted(set(declared.values()))),
        checks=tuple(checks),
        status=status,
        advisory_evidence=tuple(advisory_evidence),
    )


def verified_candidate(candidate: ResultCandidate, verification: CandidateVerification) -> ResultCandidate:
    if verification.status is not VerificationStatus.PASSED or verification.candidate_id != candidate.candidate_id:
        raise ValueError("RESULT_CANDIDATE_NOT_VERIFIED")
    return ResultCandidate.model_validate(
        {**candidate.model_dump(mode="json"), "verification_status": VerificationStatus.PASSED.value}
    )


def scope_version_number(scope_version_ref: str) -> int:
    prefix, _, value = scope_version_ref.partition(":")
    if prefix != "scope" or not value.isdigit():
        raise ValueError("SCOPE_VERSION_REF_INVALID")
    return int(value)


def result_receipt_specification(
    task: TaskEnvelope,
    candidate: ResultCandidate,
    verification: CandidateVerification,
    results: tuple[CapabilityResult, ...],
    *,
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    """References that make up the canonical ResultReceipt input manifest (no prompt/context)."""
    if candidate.verification_status is not VerificationStatus.PASSED:
        raise ValueError("RESULT_RECEIPT_REQUIRES_VERIFIED_CANDIDATE")
    required = {"proof_ref", "binding_digest", "actor_id", "session_id", "approval_ref", "decided_at",
                "presence_method", "assertion_digest"}
    if set(approval) != required or any(not isinstance(approval[key], str) or not approval[key] for key in required):
        raise ValueError("RESULT_RECEIPT_APPROVAL_REFERENCE_INVALID")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "protocol_version": "1.0",
        "task_id": task.task_id,
        "task_envelope_hash": task.content_hash,
        "goal_ids": [goal.goal_id for goal in task.normalized_goals],
        "candidate_id": candidate.candidate_id,
        "candidate_digest": candidate.content_hash,
        "candidate_provenance_hash": candidate.provenance_hash,
        "capability_results": [
            {"result_id": item.result_id, "result_hash": item.content_hash, "goal_id": item.goal_id}
            for item in results
        ],
        "execution_receipts": [item.execution_receipt_ref.model_dump(mode="json") for item in results],
        "artifacts": sorted(
            ({"artifact_id": artifact.artifact_id, "digest": artifact.digest}
             for item in results for artifact in item.artifacts),
            key=lambda value: value["artifact_id"],
        ),
        "verification": {"digest": verification.digest, "status": verification.status.value,
                         "verifier": verification.verifier},
        "approval": dict(approval),
        "project_ref": task.project_ref,
        "classification": task.classification.value,
        "expected_scope_version_ref": task.scope_version_ref,
        "provenance_hash": candidate.provenance_hash,
    }


def result_receipt_document(candidate: ResultCandidate, coverage: CoverageMap) -> str:
    """The accepted result document recorded as the ResultReceipt output (bounded)."""
    sections = [
        {"section_id": item.section_id, "goal_id": item.goal_id, "result_ref": item.result_ref,
         "content": item.content}
        for item in candidate.sections
    ]
    body: dict[str, Any] = {
        "candidate_id": candidate.candidate_id,
        "candidate_digest": candidate.content_hash,
        "task_id": candidate.task_id,
        "goals": [{"goal_id": entry.goal_id, "status": entry.status.value} for entry in coverage.entries],
        "sections": sections,
    }
    answer = json.dumps(body, sort_keys=True, ensure_ascii=False)
    if len(answer) > _MAX_RECEIPT_ANSWER_CHARS:
        body["sections"] = [{**item, "content": item["content"][:200]} for item in sections]
        body["sections_truncated_for_receipt"] = True
        answer = json.dumps(body, sort_keys=True, ensure_ascii=False)
    if len(answer) > _MAX_RECEIPT_ANSWER_CHARS:
        raise ValueError("RESULT_RECEIPT_DOCUMENT_TOO_LARGE")
    return json.dumps(
        {
            "title": "Accepted task result",
            "answer": answer,
            "subquestions": [],
            "gaps": [],
            "claims": [],
            "evidence_refs": [],
            "proposed_graph_changes": [],
            "limitations": ["HUMAN_APPROVED_VIA_FRESH_STEP_UP"],
        }
    )
