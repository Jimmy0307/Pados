# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D4 fixture helpers are imported by name
"""RC2 SEC-9: a local-model call never runs inside the routing transaction.

Operator surfaces (``loop_service.run_step``) commit the route first, run the model with no transaction or row lock
held, and resume the same iteration. During the call another connection can lock the request, loop and job rows
with NOWAIT; on the legacy in-transaction path it cannot, which is what makes this test meaningful.

Needs PADOS_RC2_D4_TEST_DATABASE_URL (a loopback ``pados_rc2_d4_*`` database at the operational head). Rows are
committed, so use a disposable database.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pados.loop import service as loop_service
from pados.worker import service as worker_service
from tests.integration.test_rc2_loop_model_handoff_postgres import _install_router, _seed_loop

SETTINGS = SimpleNamespace(local_inference_timeout_seconds=5)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("PADOS_RC2_D4_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set PADOS_RC2_D4_TEST_DATABASE_URL to a dedicated local disposable PostgreSQL database")
    parsed = make_url(database_url)
    if not str(parsed.database or "").startswith("pados_rc2_d4_") or parsed.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("SEC-9 integration tests require a loopback pados_rc2_d4_* database")
    built = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    yield built
    built.dispose()


def _scope(engine: Engine) -> Any:
    @contextlib.contextmanager
    def scope(_settings: object = None) -> Iterator[Session]:
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


class LockProbe:
    """A bound-provider stand-in that, while 'thinking', checks which rows another connection can lock."""

    def __init__(self, engine: Engine, rows: dict[str, str], *, during_call: Any = None) -> None:
        self.engine, self.rows, self.during_call = engine, rows, during_call
        self.calls = 0
        self.locked: list[str] = []
        self.last_evidence: dict[str, object] = {"profile_id": "fake-bound-profile", "binding_hash": "a" * 64}

    async def chat(self, prompt: str, *, response_schema: dict[str, object] | None = None) -> str:
        del prompt, response_schema
        self.calls += 1
        await asyncio.to_thread(self._probe)
        return "Bound summary."

    def _probe(self) -> None:
        for table, row_id in self.rows.items():
            try:
                with self.engine.begin() as connection:  # NOWAIT: fails at once if any transaction holds the row
                    connection.execute(text(f"SELECT 1 FROM {table} WHERE id = CAST(:id AS uuid) FOR UPDATE NOWAIT"),
                                       {"id": row_id})
            except OperationalError:
                self.locked.append(table)
        if self.during_call is not None:
            with self.engine.begin() as connection:
                self.during_call(connection)


def _seed(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Any]:
    _install_router(monkeypatch)
    scope = _scope(engine)
    with scope() as session:
        runtime = _seed_loop(session, tmp_path / "ws")
    first = loop_service.run_step(SETTINGS, loop_id=runtime["loop_id"], artifact_root=tmp_path / "artifacts",
                                  idempotency_key=f"sec9-1-{uuid4()}", session_factory=scope)
    assert first["decision"] == "ADVANCE_STEP"
    return runtime, scope


def _model_job(engine: Engine, request_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(connection.execute(text(
            "SELECT * FROM worker_jobs WHERE request_id = CAST(:r AS uuid) AND worker_type = 'OLLAMA'"),
            {"r": request_id}).mappings().one())


def test_operator_step_runs_the_model_with_no_lock_held(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    probe = LockProbe(engine, {"control_work_requests": runtime["request_id"], "task_loops": runtime["loop_id"]})
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    result = loop_service.run_step(SETTINGS, loop_id=runtime["loop_id"], artifact_root=tmp_path / "artifacts",
                                   idempotency_key=f"sec9-2-{uuid4()}", session_factory=scope)
    assert probe.calls == 1 and probe.locked == []
    assert result["job"]["status"] == "SUCCEEDED" and result["loop"]["status"] == "COMPLETED"
    job = _model_job(engine, runtime["request_id"])
    assert job["status"] == "SUCCEEDED" and job["validation_status"] == "PASSED"


def test_the_legacy_in_transaction_path_is_what_the_probe_detects(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    probe = LockProbe(engine, {"control_work_requests": runtime["request_id"], "task_loops": runtime["loop_id"]})
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    with scope() as session:
        loop_service.run_one_iteration(session, loop_id=runtime["loop_id"], settings=SETTINGS,
                                       artifact_root=tmp_path / "artifacts", idempotency_key=f"sec9-l-{uuid4()}")
    assert probe.calls == 1 and set(probe.locked) == {"control_work_requests", "task_loops"}


def test_a_job_that_expired_during_the_call_keeps_its_state(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)

    def expire(connection: Any) -> None:  # what expire_overdue_jobs does to an overdue job
        connection.execute(text("UPDATE worker_jobs SET status = 'TIMED_OUT', error_code = 'TIMED_OUT', "
                                "completed_at = now() WHERE request_id = CAST(:r AS uuid) AND worker_type = 'OLLAMA'"),
                           {"r": runtime["request_id"]})

    probe = LockProbe(engine, {}, during_call=expire)
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    loop_service.run_step(SETTINGS, loop_id=runtime["loop_id"], artifact_root=tmp_path / "artifacts",
                          idempotency_key=f"sec9-e-{uuid4()}", session_factory=scope)
    job = _model_job(engine, runtime["request_id"])
    assert job["status"] == "TIMED_OUT"
    with engine.connect() as connection:
        stdout_rows = connection.execute(text(
            "SELECT count(*) FROM worker_job_artifacts WHERE job_id = CAST(:j AS uuid) AND artifact_type = 'stdout'"),
            {"j": str(job["id"])}).scalar_one()
    assert int(stdout_rows) == 0  # the late result is not recorded


def test_an_interrupted_step_is_resumed_and_the_model_runs_exactly_once(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, scope = _seed(engine, tmp_path, monkeypatch)
    probe = LockProbe(engine, {})
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_a, **_k: probe)
    with scope() as session:  # routing committed, then the process stopped before the model ran
        loop_service.run_one_iteration(session, loop_id=runtime["loop_id"], settings=SETTINGS,
                                       artifact_root=tmp_path / "artifacts", idempotency_key=f"sec9-i-{uuid4()}",
                                       defer_model_execution=True)
    job = _model_job(engine, runtime["request_id"])
    assert job["status"] == "ROUTED" and probe.calls == 0
    result = loop_service.run_step(SETTINGS, loop_id=runtime["loop_id"], artifact_root=tmp_path / "artifacts",
                                   idempotency_key=f"sec9-r-{uuid4()}", session_factory=scope)
    assert result["loop"]["status"] == "COMPLETED" and probe.calls == 1
    again = worker_service.execute_routed_model_job(SETTINGS, str(job["id"]), session_factory=scope)
    assert again["status"] == "SUCCEEDED" and probe.calls == 1  # a repeated execution is a no-op
