# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json

from pados.loop.amendments import _task_envelope_from_body, normalize_p6_10_capability_outcome
from pados.work_protocol.models import CoverageStatus
from scripts.dogfood.local_dogfood_e2e import dogfood_plan_step, task_envelope_body


def test_dogfood_work_request_has_a_valid_hashed_task_envelope() -> None:
    body = task_envelope_body("11111111-1111-4111-8111-111111111111", "synthetic-1")

    decoded = json.loads(body)
    envelope = _task_envelope_from_body(body)
    plan_step = dogfood_plan_step("synthetic-1")

    assert envelope is not None
    assert decoded["task_envelope_hash"] == envelope.content_hash
    assert envelope.project_ref == "11111111-1111-4111-8111-111111111111"
    assert envelope.normalized_goals[0].required_capabilities == ("RUN_STATIC_ANALYSIS",)
    assert plan_step["product_goal_id"] == envelope.normalized_goals[0].goal_id
    assert plan_step["output_contract"] == {"pados_capability_outcome": "pados.capability-outcome.v1"}


def test_plain_python_version_output_does_not_satisfy_semantic_outcome_contract() -> None:
    outcome = normalize_p6_10_capability_outcome(
        {"pados_capability_outcome": "pados.capability-outcome.v1"},
        b"Python 3.12.0\r\n",
        execution_succeeded=True,
        execution_status="SUCCEEDED",
    )

    assert outcome is CoverageStatus.UNRESOLVED
