"""Shared RC2 D1 test support: WORK_ROUTE approval subjects for integration fixtures.

A control approval routes work only when it names exactly what it authorizes, so
fixtures that route or launch P6.10 loops create bound approvals with this helper.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# One deterministic P6.10 step: READ_REPOSITORY via the registered python_version command.
VERSION_STEP: dict[str, Any] = {
    "step_sequence": 1,
    "capability_id": "READ_REPOSITORY",
    "command_id": "python_version",
    "max_risk_level": "LOW",
}


def work_route_subject(
    project_id: str,
    request_id: str,
    operations: Iterable[dict[str, Any]] = (VERSION_STEP,),
    *,
    surfaces: tuple[str, ...] = ("TASK_LOOP",),
    worker_types: tuple[str, ...] = ("DETERMINISTIC_TOOL",),
) -> dict[str, Any]:
    return {
        "kind": "WORK_ROUTE",
        "schema_version": "pados.approval-subject.v1",
        "project_id": project_id,
        "request_id": request_id,
        "actor": {"surfaces": list(surfaces), "worker_types": list(worker_types)},
        "operations": [dict(operation) for operation in operations],
    }
