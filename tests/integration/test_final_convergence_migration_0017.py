"""Migration 20261007_0017: previous head -> head with live data, constraints, replay protection.

Uses a dedicated disposable database named by PADOS_FC_MIGRATION_TEST_DATABASE (loopback only).
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from pados.config import get_settings, lane_settings

DATABASE = os.environ.get("PADOS_FC_MIGRATION_TEST_DATABASE", "")
pytestmark = pytest.mark.skipif(not DATABASE, reason="set PADOS_FC_MIGRATION_TEST_DATABASE")
ROOT = Path(__file__).resolve().parents[2]


def _alembic(*args: str) -> str:
    env = {**os.environ, "PADOS_DB_NAME": DATABASE}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.fixture
def engine() -> Iterator[Engine]:
    settings = get_settings()
    if settings.db_host not in {"127.0.0.1", "localhost"} or not DATABASE.startswith("pados_fc_migration"):
        pytest.fail("migration test requires a dedicated loopback pados_fc_migration_* database")
    engine = create_engine(lane_settings(settings, DATABASE).database_url)
    yield engine
    engine.dispose()


def _version(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return sorted(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())


def test_previous_head_upgrades_with_data_and_downgrade_is_reversible(engine: Engine) -> None:
    _alembic("upgrade", "20261006_0016")
    if _version(engine) != ["20261006_0016"]:
        _alembic("downgrade", "20261006_0016")  # dedicated disposable DB: start from the previous head
    assert _version(engine) == ["20261006_0016"]
    project_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects(id, name, root_path, classification) VALUES "
            "(CAST(:id AS uuid), :name, :root, 'internal')"),
            {"id": project_id, "name": f"migration-{project_id}", "root": f"/tmp/{project_id}"})
        connection.execute(text(
            "INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, idempotency_key) "
            "VALUES (CAST(:p AS uuid), :h, 'pre-existing', :k)"),
            {"p": project_id, "h": "a" * 64, "k": f"pre-{project_id}"})
    _alembic("upgrade", "20261007_0017")
    assert _version(engine) == ["20261007_0017"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM control_approvals WHERE project_id = CAST(:p AS uuid)"),
                                  {"p": project_id}).scalar_one() == 1
        tables = set(connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        assert {"remote_step_up_sessions", "approval_step_up_challenges", "result_acceptance_candidates",
                "result_acceptance_decisions"} <= tables
        indexes = set(connection.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")).scalars())
        assert {"ix_approval_step_up_credential", "ix_approval_step_up_actor_status",
                "ix_result_acceptance_candidates_task"} <= indexes
    _alembic("downgrade", "20261006_0016")
    assert _version(engine) == ["20261006_0016"]
    _alembic("upgrade", "20261007_0017")
    assert _version(engine) == ["20261007_0017"]


def test_schema_enforces_single_use_and_append_only_evidence(engine: Engine) -> None:
    _alembic("upgrade", "20261007_0017")
    session_id = "rs-" + uuid4().hex
    challenge = str(uuid4())
    proof = str(uuid4())
    base = {
        "challenge_id": challenge, "nonce": (uuid4().hex + uuid4().hex)[:43], "actor_id": "owner", "session_id": session_id,
        "approval_ref": "appr", "kind": "RESULT_CANDIDATE", "project_id": "p", "task_id": "t",
        "candidate_id": "candidate:x", "candidate_digest": "d" * 64, "action": "APPROVE",
        "classification": "INTERNAL", "expected_scope_version": 0, "target_run_id": "candidate:x",
        "binding_digest": "b" * 64,
    }
    insert = text(
        "INSERT INTO approval_step_up_challenges(challenge_id, nonce, actor_id, session_id, approval_ref, kind, "
        "project_id, task_id, candidate_id, candidate_digest, action, classification, expected_scope_version, "
        "target_run_id, binding_digest, protocol_version, schema_version, issued_at, expires_at, status) VALUES "
        "(CAST(:challenge_id AS uuid), :nonce, :actor_id, :session_id, :approval_ref, :kind, :project_id, :task_id, "
        ":candidate_id, :candidate_digest, :action, :classification, :expected_scope_version, :target_run_id, "
        ":binding_digest, '1.0', 'pados.approval-step-up.v1', now(), now() + interval '2 minutes', 'ISSUED')")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO remote_step_up_sessions VALUES (:s, :h, 'owner', now(), now() + interval '1 hour')"),
            {"s": session_id, "h": uuid4().hex + uuid4().hex})
        connection.execute(insert, base)
    with pytest.raises(IntegrityError), engine.begin() as connection:  # nonce is unique
        connection.execute(insert, {**base, "challenge_id": str(uuid4())})
    with pytest.raises(DBAPIError), engine.begin() as connection:  # ISSUED -> CONSUMED is not a transition
        connection.execute(text("UPDATE approval_step_up_challenges SET status = 'CONSUMED', "
                                "proof_consumed_at = now() WHERE challenge_id = CAST(:c AS uuid)"), {"c": challenge})
    verify = text(
        "UPDATE approval_step_up_challenges SET status='VERIFIED', proof_id=CAST(:p AS uuid), verified_at=now(), "
        "proof_expires_at=now() + interval '1 minute', presence_method='WEBAUTHN_USER_VERIFICATION', "
        "credential_id='cred', assertion_client_data=:e, assertion_authenticator_data=:e, assertion_signature=:e, "
        "assertion_digest=:d WHERE challenge_id = CAST(:c AS uuid)")
    with pytest.raises(IntegrityError), engine.begin() as connection:  # a proof needs its presence evidence
        connection.execute(verify, {"c": challenge, "p": proof, "e": None, "d": None})
    with pytest.raises(DBAPIError), engine.begin() as connection:  # binding columns are frozen
        connection.execute(text("UPDATE approval_step_up_challenges SET candidate_digest = :d "
                                "WHERE challenge_id = CAST(:c AS uuid)"), {"c": challenge, "d": "e" * 64})
    with engine.begin() as connection:
        connection.execute(verify, {"c": challenge, "p": proof, "e": "evidence", "d": "f" * 64})
    consume = text(
        "UPDATE approval_step_up_challenges SET status='CONSUMED', proof_consumed_at=now() "
        "WHERE proof_id = CAST(:p AS uuid) AND status='VERIFIED' AND proof_consumed_at IS NULL "
        "AND proof_expires_at > now()")
    with engine.begin() as connection:
        assert connection.execute(consume, {"p": proof}).rowcount == 1
    with engine.begin() as connection:
        assert connection.execute(consume, {"p": proof}).rowcount == 0  # replay consumes nothing
    with pytest.raises(DBAPIError), engine.begin() as connection:  # CONSUMED is terminal
        connection.execute(text("UPDATE approval_step_up_challenges SET status = 'VERIFIED', proof_consumed_at = NULL "
                                "WHERE challenge_id = CAST(:c AS uuid)"), {"c": challenge})
    for statement in ("DELETE FROM approval_step_up_challenges", "TRUNCATE approval_step_up_challenges CASCADE",
                      "TRUNCATE result_acceptance_decisions", "TRUNCATE result_acceptance_candidates CASCADE"):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(statement))


def test_offline_sql_generation_for_0017() -> None:
    sql = _alembic("upgrade", "20261006_0016:20261007_0017", "--sql")
    assert "CREATE TABLE approval_step_up_challenges" in sql
    assert "CREATE TABLE result_acceptance_decisions" in sql
    assert "trg_result_acceptance_candidates_append_only_update" in sql
