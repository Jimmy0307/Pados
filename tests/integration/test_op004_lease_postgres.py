# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the residual fixture is imported by name and requested as a parameter
"""OP-004: an owner's lease is renewed on each iteration, other owners are fenced, a lapse can be taken over, and
an owner-less (operator) step acts as the loop's starting owner: it renews that owner's lease, takes a lapsed one,
and is fenced like any other owner while someone else holds a live lease.

Uses the residual amendment database (PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL) and its deterministic router.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.loop import scope_authority
from pados.loop import service as loop_service
from tests.integration.test_residual_plan_amendment_postgres import (  # noqa: F401 - fixture used by name
    _install_deterministic_router,
    _seed_runtime,
    disposable_session,
)


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session) -> tuple[Session, str]:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    _install_deterministic_router(monkeypatch, ["COMPLETE"] * 6)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, _, _, _, loop_id, _ = _seed_runtime(disposable_session, workspace, child_capability="READ_REPOSITORY")
    return disposable_session, loop_id


def _iterate(session: Session, loop_id: str, tmp_path: Path, owner: str | None = None) -> dict[str, object]:
    return loop_service.run_one_iteration(session, loop_id=loop_id, settings=None,
                                          artifact_root=tmp_path / "artifacts", idempotency_key=f"op004-{uuid4()}",
                                          owner_instance=owner)


def _lease(session: Session, loop_id: str) -> dict[str, object]:
    return dict(session.execute(text(
        "SELECT owner_instance, lease_acquired_at, heartbeat_at, lease_expires_at, "
        "lease_expires_at - now() AS remaining FROM task_loop_leases WHERE loop_id = CAST(:l AS uuid)"),
        {"l": loop_id}).mappings().one())


def _iterations(session: Session, loop_id: str) -> int:
    return int(session.execute(text("SELECT count(*) FROM task_loop_iterations WHERE loop_id = CAST(:l AS uuid)"),
                               {"l": loop_id}).scalar_one())


def test_every_iteration_renews_the_owners_lease(seeded: tuple[Session, str], tmp_path: Path) -> None:
    session, loop_id = seeded
    session.execute(text("UPDATE task_loop_leases SET lease_expires_at = now() + interval '30 seconds', "
                         "heartbeat_at = now() - interval '1 hour' WHERE loop_id = CAST(:l AS uuid)"), {"l": loop_id})
    before = _lease(session, loop_id)
    _iterate(session, loop_id, tmp_path, owner=str(before["owner_instance"]))
    after = _lease(session, loop_id)
    assert after["owner_instance"] == before["owner_instance"]
    assert after["heartbeat_at"] > before["heartbeat_at"]
    assert after["remaining"].total_seconds() > 240  # renewed to five minutes


STARTER = "residual-integration"  # the owner _seed_runtime starts the loop as


def test_an_owner_less_step_is_fenced_while_another_owner_holds_a_live_lease(seeded: tuple[Session, str],
                                                                            tmp_path: Path) -> None:
    session, loop_id = seeded
    session.execute(text("UPDATE task_loop_leases SET owner_instance = 'replacement-worker', "
                         "lease_expires_at = now() + interval '30 seconds', heartbeat_at = now() - interval '1 hour' "
                         "WHERE loop_id = CAST(:l AS uuid)"), {"l": loop_id})
    before, iterations = _lease(session, loop_id), _iterations(session, loop_id)
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        _iterate(session, loop_id, tmp_path)
    after = _lease(session, loop_id)
    assert (after["owner_instance"], after["heartbeat_at"], after["lease_expires_at"]) == (
        "replacement-worker", before["heartbeat_at"], before["lease_expires_at"])  # never heartbeats for it
    assert _iterations(session, loop_id) == iterations  # refused before any step was selected or routed


def test_an_owner_less_step_renews_the_starting_owners_lease(seeded: tuple[Session, str], tmp_path: Path) -> None:
    session, loop_id = seeded
    session.execute(text("UPDATE task_loop_leases SET lease_expires_at = now() + interval '30 seconds', "
                         "heartbeat_at = now() - interval '1 hour' WHERE loop_id = CAST(:l AS uuid)"), {"l": loop_id})
    assert _lease(session, loop_id)["owner_instance"] == STARTER
    _iterate(session, loop_id, tmp_path)
    lease = _lease(session, loop_id)
    assert lease["owner_instance"] == STARTER and lease["remaining"].total_seconds() > 240


def test_an_owner_less_step_takes_a_lapsed_lease_as_the_starter_and_fences_the_old_holder(
        seeded: tuple[Session, str], tmp_path: Path) -> None:
    session, loop_id = seeded
    session.execute(text("UPDATE task_loop_leases SET owner_instance = 'crashed-owner', "
                         "lease_acquired_at = now() - interval '10 minutes', heartbeat_at = now() - interval '10 minutes', "
                         "lease_expires_at = now() - interval '1 second' WHERE loop_id = CAST(:l AS uuid)"), {"l": loop_id})
    resumed = _iterate(session, loop_id, tmp_path)
    lease = _lease(session, loop_id)
    # The crashed owner's claim is not revived: the operator now holds it as the starter, and the old holder is fenced.
    assert lease["owner_instance"] == STARTER and lease["remaining"].total_seconds() > 240
    assert resumed["status"] in {"RUNNING", "COMPLETED"}
    if resumed["status"] == "RUNNING":
        with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
            _iterate(session, loop_id, tmp_path, owner="crashed-owner")


def test_another_owner_is_refused_while_the_lease_is_live_and_nothing_runs(seeded: tuple[Session, str],
                                                                          tmp_path: Path) -> None:
    session, loop_id = seeded
    holder = _lease(session, loop_id)["owner_instance"]
    before = _iterations(session, loop_id)
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        _iterate(session, loop_id, tmp_path, owner="second-worker")
    assert _iterations(session, loop_id) == before  # refused before any step was selected or routed
    assert _lease(session, loop_id)["owner_instance"] == holder
    assert _iterate(session, loop_id, tmp_path, owner=str(holder))["status"] in {"RUNNING", "COMPLETED"}


def test_a_lapsed_lease_can_be_taken_over_and_the_loop_resumes(seeded: tuple[Session, str], tmp_path: Path) -> None:
    session, loop_id = seeded
    first = _iterate(session, loop_id, tmp_path)
    session.execute(text("UPDATE task_loop_leases SET lease_acquired_at = now() - interval '10 minutes', "
                         "heartbeat_at = now() - interval '10 minutes', lease_expires_at = now() - interval '1 second' "
                         "WHERE loop_id = CAST(:l AS uuid)"), {"l": loop_id})  # the old owner crashed
    resumed = _iterate(session, loop_id, tmp_path, owner="replacement-worker")
    lease = _lease(session, loop_id)
    assert lease["owner_instance"] == "replacement-worker"
    assert lease["remaining"].total_seconds() > 240
    assert first["status"] == "RUNNING" and resumed["status"] in {"RUNNING", "COMPLETED"}
    assert _iterations(session, loop_id) == 2


def test_start_records_no_owner_that_did_not_get_the_lease(seeded: tuple[Session, str]) -> None:
    session, loop_id = seeded
    session.execute(text("UPDATE task_loops SET status = 'READY' WHERE id = CAST(:l AS uuid)"), {"l": loop_id})
    session.execute(text("UPDATE task_loop_leases SET owner_instance = 'live-worker', "
                         "lease_expires_at = now() + interval '5 minutes' WHERE loop_id = CAST(:l AS uuid)"),
                    {"l": loop_id})
    started = int(session.execute(text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:l AS uuid) "
                                       "AND event_type = 'LOOP_STARTED'"), {"l": loop_id}).scalar_one())
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        loop_service.start_loop(session, loop_id, owner_instance="late-starter")
    after = int(session.execute(text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:l AS uuid) "
                                     "AND event_type = 'LOOP_STARTED'"), {"l": loop_id}).scalar_one())
    assert after == started  # owner-less steps can never come to act as "late-starter"
    assert _lease(session, loop_id)["owner_instance"] == "live-worker"


@pytest.mark.parametrize(("transition", "state"), [("pause_loop", "PAUSED"), ("cancel_loop", "CANCELLED")])
def test_a_step_on_a_stopped_loop_names_the_state(seeded: tuple[Session, str], tmp_path: Path, transition: str,
                                                   state: str) -> None:
    """Usability walkthrough: the refusal said only that the request conflicts with the current state."""
    session, loop_id = seeded
    getattr(loop_service, transition)(session, loop_id)
    with pytest.raises(ValueError, match=rf"^LOOP_NOT_RUNNING: the loop is {state}"):
        _iterate(session, loop_id, tmp_path)
