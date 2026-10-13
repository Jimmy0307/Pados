import pytest

from pados.loop.engine import (
    DecisionInput,
    assert_loop_transition,
    calculate_progress_delta,
    decide_next_action,
    ensure_stop_policy_is_bounded,
    next_streaks,
)
from pados.loop.schemas import (
    DeltaClassification,
    LoopDecision,
    LoopStatus,
    ProgressSnapshot,
    StepStatus,
    StopPolicy,
)


def snapshot(
    *,
    criteria: int = 0,
    passed: int = 0,
    failed: int = 0,
    blockers: int = 0,
    artifacts: int = 0,
    scope: str = "scope-a",
    state: str = "state-a",
) -> ProgressSnapshot:
    return ProgressSnapshot(
        acceptance_criteria_total=3,
        acceptance_criteria_met=criteria,
        tests_total=passed + failed,
        tests_passed=passed,
        tests_failed=failed,
        blocker_count=blockers,
        artifact_count=artifacts,
        validation_status="PASSED" if failed == 0 else "FAILED",
        scope_hash=scope,
        state_hash=state,
    )


def test_loop_state_machine_allows_expected_path() -> None:
    assert_loop_transition(LoopStatus.DRAFT, LoopStatus.PENDING_APPROVAL)
    assert_loop_transition(LoopStatus.PENDING_APPROVAL, LoopStatus.READY)
    assert_loop_transition(LoopStatus.READY, LoopStatus.RUNNING)
    assert_loop_transition(LoopStatus.RUNNING, LoopStatus.VALIDATING)
    assert_loop_transition(LoopStatus.VALIDATING, LoopStatus.COMPLETED)


@pytest.mark.parametrize(
    ("current", "desired"),
    [
        (LoopStatus.DRAFT, LoopStatus.RUNNING),
        (LoopStatus.PENDING_APPROVAL, LoopStatus.COMPLETED),
        (LoopStatus.COMPLETED, LoopStatus.RUNNING),
        (LoopStatus.CANCELLED, LoopStatus.RUNNING),
        (LoopStatus.FAILED_TERMINAL, LoopStatus.RUNNING),
    ],
)
def test_loop_state_machine_rejects_invalid_or_terminal_transitions(
    current: LoopStatus,
    desired: LoopStatus,
) -> None:
    with pytest.raises(ValueError):
        assert_loop_transition(current, desired)


def test_progress_delta_classifies_positive_neutral_negative_and_incomparable() -> None:
    assert calculate_progress_delta(snapshot(criteria=1), snapshot(criteria=2)).classification == (
        DeltaClassification.POSITIVE
    )
    assert calculate_progress_delta(snapshot(criteria=1), snapshot(criteria=1)).classification == (
        DeltaClassification.NEUTRAL
    )
    assert calculate_progress_delta(snapshot(passed=3), snapshot(passed=2, failed=1)).classification == (
        DeltaClassification.NEGATIVE
    )
    assert calculate_progress_delta(snapshot(scope="a"), snapshot(scope="b")).classification == (
        DeltaClassification.INCOMPARABLE
    )


def test_decision_complete_requires_validation_and_no_blockers() -> None:
    delta = calculate_progress_delta(snapshot(criteria=2), snapshot(criteria=3, passed=2, artifacts=1))
    decision, reason, status = decide_next_action(
        DecisionInput(
            validation_status="PASSED",
            step_status=StepStatus.PASSED,
            delta=delta,
            all_success_criteria_met=True,
            unresolved_blockers=0,
            attempt_number=1,
            worker_failures=0,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=0,
            stop_policy=StopPolicy(),
        )
    )
    assert decision == LoopDecision.COMPLETE
    assert "success criteria" in reason
    assert status == LoopStatus.COMPLETED


def test_decision_waits_for_human_on_scope_change() -> None:
    delta = calculate_progress_delta(snapshot(scope="before"), snapshot(scope="after"))
    decision, reason, status = decide_next_action(
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
    assert "scope" in reason
    assert status == LoopStatus.WAITING_HUMAN


def test_blocked_step_waits_for_human_before_negative_progress_pause() -> None:
    delta = calculate_progress_delta(snapshot(passed=1), snapshot(passed=1, failed=1, blockers=1))
    decision, reason, status = decide_next_action(
        DecisionInput(
            validation_status="FAILED",
            step_status=StepStatus.BLOCKED,
            delta=delta,
            all_success_criteria_met=False,
            unresolved_blockers=1,
            attempt_number=1,
            worker_failures=0,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=1,
            stop_policy=StopPolicy(negative_progress_limit=1),
        )
    )
    assert decision == LoopDecision.WAIT_FOR_HUMAN
    assert "blocked" in reason
    assert status == LoopStatus.WAITING_HUMAN


def test_retry_requires_transient_failure_and_attempts_remaining() -> None:
    delta = calculate_progress_delta(snapshot(), snapshot())
    decision, _, status = decide_next_action(
        DecisionInput(
            validation_status="FAILED",
            step_status=StepStatus.FAILED,
            delta=delta,
            all_success_criteria_met=False,
            unresolved_blockers=0,
            attempt_number=1,
            worker_failures=1,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=0,
            stop_policy=StopPolicy(max_attempts_per_step=2),
            transient_failure=True,
            retry_allowed=True,
        )
    )
    assert decision == LoopDecision.RETRY_SAME_STEP
    assert status == LoopStatus.RUNNING


def test_no_progress_and_negative_progress_stop_policies_are_enforced() -> None:
    assert next_streaks([DeltaClassification.POSITIVE, DeltaClassification.NEUTRAL, DeltaClassification.NEUTRAL]) == (
        2,
        0,
    )
    assert next_streaks([DeltaClassification.NEUTRAL, DeltaClassification.NEGATIVE]) == (0, 1)

    neutral = calculate_progress_delta(snapshot(), snapshot())
    decision, _, status = decide_next_action(
        DecisionInput(
            validation_status="PASSED",
            step_status=StepStatus.PASSED,
            delta=neutral,
            all_success_criteria_met=False,
            unresolved_blockers=0,
            attempt_number=1,
            worker_failures=0,
            current_iteration=2,
            no_progress_streak=2,
            negative_progress_streak=0,
            stop_policy=StopPolicy(no_progress_limit=2),
        )
    )
    assert decision == LoopDecision.WAIT_FOR_HUMAN
    assert status == LoopStatus.WAITING_HUMAN


def test_external_dependency_missing_requires_human_before_negative_progress_pause() -> None:
    negative = calculate_progress_delta(snapshot(), snapshot(failed=1, blockers=1))
    decision, reason, status = decide_next_action(
        DecisionInput(
            validation_status="FAILED",
            step_status=StepStatus.FAILED,
            delta=negative,
            all_success_criteria_met=False,
            unresolved_blockers=1,
            attempt_number=1,
            worker_failures=0,
            current_iteration=1,
            no_progress_streak=0,
            negative_progress_streak=1,
            stop_policy=StopPolicy(),
            external_dependency_missing=True,
        )
    )
    assert decision == LoopDecision.WAIT_FOR_HUMAN
    assert reason == "external dependency missing"
    assert status == LoopStatus.WAITING_HUMAN


def test_stop_policy_cannot_use_unbounded_zero_or_negative_limits() -> None:
    ensure_stop_policy_is_bounded(StopPolicy())
    with pytest.raises(ValueError):
        ensure_stop_policy_is_bounded(StopPolicy.model_construct(max_iterations=0))
