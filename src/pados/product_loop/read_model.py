# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pados.product_loop.schemas import (
    AttemptRecord,
    CognitiveComposition,
    Decision,
    Design,
    DurableProductRead,
    Goal,
    Intent,
    ProductLoopReport,
    ProductLoopStatus,
    ReadAcceptedResult,
    ReadAttemptSummary,
    ReadContractSummary,
    ReadFailureSummary,
    ReadProgressStatus,
    ReadUnderstanding,
    ReadValidationSummary,
    Reflection,
    RuntimeResult,
    RuntimeStatus,
    ValidationReport,
    ValidationStatus,
    WorkContract,
)
from pados.state_proof import ObjectRef, StateProofService, repository


def contract_summary(contract: WorkContract) -> ReadContractSummary:
    return ReadContractSummary(
        work_contract_id=contract.work_contract_id,
        version=contract.version,
        parent_contract_id=contract.parent_contract_id,
        objective=contract.objective,
        workspace_root=contract.scope.workspace_root,
        allowed_paths=tuple(contract.scope.allowed_paths),
        acceptance_criteria=tuple(item.criterion_id for item in contract.acceptance_criteria),
        allowed_actions=tuple(contract.allowed_actions),
        prohibited_actions=tuple(contract.prohibited_actions),
        state=contract.state.value,
    )


def build_durable_read(
    *,
    run_id: str,
    status: str | ProductLoopStatus,
    goal: Goal,
    intent: Intent,
    composition: CognitiveComposition,
    design: Design,
    decision: Decision,
    contracts: Sequence[WorkContract],
    attempts: Sequence[AttemptRecord],
    reflections: Sequence[Reflection],
    final_result: RuntimeResult | None,
    final_validation: ValidationReport | None,
    commit: Mapping[str, Any] | None,
    started_at: str,
    user_report: str | None = None,
    current_phase: str | None = None,
    finished_at: str | None = None,
    human_action_required: bool = False,
    automatic_continuation_allowed: bool = False,
    report_object_ref: Mapping[str, Any] | None = None,
    unresolved_issues: Sequence[str] = (),
) -> DurableProductRead:
    status_value = status.value if isinstance(status, ProductLoopStatus) else str(status)
    summaries = tuple(contract_summary(contract) for contract in contracts)
    if not summaries:
        raise ValueError("READ_MODEL_REQUIRES_CONTRACT")
    read_attempts = tuple(
        _attempt_summary(attempt, reflections, index, current_phase=current_phase)
        for index, attempt in enumerate(attempts)
    )
    failures = tuple(
        ReadFailureSummary(
            attempt=attempt.attempt,
            classification=(
                attempt.runtime_result.failure_classification.value
                if attempt.runtime_result.failure_classification is not None
                else attempt.validation.failure_classification.value
                if attempt.validation.failure_classification is not None
                else "VALIDATION_FAILURE"
            ),
            detail=_failure_detail(attempt),
        )
        for attempt in attempts
        if attempt.runtime_result.status != RuntimeStatus.COMPLETED
        or not attempt.validation.validation_passed
    )
    latest_validation = _validation_summary(final_validation, attempts)
    accepted_result = _accepted_result(attempts, commit)
    progress_status = _progress_status(status_value, attempts, automatic_continuation_allowed)
    semantic_commit_status = _semantic_commit_status(commit, accepted_result, status_value)
    effective_human_action = human_action_required or progress_status in {
        ReadProgressStatus.BLOCKED,
        ReadProgressStatus.STALE,
        ReadProgressStatus.ITERATION_LIMIT,
    }
    issues = list(unresolved_issues)
    if progress_status == ReadProgressStatus.BLOCKED:
        issues.append("Authorization or workspace authority action is required.")
    elif progress_status == ReadProgressStatus.STALE:
        issues.append("The candidate was not committed because its context or scope was stale.")
    elif progress_status == ReadProgressStatus.ITERATION_LIMIT:
        issues.append("The bounded attempt budget ended without an accepted semantic commit.")
    elif progress_status == ReadProgressStatus.VERIFIED and semantic_commit_status != "COMMITTED":
        issues.append("Validation passed, but semantic commit is not recorded.")
    elif progress_status in {ReadProgressStatus.FAILED, ReadProgressStatus.INCOMPLETE}:
        issues.append("The latest durable attempt did not reach an accepted result.")
    if not attempts and progress_status == ReadProgressStatus.WORKING:
        issues.append("No implementation attempt has been durably recorded yet.")
    unique_issues = tuple(dict.fromkeys(issues))
    if finished_at is None and progress_status in {
        ReadProgressStatus.ACCEPTED,
        ReadProgressStatus.FAILED,
        ReadProgressStatus.BLOCKED,
        ReadProgressStatus.STALE,
        ReadProgressStatus.ITERATION_LIMIT,
    }:
        finished_at = final_result.completed_at if final_result is not None else None
    progress = _verified_progress(progress_status, current_phase, semantic_commit_status)
    report_text = user_report or _user_report(
        goal=goal.objective,
        progress_status=progress_status,
        attempts=len(attempts),
        semantic_commit_status=semantic_commit_status,
        issues=unique_issues,
        human_action_required=effective_human_action,
    )
    understanding_changes = tuple(
        f"Attempt {reflection.attempt}: {reflection.progress}"
        for reflection in reflections
        if reflection.revision_created
    )
    return DurableProductRead(
        run_id=run_id,
        status=status_value,
        progress_status=progress_status,
        current_phase=current_phase or _phase_for(progress_status),
        started_at=started_at,
        finished_at=finished_at,
        goal=goal.objective,
        understanding=ReadUnderstanding(
            intent_id=intent.intent_id,
            objective=intent.objective,
            selected_capabilities=tuple(composition.selected_capabilities),
            approach=design.approach,
            accepted_direction=decision.accepted_direction,
        ),
        authorized_contract=summaries[-1],
        contracts=summaries,
        attempts=read_attempts,
        attempt_count=len(read_attempts),
        failed_attempts=failures,
        latest_validation=latest_validation,
        accepted_result=accepted_result,
        semantic_commit_status=semantic_commit_status,
        verified_progress=progress,
        understanding_changes=understanding_changes,
        unresolved_issues=unique_issues,
        human_action_required=effective_human_action,
        automatic_continuation_allowed=automatic_continuation_allowed,
        user_report=report_text,
        report_object_ref=dict(report_object_ref) if report_object_ref is not None else None,
    )


def read_from_report(
    report: ProductLoopReport,
    *,
    started_at: str,
    report_object_ref: Mapping[str, Any] | None = None,
) -> DurableProductRead:
    status: str = report.status.value
    if _is_iteration_limit(report):
        status = ReadProgressStatus.ITERATION_LIMIT.value
    return build_durable_read(
        run_id=report.run_id,
        status=status,
        goal=report.goal,
        intent=report.intent,
        composition=report.composition,
        design=report.design,
        decision=report.decision,
        contracts=report.contracts,
        attempts=report.attempts,
        reflections=report.reflections,
        final_result=report.final_result,
        final_validation=report.final_validation,
        commit=report.commit,
        started_at=started_at,
        user_report=report.user_report,
        human_action_required=report.human_decision_required,
        report_object_ref=report_object_ref,
    )


def read_from_context(context: Mapping[str, Any]) -> DurableProductRead:
    goal = Goal.model_validate(context["goal"])
    intent = Intent.model_validate(context["intent"])
    composition = CognitiveComposition.model_validate(context["composition"])
    design = Design.model_validate(context["design"])
    decision = Decision.model_validate(context["decision"])
    contract = ReadContractSummary.model_validate(context["contract"])
    progress_status = ReadProgressStatus.BLOCKED if context.get("authorized") is False else ReadProgressStatus.WORKING
    status = ProductLoopStatus.AUTHORITY_BLOCK.value if progress_status == ReadProgressStatus.BLOCKED else "WORKING"
    issues = ("Authorization is required before dispatch.",) if progress_status == ReadProgressStatus.BLOCKED else (
        "No implementation attempt has been durably recorded yet.",
    )
    return DurableProductRead(
        run_id=str(context["run_id"]),
        status=status,
        progress_status=progress_status,
        current_phase="AUTHORIZATION" if progress_status == ReadProgressStatus.BLOCKED else "AUTHORIZED",
        started_at=str(context["started_at"]),
        goal=goal.objective,
        understanding=ReadUnderstanding(
            intent_id=intent.intent_id,
            objective=intent.objective,
            selected_capabilities=tuple(composition.selected_capabilities),
            approach=design.approach,
            accepted_direction=decision.accepted_direction,
        ),
        authorized_contract=contract,
        contracts=(contract,),
        attempts=(),
        attempt_count=0,
        failed_attempts=(),
        semantic_commit_status="NOT_STARTED",
        verified_progress="The goal is authorized for bounded execution but no attempt is recorded yet.",
        understanding_changes=(),
        unresolved_issues=issues,
        human_action_required=progress_status == ReadProgressStatus.BLOCKED,
        automatic_continuation_allowed=False,
        user_report=(
            f"Goal: {goal.objective}\n"
            f"Progress: {progress_status.value}\n"
            "Memory: CANDIDATE only. Graph: PROPOSAL_ONLY."
        ),
    )


class ProductLoopReadModel:
    """Rebuild a user read from P6.19B state and P6.21H ObjectRef pointers."""

    def __init__(self, proof_service: StateProofService) -> None:
        self.proof_service = proof_service

    def get_run(self, run_id: str) -> DurableProductRead | None:
        with self.proof_service._session() as session:
            index = repository.get_product_read_index(session, run_id)
            if index is None:
                return None
            results = repository.get_results_for_run(session, run_id)
            commits = repository.get_commits_for_run(session, run_id)
        context_ref = _ref_from_index(index, "context")
        if context_ref is None:
            raise ValueError("READ_CONTEXT_REFERENCE_MISSING")
        context = _load_json(self.proof_service, context_ref)
        report_ref = _ref_from_index(index, "report")
        snapshot_ref = _ref_from_index(index, "snapshot")
        if report_ref is not None:
            report = ProductLoopReport.model_validate(_load_json(self.proof_service, report_ref))
            read = read_from_report(
                report,
                started_at=str(context["started_at"]),
                report_object_ref=_ref_payload(report_ref),
            )
        elif snapshot_ref is not None and len(results) <= _snapshot_attempt_count(
            self.proof_service, snapshot_ref
        ):
            read = DurableProductRead.model_validate(_load_json(self.proof_service, snapshot_ref))
        else:
            read = _read_from_canonical_results(
                self.proof_service,
                context=context,
                results=results,
                commits=commits,
            )
        return _reconcile_commit(read, results, commits)

    def get_latest_status(self, run_id: str) -> ReadProgressStatus | None:
        read = self.get_run(run_id)
        return None if read is None else read.progress_status

    def get_attempt_history(self, run_id: str) -> tuple[ReadAttemptSummary, ...] | None:
        read = self.get_run(run_id)
        return None if read is None else read.attempts

    def get_accepted_result(self, run_id: str) -> ReadAcceptedResult | None:
        read = self.get_run(run_id)
        return None if read is None else read.accepted_result

    def get_user_report(self, run_id: str) -> str | None:
        read = self.get_run(run_id)
        return None if read is None else read.user_report


def _attempt_summary(
    attempt: AttemptRecord,
    reflections: Sequence[Reflection],
    index: int,
    *,
    current_phase: str | None,
) -> ReadAttemptSummary:
    reflection = next((item for item in reflections if item.attempt == attempt.attempt), None)
    validation = attempt.validation
    commit = attempt.receipt.commit or {}
    phase = current_phase if index == len(reflections) - 1 and current_phase else _attempt_phase(attempt)
    return ReadAttemptSummary(
        attempt=attempt.attempt,
        contract_id=attempt.contract_id,
        current_phase=phase,
        runtime_status=attempt.runtime_result.status,
        validation_status=validation.status,
        validation_passed=validation.validation_passed,
        failure_classification=(
            attempt.runtime_result.failure_classification or validation.failure_classification
        ),
        failure_observation=_failure_detail(attempt) if not validation.validation_passed else None,
        reflection_next_action=None if reflection is None else reflection.next_action,
        revision_created=False if reflection is None else reflection.revision_created,
        receipt_id=attempt.receipt.receipt_id,
        input_scope_version=attempt.receipt.input_scope_version,
        input_manifest_hash=attempt.receipt.input_manifest_hash,
        output_content_hash=attempt.receipt.output_content_hash,
        changed_paths=tuple(attempt.runtime_result.changed_paths),
        semantic_commit_status=str(commit.get("status", "NOT_COMMITTED")),
    )


def _failure_detail(attempt: AttemptRecord) -> str:
    if attempt.runtime_result.status != RuntimeStatus.COMPLETED:
        return attempt.runtime_result.summary
    failed = next(
        (finding.detail for finding in attempt.validation.criterion_findings if finding.status != ValidationStatus.PASSED),
        None,
    )
    if failed:
        return failed
    if attempt.validation.failure_classification is not None:
        return attempt.validation.failure_classification.value
    return "Validation did not pass."


def _validation_summary(
    validation: ValidationReport | None,
    attempts: Sequence[AttemptRecord],
) -> ReadValidationSummary | None:
    selected = validation
    attempt_number = attempts[-1].attempt if attempts else 0
    if selected is None and attempts and attempts[-1].validation.status != ValidationStatus.NOT_RUN:
        selected = attempts[-1].validation
    if selected is None:
        return None
    return ReadValidationSummary(
        attempt=attempt_number,
        status=selected.status.value,
        validation_passed=selected.validation_passed,
        technical_test_passed=selected.technical_test_passed,
        criterion_statuses=tuple(
            f"{finding.criterion_id}:{finding.status.value}" for finding in selected.criterion_findings
        ),
    )


def _accepted_result(
    attempts: Sequence[AttemptRecord],
    commit: Mapping[str, Any] | None,
) -> ReadAcceptedResult | None:
    if commit is None or str(commit.get("status")) != "COMMITTED" or not attempts:
        return None
    receipt = attempts[-1].receipt
    return ReadAcceptedResult(
        receipt_id=receipt.receipt_id,
        input_scope_version=receipt.input_scope_version,
        input_manifest_hash=receipt.input_manifest_hash,
        output_content_hash=receipt.output_content_hash,
        changed_paths=tuple(attempts[-1].runtime_result.changed_paths),
        semantic_commit_status="COMMITTED",
    )


def _progress_status(
    status: str,
    attempts: Sequence[AttemptRecord],
    automatic_continuation_allowed: bool,
) -> ReadProgressStatus:
    if status == ProductLoopStatus.COMMITTED.value:
        return ReadProgressStatus.ACCEPTED
    if status in {ProductLoopStatus.AUTHORITY_BLOCK.value, "AUTHORITY_BLOCK"}:
        return ReadProgressStatus.BLOCKED
    if status in {ProductLoopStatus.STALE_CANDIDATE.value, "STALE_CONTEXT"}:
        return ReadProgressStatus.STALE
    if status == ReadProgressStatus.ITERATION_LIMIT.value:
        return ReadProgressStatus.ITERATION_LIMIT
    if status == ProductLoopStatus.HUMAN_REVIEW_REQUIRED.value:
        return ReadProgressStatus.VERIFIED
    if status == ReadProgressStatus.VERIFIED.value:
        return ReadProgressStatus.VERIFIED
    if status == ReadProgressStatus.CORRECTED.value:
        return ReadProgressStatus.CORRECTED
    if status == ReadProgressStatus.INCOMPLETE.value:
        return ReadProgressStatus.INCOMPLETE
    if status == "WORKING":
        return ReadProgressStatus.WORKING
    if status == "REQUESTED":
        return ReadProgressStatus.REQUESTED
    if automatic_continuation_allowed:
        return ReadProgressStatus.CORRECTED
    return ReadProgressStatus.FAILED if attempts else ReadProgressStatus.WORKING


def _semantic_commit_status(
    commit: Mapping[str, Any] | None,
    accepted_result: ReadAcceptedResult | None,
    status: str,
) -> str:
    if commit is not None:
        return str(commit.get("status", "NOT_COMMITTED"))
    if accepted_result is not None:
        return accepted_result.semantic_commit_status
    if status in {"WORKING", "REQUESTED"}:
        return "NOT_STARTED"
    return "NOT_COMMITTED"


def _verified_progress(
    progress_status: ReadProgressStatus,
    current_phase: str | None,
    semantic_commit_status: str,
) -> str:
    phase = current_phase or _phase_for(progress_status)
    return {
        ReadProgressStatus.ACCEPTED: "Verified acceptance checks passed and semantic commit succeeded.",
        ReadProgressStatus.VERIFIED: "Validation passed; semantic commit is pending or human-gated.",
        ReadProgressStatus.BLOCKED: "Execution was blocked by an authority or scope gate.",
        ReadProgressStatus.STALE: "The receipt was retained, but semantic commit was stale.",
        ReadProgressStatus.ITERATION_LIMIT: "The bounded attempt budget ended with retained evidence and no accepted commit.",
        ReadProgressStatus.CORRECTED: "A bounded correction was produced from the observed failure.",
        ReadProgressStatus.FAILED: "The latest attempt did not satisfy the declared validation.",
        ReadProgressStatus.INCOMPLETE: f"Execution is incomplete; the last durable stage is {phase}.",
        ReadProgressStatus.WORKING: f"Execution is working; the last durable stage is {phase}.",
        ReadProgressStatus.REQUESTED: "The goal is requested and awaiting bounded execution.",
    }[progress_status]


def _phase_for(progress_status: ReadProgressStatus) -> str:
    return {
        ReadProgressStatus.ACCEPTED: "ACCEPTED",
        ReadProgressStatus.VERIFIED: "VERIFY",
        ReadProgressStatus.BLOCKED: "AUTHORITY_BLOCK",
        ReadProgressStatus.STALE: "STALE",
        ReadProgressStatus.ITERATION_LIMIT: "STOPPED",
        ReadProgressStatus.CORRECTED: "REVISE",
        ReadProgressStatus.FAILED: "STOPPED",
        ReadProgressStatus.INCOMPLETE: "RESULT_RECEIVED",
        ReadProgressStatus.WORKING: "AUTHORIZED",
        ReadProgressStatus.REQUESTED: "REQUESTED",
    }[progress_status]


def _attempt_phase(attempt: AttemptRecord) -> str:
    if attempt.runtime_result.status == RuntimeStatus.BLOCKED:
        return "AUTHORITY_BLOCK"
    if attempt.validation.status == ValidationStatus.NOT_RUN:
        return "RESULT_RECEIVED"
    if attempt.validation.validation_passed:
        return "VERIFY"
    return "REFLECT"


def _is_iteration_limit(report: ProductLoopReport) -> bool:
    if report.status != ProductLoopStatus.REJECTED or not report.attempts or not report.contracts:
        return False
    last = report.attempts[-1]
    return (
        not last.validation.validation_passed
        and last.validation.failure_classification is not None
        and last.validation.failure_classification.value == "IMPLEMENTATION_DEFECT"
        and len(report.attempts) >= report.contracts[-1].retry_policy.max_attempts
        and any(item.stop_reason == "REVISION_NOT_AVAILABLE_OR_BUDGET_EXHAUSTED" for item in report.reflections)
    )


def _user_report(
    *,
    goal: str,
    progress_status: ReadProgressStatus,
    attempts: int,
    semantic_commit_status: str,
    issues: Sequence[str],
    human_action_required: bool,
) -> str:
    unresolved = "none" if not issues else "; ".join(issues)
    return (
        f"Goal: {goal}\n"
        f"Progress: {progress_status.value}\n"
        f"Attempts: {attempts}\n"
        f"Semantic commit: {semantic_commit_status}\n"
        f"Human action required: {'yes' if human_action_required else 'no'}\n"
        f"Unresolved: {unresolved}\n"
        "Memory: CANDIDATE only. Graph: PROPOSAL_ONLY."
    )


def _ref_from_index(index: Mapping[str, Any], kind: str) -> ObjectRef | None:
    object_key = {
        "context": "context_object_id",
        "snapshot": "latest_snapshot_object_id",
        "report": "final_report_object_id",
    }[kind]
    if index.get(object_key) is None:
        return None
    prefix = {"context": "context", "snapshot": "snapshot", "report": "report"}[kind]
    indexed_hash = index.get(
        {"context": "context_hash", "snapshot": "latest_snapshot_hash", "report": "final_report_hash"}[kind]
    )
    object_hash = index.get(f"{prefix}_content_hash")
    if indexed_hash != object_hash:
        raise ValueError("READ_INDEX_HASH_MISMATCH")
    return ObjectRef(
        object_id=str(index[object_key]),
        content_hash=str(index[f"{prefix}_content_hash"]),
        size_bytes=int(index[f"{prefix}_size_bytes"]),
        media_type=str(index[f"{prefix}_media_type"]),
        storage_uri=str(index[f"{prefix}_storage_uri"]),
        object_kind=str(index[f"{prefix}_object_kind"]),
    )


def _ref_payload(ref: ObjectRef) -> dict[str, Any]:
    return {
        "object_id": ref.object_id,
        "content_hash": ref.content_hash,
        "size_bytes": ref.size_bytes,
        "media_type": ref.media_type,
        "storage_uri": ref.storage_uri,
        "object_kind": ref.object_kind,
    }


def _load_json(proof_service: StateProofService, ref: ObjectRef) -> dict[str, Any]:
    try:
        payload = json.loads(proof_service.object_store.read(ref).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("READ_OBJECT_INVALID") from exc
    if not isinstance(payload, dict):
        raise TypeError("READ_OBJECT_NOT_OBJECT")
    return payload


def _snapshot_attempt_count(proof_service: StateProofService, ref: ObjectRef) -> int:
    return int(_load_json(proof_service, ref).get("attempt_count", 0))


def _read_from_canonical_results(
    proof_service: StateProofService,
    *,
    context: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    commits: Sequence[Mapping[str, Any]],
) -> DurableProductRead:
    base = read_from_context(context)
    commit_by_result = {str(item["result_id"]): item for item in commits}
    attempts: list[ReadAttemptSummary] = []
    failures: list[ReadFailureSummary] = []
    for row in results:
        runtime = _runtime_from_result(proof_service, row)
        commit = commit_by_result.get(str(row["id"]))
        failure = runtime.failure_classification
        attempt = ReadAttemptSummary(
            attempt=int(row["attempt"]),
            contract_id=str(row.get("contract_id") or base.authorized_contract.work_contract_id),
            current_phase="ACCEPTED" if commit and commit.get("status") == "COMMITTED" else "RESULT_RECEIVED",
            runtime_status=runtime.status,
            validation_status=None,
            validation_passed=None,
            failure_classification=failure,
            failure_observation=runtime.summary if failure is not None else None,
            reflection_next_action=None,
            revision_created=False,
            receipt_id=str(row["id"]),
            input_scope_version=int(row["input_scope_version"]),
            input_manifest_hash=str(row["input_manifest_hash"]),
            output_content_hash=(str(row["output_content_hash"]) if row.get("output_content_hash") else None),
            changed_paths=tuple(runtime.changed_paths),
            semantic_commit_status=str(commit.get("status")) if commit else "NOT_COMMITTED",
        )
        attempts.append(attempt)
        if failure is not None:
            failures.append(
                ReadFailureSummary(
                    attempt=attempt.attempt,
                    classification=failure.value,
                    detail=runtime.summary,
                )
            )
    committed = next((item for item in reversed(commits) if item.get("status") == "COMMITTED"), None)
    if committed is not None and attempts:
        last = attempts[-1]
        accepted = ReadAcceptedResult(
            receipt_id=last.receipt_id or "",
            input_scope_version=last.input_scope_version or 0,
            input_manifest_hash=last.input_manifest_hash or "",
            output_content_hash=last.output_content_hash,
            changed_paths=last.changed_paths,
            semantic_commit_status="COMMITTED",
        )
        return base.model_copy(
            update={
                "status": ProductLoopStatus.COMMITTED.value,
                "progress_status": ReadProgressStatus.ACCEPTED,
                "current_phase": "ACCEPTED",
                "attempts": tuple(attempts),
                "attempt_count": len(attempts),
                "failed_attempts": tuple(failures),
                "accepted_result": accepted,
                "semantic_commit_status": "COMMITTED",
                "verified_progress": "Verified acceptance checks and semantic commit are recorded in canonical state.",
                "unresolved_issues": (),
                "human_action_required": False,
                "user_report": _user_report(
                    goal=base.goal,
                    progress_status=ReadProgressStatus.ACCEPTED,
                    attempts=len(attempts),
                    semantic_commit_status="COMMITTED",
                    issues=(),
                    human_action_required=False,
                ),
            }
        )
    return base.model_copy(
        update={
            "status": "INCOMPLETE",
            "progress_status": ReadProgressStatus.INCOMPLETE,
            "current_phase": "RESULT_RECEIVED" if attempts else base.current_phase,
            "attempts": tuple(attempts),
            "attempt_count": len(attempts),
            "failed_attempts": tuple(failures),
            "semantic_commit_status": "NOT_COMMITTED" if attempts else "NOT_STARTED",
            "verified_progress": "Execution is incomplete; only the durable result stage is known.",
            "unresolved_issues": ("Validation observation or final report was not durably recorded.",),
            "human_action_required": True,
            "user_report": _user_report(
                goal=base.goal,
                progress_status=ReadProgressStatus.INCOMPLETE,
                attempts=len(attempts),
                semantic_commit_status="NOT_COMMITTED" if attempts else "NOT_STARTED",
                issues=("Validation observation or final report was not durably recorded.",),
                human_action_required=True,
            ),
        }
    )


def _runtime_from_result(proof_service: StateProofService, row: Mapping[str, Any]) -> RuntimeResult:
    parsed = row.get("parsed_payload_json") or {}
    answer = parsed.get("answer") if isinstance(parsed, Mapping) else None
    if isinstance(answer, str):
        try:
            return RuntimeResult.model_validate(json.loads(answer))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("READ_RUNTIME_RESULT_INVALID") from exc
    output_uri = row.get("output_storage_uri")
    if output_uri:
        ref = ObjectRef(
            object_id=str(row["output_object_id"]),
            content_hash=str(row["output_content_hash"]),
            size_bytes=int(row["output_size_bytes"]),
            media_type=str(row["output_media_type"]),
            storage_uri=str(output_uri),
            object_kind=str(row["output_object_kind"]),
        )
        payload = _load_json(proof_service, ref)
        answer = payload.get("answer")
        if isinstance(answer, str):
            return RuntimeResult.model_validate(json.loads(answer))
    raise ValueError("READ_RUNTIME_RESULT_MISSING")


def _reconcile_commit(
    read: DurableProductRead,
    results: Sequence[Mapping[str, Any]],
    commits: Sequence[Mapping[str, Any]],
) -> DurableProductRead:
    if not commits:
        return read
    latest = commits[-1]
    if latest.get("status") == "STALE_CANDIDATE":
        return read.model_copy(
            update={
                "status": ProductLoopStatus.STALE_CANDIDATE.value,
                "progress_status": ReadProgressStatus.STALE,
                "current_phase": "STALE",
                "semantic_commit_status": "STALE_CANDIDATE",
                "accepted_result": None,
                "human_action_required": True,
                "unresolved_issues": tuple(
                    dict.fromkeys(
                        (*read.unresolved_issues, "The candidate was not committed because its scope version was stale.")
                    )
                ),
            }
        )
    if latest.get("status") == "COMMITTED" and read.progress_status != ReadProgressStatus.ACCEPTED:
        matching = next((item for item in reversed(read.attempts) if item.receipt_id == str(latest["result_id"])), None)
        accepted = None
        if matching is not None:
            accepted = ReadAcceptedResult(
                receipt_id=matching.receipt_id or "",
                input_scope_version=matching.input_scope_version or 0,
                input_manifest_hash=matching.input_manifest_hash or "",
                output_content_hash=matching.output_content_hash,
                changed_paths=matching.changed_paths,
                semantic_commit_status="COMMITTED",
            )
        return read.model_copy(
            update={
                "status": ProductLoopStatus.COMMITTED.value,
                "progress_status": ReadProgressStatus.ACCEPTED,
                "current_phase": "ACCEPTED",
                "semantic_commit_status": "COMMITTED",
                "accepted_result": accepted,
                "unresolved_issues": tuple(
                    item for item in read.unresolved_issues if "commit" not in item.lower()
                ),
                "human_action_required": False,
            }
        )
    return read
