import pytest
from pydantic import ValidationError

from pados.loop.engine import DecisionInput, calculate_progress_delta, decide_next_action
from pados.loop.schemas import (
    LoopDecision,
    LoopStatus,
    PlanStepCreate,
    ProgressSnapshot,
    StepStatus,
    StepType,
    StopPolicy,
    TaskGoalCreate,
)
from pados.worker.schemas import CapabilityId


def _snapshot(scope: str = "stable") -> ProgressSnapshot:
    return ProgressSnapshot(
        acceptance_criteria_total=1,
        acceptance_criteria_met=0,
        tests_total=0,
        tests_passed=0,
        tests_failed=0,
        blocker_count=0,
        artifact_count=0,
        validation_status="NOT_RUN",
        scope_hash=scope,
        state_hash="state",
    )


def test_goal_scope_injection_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskGoalCreate(
            project_id="11111111-1111-4111-8111-111111111111",
            request_id="22222222-2222-4222-8222-222222222222",
            objective="validate repo",
            success_criteria=["tests pass"],
            scope_paths=["../outside"],
            approval_id="33333333-3333-4333-8333-333333333333",
            idempotency_key="scope-injection",
        )


def test_security_violation_fails_terminal() -> None:
    delta = calculate_progress_delta(_snapshot(), _snapshot())
    decision, reason, status = decide_next_action(
        DecisionInput(
            validation_status="FAILED",
            step_status=StepStatus.FAILED,
            delta=delta,
            all_success_criteria_met=False,
            unresolved_blockers=1,
            attempt_number=1,
            worker_failures=1,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=0,
            stop_policy=StopPolicy(),
            security_violation=True,
        )
    )
    assert decision == LoopDecision.FAIL_TERMINAL
    assert "security" in reason
    assert status == LoopStatus.FAILED_TERMINAL


def test_scope_hash_manipulation_waits_for_human() -> None:
    delta = calculate_progress_delta(_snapshot("a"), _snapshot("b"))
    decision, _, status = decide_next_action(
        DecisionInput(
            validation_status="PASSED",
            step_status=StepStatus.PASSED,
            delta=delta,
            all_success_criteria_met=False,
            unresolved_blockers=0,
            attempt_number=1,
            worker_failures=0,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=0,
            stop_policy=StopPolicy(),
        )
    )
    assert decision == LoopDecision.WAIT_FOR_HUMAN
    assert status == LoopStatus.WAITING_HUMAN


def test_plan_step_path_traversal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanStepCreate(
            sequence=1,
            title="bad path",
            step_type=StepType.RUN_VALIDATION,
            required_capability=CapabilityId.RUN_TESTS,
            allowed_paths=["../outside"],
        )


def test_plan_step_command_injection_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanStepCreate(
            sequence=1,
            title="bad command",
            step_type=StepType.RUN_VALIDATION,
            required_capability=CapabilityId.RUN_TESTS,
            allowed_actions=["RUN_TESTS; whoami"],
        )


def test_negative_limit_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        StopPolicy(max_iterations=-1)


def test_approval_expiry_waits_for_human_without_retrying() -> None:
    delta = calculate_progress_delta(_snapshot(), _snapshot())
    decision, reason, status = decide_next_action(
        DecisionInput(
            validation_status="FAILED",
            step_status=StepStatus.FAILED,
            delta=delta,
            all_success_criteria_met=False,
            unresolved_blockers=0,
            attempt_number=1,
            worker_failures=0,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=0,
            stop_policy=StopPolicy(),
            approval_expired=True,
            transient_failure=True,
            retry_allowed=True,
        )
    )
    assert decision == LoopDecision.WAIT_FOR_HUMAN
    assert "approval" in reason
    assert status == LoopStatus.WAITING_HUMAN
