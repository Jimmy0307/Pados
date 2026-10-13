# SPDX-License-Identifier: AGPL-3.0-only
"""Persistence helpers for opaque product-goal to P6.10 runtime identities."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def insert_goal_runtime_binding(
    session: Session,
    *,
    task_id: str,
    goal_id: str,
    plan_id: str,
    step_id: str,
    provenance_hash: str,
    residual_id: str | None = None,
    loop_id: str | None = None,
    amendment_id: str | None = None,
) -> dict[str, Any]:
    identity_hash = _stable_hash(
        {
            "task_id": task_id,
            "goal_id": goal_id,
            "residual_id": residual_id,
            "plan_id": plan_id,
            "step_id": step_id,
        }
    )
    binding_identity = f"goal-runtime-binding:{identity_hash}"
    execution_identity = f"p6.10-execution:{_stable_hash({'plan_id': plan_id, 'step_id': step_id})}"
    row = session.execute(
        text(
            """
            INSERT INTO goal_runtime_bindings(
              task_id, goal_id, residual_id, plan_id, step_id, loop_id, amendment_id,
              binding_identity, execution_identity, provenance_hash
            )
            VALUES (
              :task_id, :goal_id, :residual_id, CAST(:plan_id AS uuid), CAST(:step_id AS uuid),
              CAST(:loop_id AS uuid), CAST(:amendment_id AS uuid), :binding_identity,
              :execution_identity, :provenance_hash
            )
            ON CONFLICT (binding_identity) DO NOTHING
            RETURNING *
            """
        ),
        {
            "task_id": task_id,
            "goal_id": goal_id,
            "residual_id": residual_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "loop_id": loop_id,
            "amendment_id": amendment_id,
            "binding_identity": binding_identity,
            "execution_identity": execution_identity,
            "provenance_hash": provenance_hash,
        },
    ).mappings().first()
    if row is None:
        row = session.execute(
            text("SELECT * FROM goal_runtime_bindings WHERE binding_identity = :identity FOR UPDATE"),
            {"identity": binding_identity},
        ).mappings().one()
        if (
            str(row["task_id"]) != task_id
            or str(row["goal_id"]) != goal_id
            or row["residual_id"] != residual_id
            or str(row["plan_id"]) != plan_id
            or str(row["step_id"]) != step_id
            or (None if row["loop_id"] is None else str(row["loop_id"])) != loop_id
            or (None if row["amendment_id"] is None else str(row["amendment_id"])) != amendment_id
            or str(row["provenance_hash"]) != provenance_hash
        ):
            raise ValueError("GOAL_RUNTIME_BINDING_IDENTITY_CONFLICT")
    return dict(row)
