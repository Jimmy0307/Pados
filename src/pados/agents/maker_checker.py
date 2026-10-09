# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class MakerCheckerResult:
    task_id: str
    status: str
    release_allowed: bool
    finding_id: str | None


def _open_high_findings(session: Session, task_id: str) -> int:
    return int(
        session.execute(
            text(
                """
                SELECT count(*)
                FROM review_findings
                WHERE task_id = :task_id
                  AND severity IN ('critical', 'high')
                  AND status = 'open'
                """
            ),
            {"task_id": task_id},
        ).scalar_one()
    )


def reviewer_create_finding(
    session: Session,
    *,
    task_id: str,
    severity: str,
    file_path: str,
    finding: str,
    evidence: str,
    recommended_fix: str,
) -> str:
    finding_id = session.execute(
        text(
            """
            INSERT INTO review_findings
              (task_id, severity, category, file_path, finding, evidence, recommended_fix, status)
            VALUES
              (:task_id, :severity, 'p4-review', :file_path, :finding,
               :evidence, :recommended_fix, 'open')
            RETURNING id
            """
        ),
        {
            "task_id": task_id,
            "severity": severity,
            "file_path": file_path,
            "finding": finding,
            "evidence": evidence,
            "recommended_fix": recommended_fix,
        },
    ).scalar_one()
    return str(finding_id)


def fixer_resolve_finding(session: Session, *, finding_id: str, run_id: str | None = None) -> None:
    session.execute(
        text(
            """
            UPDATE review_findings
            SET status = 'resolved',
                resolved_by_run_id = CAST(:run_id AS uuid),
                resolved_at = now()
            WHERE id = :finding_id AND status = 'open'
            """
        ),
        {"finding_id": finding_id, "run_id": run_id},
    )


def release_allowed(session: Session, *, task_id: str) -> bool:
    return _open_high_findings(session, task_id) == 0


def run_maker_checker_loop(session: Session, *, task_id: str, inject_high_finding: bool) -> MakerCheckerResult:
    session.execute(
        text(
            """
            INSERT INTO test_runs (task_id, run_type, command, status, exit_code, summary, completed_at)
            VALUES (:task_id, 'qa', 'p4 deterministic qa', 'passed', 0, 'qa pass before review', now())
            """
        ),
        {"task_id": task_id},
    )
    finding_id = None
    if inject_high_finding:
        finding_id = reviewer_create_finding(
            session,
            task_id=task_id,
            severity="high",
            file_path="runtime/artifacts/p4/build.txt",
            finding="release evidence missing",
            evidence="reviewer observed missing release manifest",
            recommended_fix="generate release manifest before release gate",
        )
    if finding_id:
        fixer_resolve_finding(session, finding_id=finding_id)
    session.execute(
        text(
            """
            INSERT INTO test_runs (task_id, run_type, command, status, exit_code, summary, completed_at)
            VALUES (:task_id, 'qa', 'p4 deterministic qa after fixer', 'passed', 0,
                    'qa pass after fixer', now())
            """
        ),
        {"task_id": task_id},
    )
    allowed = release_allowed(session, task_id=task_id)
    status = "READY_FOR_RELEASE" if allowed else "BLOCKED"
    session.execute(
        text("UPDATE tasks SET status = :status, current_gate = 'G8', updated_at = now() WHERE id = :task_id"),
        {"task_id": task_id, "status": status},
    )
    return MakerCheckerResult(task_id=task_id, status=status, release_allowed=allowed, finding_id=finding_id)

