from __future__ import annotations

from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.models import (
    AuthorizationStatus,
    InstallationStatus,
    RegistrationStatus,
    SchedulingCandidate,
    SelectionStatus,
)
from pados.work_protocol.planning import plan_scheduling_candidates, submit_to_existing_scheduler
from pados.work_protocol.recommendations import recommend_capability_gap
from tests.work_protocol.test_completion import _envelope, _result


class ExistingQueue:
    def __init__(self) -> None:
        self.offered: list[str] = []

    def offer(self, candidate: SchedulingCandidate) -> str:
        self.offered.append(candidate.candidate_id)
        return f"existing-loop-scheduler:{candidate.candidate_id}"


def test_candidate_planner_delegates_order_to_one_existing_scheduler() -> None:
    task = _envelope()
    result_a = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft")
    result_b = _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Partial check")
    coverage = build_coverage(task, (result_a, result_b))
    candidates = plan_scheduling_candidates(task, coverage, available_resource_goals=frozenset({"goal-b2"}))
    queue = ExistingQueue()

    scheduler_receipts = submit_to_existing_scheduler(queue, candidates + candidates)

    assert [candidate.goal_id for candidate in candidates] == ["goal-b2"]
    assert len(queue.offered) == 1
    assert len(scheduler_receipts) == 1
    assert candidates[0].reason.value == "REQUIRED_RESOURCE_ONLINE"
    assert candidates[0].estimated_latency_ms is None
    assert candidates[0].estimated_cost_microunits is None


def test_missing_cad_or_ocr_recommends_without_install_register_or_authorize() -> None:
    original = _envelope()
    task = original.model_copy(
        update={
            "normalized_goals": (
                original.normalized_goals[0].model_copy(
                    update={"required_capabilities": ("CAD_EDIT", "OCR_TEXT")}
                ),
                *original.normalized_goals[1:],
            )
        }
    )
    goal = task.normalized_goals[0]

    recommendations = recommend_capability_gap(
        task,
        goal,
        registered_capabilities=frozenset(),
    )

    assert [(item.required_capability, item.candidate_integration) for item in recommendations] == [
        ("CAD_EDIT", "solidworks-mcp-adapter"),
        ("OCR_TEXT", "ocr-capability-pack-backend"),
    ]
    for recommendation in recommendations:
        assert recommendation.relevance == "CAPABILITY_CATEGORY_MATCH"
        assert recommendation.match_rationale
        assert recommendation.auto_install is False
        assert recommendation.installation_status is InstallationStatus.NOT_INSTALLED
        assert recommendation.registration_status is RegistrationStatus.NOT_REGISTERED
        assert recommendation.authorization_status is AuthorizationStatus.NOT_AUTHORIZED
        assert recommendation.selection_status is SelectionStatus.NOT_SELECTED
        assert recommendation.license_status == "UNVERIFIED"
        assert recommendation.provenance_status == "UNVERIFIED"
