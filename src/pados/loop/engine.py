# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pados.loop.schemas import (
    DeltaClassification,
    LoopDecision,
    LoopStatus,
    ProgressDelta,
    ProgressSnapshot,
    StepStatus,
    StopPolicy,
)

ALLOWED_LOOP_TRANSITIONS: dict[LoopStatus, set[LoopStatus]] = {
    LoopStatus.DRAFT: {LoopStatus.PENDING_APPROVAL},
    LoopStatus.PENDING_APPROVAL: {LoopStatus.READY, LoopStatus.CANCELLED},
    LoopStatus.READY: {LoopStatus.RUNNING, LoopStatus.CANCELLED},
    LoopStatus.RUNNING: {LoopStatus.VALIDATING, LoopStatus.PAUSED, LoopStatus.CANCELLED, LoopStatus.TIMED_OUT},
    LoopStatus.VALIDATING: {
        LoopStatus.RUNNING,
        LoopStatus.WAITING_HUMAN,
        LoopStatus.BLOCKED,
        LoopStatus.COMPLETED,
        LoopStatus.PARTIALLY_COMPLETED,
        LoopStatus.FAILED_RECOVERABLE,
        LoopStatus.FAILED_TERMINAL,
    },
    LoopStatus.WAITING_HUMAN: {LoopStatus.RUNNING, LoopStatus.PAUSED, LoopStatus.CANCELLED},
    LoopStatus.PAUSED: {LoopStatus.RUNNING, LoopStatus.CANCELLED},
    LoopStatus.FAILED_RECOVERABLE: {LoopStatus.WAITING_HUMAN, LoopStatus.CANCELLED},
}
TERMINAL_LOOP_STATUSES = {
    LoopStatus.COMPLETED,
    LoopStatus.PARTIALLY_COMPLETED,
    LoopStatus.FAILED_TERMINAL,
    LoopStatus.CANCELLED,
    LoopStatus.TIMED_OUT,
    LoopStatus.BLOCKED,
}


def assert_loop_transition(current: LoopStatus, desired: LoopStatus) -> None:
    if current in TERMINAL_LOOP_STATUSES:
        raise ValueError("terminal loop state cannot transition")
    if desired not in ALLOWED_LOOP_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid loop transition {current.value}->{desired.value}")


def calculate_progress_delta(before: ProgressSnapshot, after: ProgressSnapshot) -> ProgressDelta:
    scope_changed = before.scope_hash != after.scope_hash
    state_changed = before.state_hash != after.state_hash
    criteria_delta = after.acceptance_criteria_met - before.acceptance_criteria_met
    tests_passed_delta = after.tests_passed - before.tests_passed
    tests_failed_delta = after.tests_failed - before.tests_failed
    blocker_delta = after.blocker_count - before.blocker_count
    artifact_delta = after.artifact_count - before.artifact_count
    score = criteria_delta * 5 + tests_passed_delta * 2 + artifact_delta - tests_failed_delta * 3 - blocker_delta * 4
    if scope_changed:
        classification = DeltaClassification.INCOMPARABLE
    elif score > 0:
        classification = DeltaClassification.POSITIVE
    elif score < 0:
        classification = DeltaClassification.NEGATIVE
    else:
        classification = DeltaClassification.NEUTRAL
    return ProgressDelta(
        criteria_met_delta=criteria_delta,
        tests_passed_delta=tests_passed_delta,
        tests_failed_delta=tests_failed_delta,
        blocker_delta=blocker_delta,
        artifact_delta=artifact_delta,
        scope_changed=scope_changed,
        state_changed=state_changed,
        progress_score=score,
        classification=classification,
    )


@dataclass(frozen=True)
class DecisionInput:
    validation_status: str
    step_status: StepStatus
    delta: ProgressDelta
    all_success_criteria_met: bool
    unresolved_blockers: int
    attempt_number: int
    worker_failures: int
    current_iteration: int
    no_progress_streak: int
    negative_progress_streak: int
    stop_policy: StopPolicy
    security_violation: bool = False
    approval_expired: bool = False
    scope_change_required: bool = False
    external_dependency_missing: bool = False
    transient_failure: bool = False
    retry_allowed: bool = False


def decide_next_action(payload: DecisionInput) -> tuple[LoopDecision, str, LoopStatus]:
    policy = payload.stop_policy
    if payload.security_violation:
        return LoopDecision.FAIL_TERMINAL, "security boundary violation", LoopStatus.FAILED_TERMINAL
    if policy.stop_on_scope_change and (payload.delta.scope_changed or payload.scope_change_required):
        return LoopDecision.WAIT_FOR_HUMAN, "scope change requires approval", LoopStatus.WAITING_HUMAN
    if policy.stop_on_approval_expiry and payload.approval_expired:
        return LoopDecision.WAIT_FOR_HUMAN, "approval expired", LoopStatus.WAITING_HUMAN
    if payload.external_dependency_missing:
        return LoopDecision.WAIT_FOR_HUMAN, "external dependency missing", LoopStatus.WAITING_HUMAN
    if payload.step_status == StepStatus.BLOCKED:
        return LoopDecision.WAIT_FOR_HUMAN, "blocked step requires human review", LoopStatus.WAITING_HUMAN
    if payload.current_iteration >= policy.max_iterations:
        return LoopDecision.FAIL_RECOVERABLE, "max iterations exhausted", LoopStatus.FAILED_RECOVERABLE
    if payload.worker_failures > policy.max_worker_failures:
        return LoopDecision.FAIL_RECOVERABLE, "worker failure limit exceeded", LoopStatus.FAILED_RECOVERABLE
    if payload.negative_progress_streak >= policy.negative_progress_limit:
        return LoopDecision.PAUSE, "negative progress threshold reached", LoopStatus.PAUSED
    if payload.no_progress_streak >= policy.no_progress_limit:
        return LoopDecision.WAIT_FOR_HUMAN, "no-progress threshold reached", LoopStatus.WAITING_HUMAN
    if payload.all_success_criteria_met and payload.validation_status == "PASSED" and payload.unresolved_blockers == 0:
        return LoopDecision.COMPLETE, "all success criteria met", LoopStatus.COMPLETED
    if payload.validation_status == "PASSED" and payload.step_status == StepStatus.PASSED:
        return LoopDecision.ADVANCE_STEP, "step validation passed", LoopStatus.RUNNING
    if (
        payload.validation_status == "FAILED"
        and payload.transient_failure
        and payload.retry_allowed
        and payload.attempt_number < policy.max_attempts_per_step
        and payload.delta.classification != DeltaClassification.NEGATIVE
    ):
        return LoopDecision.RETRY_SAME_STEP, "transient failure retry allowed", LoopStatus.RUNNING
    if payload.validation_status == "FAILED":
        return LoopDecision.FAIL_RECOVERABLE, "validation failed", LoopStatus.FAILED_RECOVERABLE
    return LoopDecision.CONTINUE, "policy permits next iteration", LoopStatus.RUNNING


def next_streaks(history: list[DeltaClassification]) -> tuple[int, int]:
    no_progress = 0
    negative = 0
    for item in reversed(history):
        if item == DeltaClassification.NEUTRAL:
            no_progress += 1
        else:
            break
    for item in reversed(history):
        if item == DeltaClassification.NEGATIVE:
            negative += 1
        else:
            break
    return no_progress, negative


def ensure_stop_policy_is_bounded(policy: StopPolicy) -> None:
    values = policy.model_dump()
    numeric = {key: value for key, value in values.items() if isinstance(value, int) and not isinstance(value, bool)}
    if any(value <= 0 for value in numeric.values()):
        raise ValueError("stop policy limits must be positive and bounded")
    if Counter(numeric.values())[0]:
        raise ValueError("zero cannot mean unlimited")
