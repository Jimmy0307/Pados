from pados.agents.maker_checker import (
    release_allowed,
    reviewer_create_finding,
    run_maker_checker_loop,
)
from pados.core.task_service import create_project, create_task
from pados.db import session_scope


def test_reviewer_high_finding_blocks_release_until_fixed() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p4-review-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="P4", objective="review")
        reviewer_create_finding(
            session,
            task_id=task_id,
            severity="high",
            file_path="x.py",
            finding="unsafe release",
            evidence="high finding evidence",
            recommended_fix="resolve high finding",
        )
        assert release_allowed(session, task_id=task_id) is False


def test_maker_checker_resolves_finding_and_allows_release() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p4-loop-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="P4", objective="maker checker")
        result = run_maker_checker_loop(session, task_id=task_id, inject_high_finding=True)
        assert result.finding_id is not None
        assert result.release_allowed is True
        assert result.status == "READY_FOR_RELEASE"

