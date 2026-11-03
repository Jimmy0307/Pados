# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D4 fixture helpers are imported by name
"""OP-004: an owner whose TaskLoop lease is taken over while its local-model call runs publishes nothing.

``loop_service.run_step`` commits routing, calls the model with no transaction open (RC2 SEC-9) and resumes the
iteration in a new transaction. A lease can lapse and be taken over by another worker in that window. Then:

* the model result is not recorded for the old owner: the job closes FAILED with LOOP_LEASE_LOST, and its stdout is
  neither recorded nor left on disk;
* the old owner's resumed iteration is refused (LOOP_LEASE_HELD), so it decides and publishes nothing;
* a takeover after the result was recorded (while the owner still held the lease) still stops the old owner's
  resumed iteration, and the new holder completes it through the normal validation path;
* an owner-less step never calls the model while another owner holds a live lease.

Needs PADOS_RC2_D4_TEST_DATABASE_URL (a loopback ``pados_rc2_d4_*`` database at the operational head).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from pados.loop import service as loop_service
from pados.worker import service as worker_service
from tests.integration.test_sec9_model_call_outside_transaction_postgres import (  # noqa: F401 - fixture by name
    SETTINGS,
    LockProbe,
    _model_job,
    _seed,
    engine,
)


def _take_over(loop_id: str, owner: str = "worker-b") -> Callable[[Any], None]:
    def take(connection: Any) -> None:  # the old lease lapsed and another worker took it (what _hold_lease does)
        connection.execute(text("UPDATE task_loop_leases SET owner_instance = :o, lease_acquired_at = now(), "
                                "heartbeat_at = now(), lease_expires_at = now() + interval '5 minutes' "
                                "WHERE loop_id = CAST(:l AS uuid)"), {"o": owner, "l": loop_id})
    return take


def _lapse(engine: Engine, loop_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(text("UPDATE task_loop_leases SET lease_acquired_at = now() - interval '10 minutes', "
                                "heartbeat_at = now() - interval '10 minutes', "
                                "lease_expires_at = now() - interval '1 second' WHERE loop_id = CAST(:l AS uuid)"),
                           {"l": loop_id})


def _decided(engine: Engine, loop_id: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(
            "SELECT count(*) FROM task_loop_iterations WHERE loop_id = CAST(:l AS uuid) AND status = 'DECIDED'"),
            {"l": loop_id}).scalar_one())


def _step(runtime: dict[str, str], scope: Any, tmp_path: Path, owner: str | None) -> dict[str, Any]:
    return loop_service.run_step(SETTINGS, loop_id=runtime["loop_id"], artifact_root=tmp_path / "artifacts",
                                 idempotency_key=f"op004-m-{uuid4()}", session_factory=scope, owner_instance=owner)


@pytest.mark.parametrize("owner", [None, "worker-a"])
def test_a_takeover_during_the_model_call_discards_the_result_and_fences_the_old_owner(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str | None
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    if owner is not None:
        _lapse(engine, runtime["loop_id"])  # so the named worker can take the lease for its step
    probe = LockProbe(engine, {}, during_call=_take_over(runtime["loop_id"]))
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    decided = _decided(engine, runtime["loop_id"])
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        _step(runtime, scope, tmp_path, owner)
    assert probe.calls == 1
    job = _model_job(engine, runtime["request_id"])
    assert (job["status"], job["error_code"], job["validation_status"]) == ("FAILED", "LOOP_LEASE_LOST", "FAILED")
    with engine.connect() as connection:
        stdout_rows = connection.execute(text(
            "SELECT count(*) FROM worker_job_artifacts WHERE job_id = CAST(:j AS uuid) AND artifact_type = 'stdout'"),
            {"j": str(job["id"])}).scalar_one()
        handoff = connection.execute(text(
            "SELECT path FROM worker_job_artifacts WHERE job_id = CAST(:j AS uuid) AND artifact_type = 'handoff_json'"),
            {"j": str(job["id"])}).scalar_one()
    assert int(stdout_rows) == 0 and not (Path(str(handoff)).parent / "stdout.txt").exists()
    assert _decided(engine, runtime["loop_id"]) == decided  # the old owner decided nothing
    _step(runtime, scope, tmp_path, "worker-b")  # the new holder carries on through the normal path
    with engine.connect() as connection:
        lost = connection.execute(text("SELECT status, error_code FROM worker_jobs WHERE id = CAST(:j AS uuid)"),
                                  {"j": str(job["id"])}).one()
    assert tuple(lost) == ("FAILED", "LOOP_LEASE_LOST")  # the discarded result never comes back as a success


def test_a_takeover_after_the_result_was_recorded_still_stops_the_old_owners_completion(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    probe = LockProbe(engine, {})
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    original = loop_service.execute_routed_model_job

    def execute_then_lose_the_lease(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        with engine.begin() as connection:
            _take_over(runtime["loop_id"])(connection)
        return result

    monkeypatch.setattr(loop_service, "execute_routed_model_job", execute_then_lose_the_lease)
    decided = _decided(engine, runtime["loop_id"])
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        _step(runtime, scope, tmp_path, None)
    assert _model_job(engine, runtime["request_id"])["status"] == "SUCCEEDED"  # recorded while the lease was held
    assert _decided(engine, runtime["loop_id"]) == decided  # but the old owner published no decision
    monkeypatch.setattr(loop_service, "execute_routed_model_job", original)
    finished = _step(runtime, scope, tmp_path, "worker-b")
    assert finished["loop"]["status"] == "COMPLETED" and probe.calls == 1  # validated and decided by the holder


def test_an_owner_less_step_never_calls_the_model_under_another_owners_live_lease(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    with engine.begin() as connection:
        _take_over(runtime["loop_id"])(connection)
    probe = LockProbe(engine, {})
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    with pytest.raises(PermissionError, match="LOOP_LEASE_HELD"):
        _step(runtime, scope, tmp_path, None)
    assert probe.calls == 0
    with engine.connect() as connection:
        jobs = connection.execute(text("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:r AS uuid) "
                                       "AND worker_type = 'OLLAMA'"), {"r": runtime["request_id"]}).scalar_one()
    assert int(jobs) == 0  # nothing was routed either
