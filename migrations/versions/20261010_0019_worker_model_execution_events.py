"""Allow an explicit blocked event for measured model refusal.

Revision ID: 20261010_0019
Revises: 20261010_0018
Create Date: 2026-10-10
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20261010_0019"
down_revision: str | None = "20261010_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENTS = (
    "JOB_CREATED",
    "ROUTING_STARTED",
    "WORKER_SELECTED",
    "WORKER_UNAVAILABLE",
    "JOB_QUEUED",
    "JOB_STARTED",
    "CONTEXT_MATERIALIZED",
    "PROCESS_STARTED",
    "OUTPUT_CAPTURED",
    "ARTIFACT_REGISTERED",
    "DIFF_CAPTURED",
    "VALIDATION_STARTED",
    "VALIDATION_PASSED",
    "VALIDATION_FAILED",
    "JOB_SUCCEEDED",
    "JOB_FAILED",
    "JOB_BLOCKED",
    "JOB_TIMED_OUT",
    "JOB_CANCELLED",
)
_PREVIOUS_EVENTS = tuple(event for event in _EVENTS if event != "JOB_BLOCKED")


def _check_expression(event_types: tuple[str, ...]) -> str:
    values = ", ".join(f"'{event_type}'" for event_type in event_types)
    return f"event_type IN ({values})"


def upgrade() -> None:
    op.drop_constraint("worker_job_events_event_type_check", "worker_job_events", type_="check")
    op.create_check_constraint(
        "ck_worker_job_events_event_type",
        "worker_job_events",
        _check_expression(_EVENTS),
    )


def downgrade() -> None:
    op.drop_constraint("ck_worker_job_events_event_type", "worker_job_events", type_="check")
    op.create_check_constraint(
        "worker_job_events_event_type_check",
        "worker_job_events",
        _check_expression(_PREVIOUS_EVENTS),
    )
