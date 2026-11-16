# SPDX-License-Identifier: AGPL-3.0-only
"""Fault injection for the recoverable staged commit of ``POST /control/projects/{id}/documents``.

Each test breaks the write at one step (staged write, the audit commit, between commit and replace, between replace
and closing the intent, between staging and commit) and then "restarts" by running the server-start reconciliation.
It asserts the one correct final state: exactly one ``PROJECT_DOCUMENT_WRITTEN`` per completed write, no orphan
staged file after a rollback, a ``PROJECT_DOCUMENT_WRITE_FAILED`` with a closed code when roll-forward is impossible,
and idempotent retry by key.

Needs PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL (a disposable loopback ``*_test`` database at the operational
head). Unlike the D1 fixture, these tests COMMIT (the commit is what is being tested), so each uses its own project
in a fresh temporary workspace; rows are left in the disposable database.
"""
from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pados.control import product_routes
from pados.control import project_documents as documents
from pados.control import service as control_service
from pados.control.product_errors import ProductControlError
from pados.memory.service import bootstrap_project

_TEST_DATABASE_ENV = "PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL"
WRITTEN = product_routes.DOCUMENT_EVENT
FAILED = product_routes.DOCUMENT_FAILED_EVENT


class SimulatedCrash(BaseException):  # noqa: N818 - a process death, not an error any handler may catch
    """The process dies here: no ``except Exception`` handler runs, only ``finally`` blocks (as on a kill)."""


@dataclass
class World:
    engine: Engine
    settings: Any
    project_id: str
    folder: Path
    faults: dict[str, str]

    @contextlib.contextmanager
    def scope(self, _settings: Any = None) -> Iterator[Session]:
        """``pados.db.engine.session_scope`` semantics, with an injectable commit failure."""
        session = Session(self.engine, expire_on_commit=False)
        try:
            yield session
            fault = self.faults.pop("commit", "")
            if fault == "lost":  # the connection drops before COMMIT reaches the server
                session.rollback()
                raise OperationalError("COMMIT", {}, ConnectionError("connection lost"))
            session.commit()
            if fault == "ack_lost":  # COMMIT landed; its acknowledgement did not
                raise OperationalError("COMMIT", {}, ConnectionError("connection lost after commit"))
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def write(self, content: str = "one\n", key: str = "k-1", path: str = "guide.md", **extra: Any) -> dict[str, Any]:
        body = product_routes.DocumentWrite(path=path, content=content, idempotency_key=key, **extra)
        return product_routes.write_document_entry(self.scope, self.settings, self.project_id, body)

    def events(self, kind: str) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.execute(text("SELECT payload_json FROM control_sync_events WHERE project_id = "
                                        "CAST(:p AS uuid) AND event_type = :k ORDER BY sequence_number"),
                                   {"p": self.project_id, "k": kind})
            return [dict(row) for row in rows.scalars().all()]

    @property
    def journal(self) -> Path:
        return self.folder.joinpath(*documents.JOURNAL_PARTS)

    def intents(self) -> list[str]:
        return sorted(p.name for p in self.journal.glob("*.json")) if self.journal.is_dir() else []

    def staged(self) -> list[Path]:
        return sorted(self.folder.rglob(f"*{documents.STAGED_SUFFIX}"))

    def restart(self) -> dict[str, int]:
        """A fresh process: no in-process lock survives, then the server-start reconciliation runs."""
        product_routes._PROJECT_LOCKS.clear()
        return product_routes.reconcile_workspace_documents(self.settings, self.scope)

    def status(self) -> dict[str, Any]:
        """What ``GET /control/projects/{id}/documents`` reports (it reconciles first, as the route does)."""
        root, events, _recovery = product_routes.reconcile_project(self.scope, self.settings, self.project_id)
        return product_routes.reconciliation_status(root, events, documents.list_documents(root)["documents"])

    def unreconciled_status(self) -> dict[str, Any]:
        """The read-only status of the current disk and database, without reconciling first."""
        root = documents.resolve_project_root(str(self.folder), self.settings.product_workspace_root)
        with self.scope() as session:
            events = product_routes.document_events(session, self.project_id)
        return product_routes.reconciliation_status(root, events, documents.list_documents(root)["documents"])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    database_url = os.environ.get(_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {_TEST_DATABASE_ENV} to a dedicated local disposable test database")
    parsed = make_url(database_url)
    if not str(parsed.database or "").endswith("_test") or parsed.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("document journal integration tests require a disposable loopback *_test database")
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    workspace = tmp_path / "workspace"
    folder = workspace / f"journal-{uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    with Session(engine) as session, session.begin():
        project_id = bootstrap_project(session, name=f"doc-journal-{uuid4()}", root_path=folder,
                                       classification="internal")
    try:
        yield World(engine=engine, settings=SimpleNamespace(product_workspace_root=str(workspace)),
                    project_id=project_id, folder=folder.resolve(), faults={})
    finally:
        product_routes._PROJECT_LOCKS.clear()
        engine.dispose()


def _crash_once(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
    original = getattr(owner, name)
    state = {"armed": True}

    def crash(*args: Any, **kwargs: Any) -> Any:
        if state["armed"]:
            state["armed"] = False
            raise SimulatedCrash(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, crash)


def test_a_clean_write_commits_one_event_and_leaves_no_intent(world: World) -> None:
    receipt = world.write()
    assert receipt["replayed"] is False and (world.folder / "guide.md").read_bytes() == b"one\n"
    (event,) = world.events(WRITTEN)
    assert event["idempotency_key"] == "k-1" and len(event["intent_id"]) == 32
    assert world.intents() == [] and world.staged() == []
    assert world.status() == {"state": "CLEAN", "pending": 0, "failed": [], "unaudited": []}
    listed = documents.list_documents(documents.resolve_project_root(str(world.folder), str(world.folder.parent)))
    assert [d["path"] for d in listed["documents"]] == ["guide.md"]  # the journal is never listed
    if sys.platform == "win32":
        from pados.identity import fs_security

        assert fs_security.problem(world.journal) is None  # owner-only from creation


def test_a_failed_staged_write_rolls_back_with_a_closed_code(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_path: Path, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(documents, "_write_staged", broken)
    with pytest.raises(ProductControlError) as excinfo:
        world.write()
    assert (excinfo.value.code, excinfo.value.status) == ("DOCUMENT_STAGE_FAILED", 503)
    assert world.events(WRITTEN) == [] and world.intents() == [] and world.staged() == []
    assert not (world.folder / "guide.md").exists()
    monkeypatch.undo()
    assert world.write()["replayed"] is False  # the same key is free: nothing was audited
    assert len(world.events(WRITTEN)) == 1


def test_a_lost_commit_rolls_back_and_the_retry_writes_once(world: World) -> None:
    world.faults["commit"] = "lost"
    with pytest.raises(ProductControlError) as excinfo:
        world.write()
    assert (excinfo.value.code, excinfo.value.status) == ("DOCUMENT_AUDIT_FAILED", 503)
    assert world.events(WRITTEN) == [] and world.intents() == [] and world.staged() == []  # reconciled at once
    assert not (world.folder / "guide.md").exists()
    assert world.write()["replayed"] is False and world.write()["replayed"] is True
    assert len(world.events(WRITTEN)) == 1 and (world.folder / "guide.md").read_bytes() == b"one\n"


def test_a_commit_whose_acknowledgement_is_lost_rolls_forward(world: World) -> None:
    world.faults["commit"] = "ack_lost"
    with pytest.raises(ProductControlError) as excinfo:
        world.write()
    assert excinfo.value.code == "DOCUMENT_AUDIT_FAILED"  # the caller cannot know; the database decides
    assert len(world.events(WRITTEN)) == 1 and (world.folder / "guide.md").read_bytes() == b"one\n"
    assert world.intents() == [] and world.staged() == []
    assert world.write()["replayed"] is True and len(world.events(WRITTEN)) == 1


def test_a_crash_after_commit_before_replace_rolls_forward_on_restart(world: World,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    first = world.write()
    _crash_once(monkeypatch, documents, "_replace_final")
    replace = {"replace": True, "expected_sha256": first["sha256"]}
    with pytest.raises(SimulatedCrash):
        world.write("two\n", key="k-2", **replace)
    assert len(world.events(WRITTEN)) == 2 and (world.folder / "guide.md").read_bytes() == b"one\n"
    assert len(world.intents()) == 1 and len(world.staged()) == 1
    pending = world.unreconciled_status()
    assert pending["state"] == "PENDING" and pending["pending"] == 1 and pending["unaudited"] == []
    monkeypatch.undo()
    totals = world.restart()
    assert totals["rolled_forward"] == 1 and totals["projects"] == 1 and totals["unaudited"] == 0
    assert (world.folder / "guide.md").read_bytes() == b"two\n"
    assert world.intents() == [] and world.staged() == [] and len(world.events(WRITTEN)) == 2
    assert world.status()["state"] == "CLEAN"
    again = world.write("two\n", key="k-2", **replace)
    assert again["replayed"] is True and again["previous_sha256"] == first["sha256"]
    assert len(world.events(WRITTEN)) == 2
    assert world.restart().get("rolled_forward", 0) == 0  # nothing left to do: reconciliation is idempotent


@pytest.mark.parametrize(("damage", "code"), [
    ("delete_staged", "DOCUMENT_STAGED_MISSING"),
    ("corrupt_staged", "DOCUMENT_STAGED_CORRUPT"),
    ("edit_target", "DOCUMENT_TARGET_CHANGED"),
])
def test_an_impossible_roll_forward_records_one_closed_failure(world: World, monkeypatch: pytest.MonkeyPatch,
                                                               damage: str, code: str) -> None:
    first = world.write()
    _crash_once(monkeypatch, documents, "_replace_final")
    with pytest.raises(SimulatedCrash):
        world.write("two\n", key="k-2", replace=True, expected_sha256=first["sha256"])
    monkeypatch.undo()
    (staged,) = world.staged()
    if damage == "delete_staged":
        staged.unlink()
    elif damage == "corrupt_staged":
        staged.write_bytes(b"tw0\n")
    else:
        (world.folder / "guide.md").write_bytes(b"edited outside PADOS\n")
    totals = world.restart()
    assert totals["failed"] == 1 and totals.get("rolled_forward", 0) == 0
    (failure,) = world.events(FAILED)
    assert failure["code"] == code and failure["idempotency_key"] == "k-2" and failure["path"] == "guide.md"
    assert world.intents() == [] and world.staged() == []
    expected_disk = b"edited outside PADOS\n" if damage == "edit_target" else b"one\n"
    assert (world.folder / "guide.md").read_bytes() == expected_disk  # never overwritten, never re-audited
    status = world.status()
    assert status["state"] == "ATTENTION" and status["failed"] == [{"path": "guide.md", "code": code}]
    unaudited = [{"path": "guide.md", "code": "DOCUMENT_UNAUDITED_CHANGE"}] if damage == "edit_target" else []
    assert status["unaudited"] == unaudited
    with pytest.raises(ProductControlError) as retry:
        world.write("two\n", key="k-2", replace=True, expected_sha256=first["sha256"])
    assert (retry.value.code, retry.value.status) == (code, 409)
    world.restart()
    assert len(world.events(FAILED)) == 1 and len(world.events(WRITTEN)) == 2  # recorded exactly once


def test_a_crash_after_replace_before_closing_the_intent_is_closed(world: World,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_once(monkeypatch, documents, "_close_entry")
    with pytest.raises(SimulatedCrash):
        world.write()
    assert (world.folder / "guide.md").read_bytes() == b"one\n" and len(world.intents()) == 1
    monkeypatch.undo()
    totals = world.restart()
    assert totals["closed"] == 1 and world.intents() == [] and world.staged() == []
    assert len(world.events(WRITTEN)) == 1 and world.status()["state"] == "CLEAN"
    assert world.write()["replayed"] is True


@pytest.mark.parametrize("step", ["_write_staged", "append_sync_event"])
def test_a_crash_before_the_commit_rolls_back_on_restart(world: World, monkeypatch: pytest.MonkeyPatch,
                                                         step: str) -> None:
    _crash_once(monkeypatch, documents if step == "_write_staged" else control_service, step)
    with pytest.raises(SimulatedCrash):
        world.write()
    assert len(world.intents()) == 1 and world.events(WRITTEN) == []  # the intent was durable first
    assert len(world.staged()) == (1 if step == "append_sync_event" else 0)
    monkeypatch.undo()
    assert world.restart()["rolled_back"] == 1
    assert world.intents() == [] and world.staged() == [] and not (world.folder / "guide.md").exists()
    assert world.write()["replayed"] is False and len(world.events(WRITTEN)) == 1


def test_a_crash_after_the_failure_commit_closes_without_a_second_failure(world: World,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_once(monkeypatch, documents, "_replace_final")
    with pytest.raises(SimulatedCrash):
        world.write()
    world.staged()[0].unlink()
    _crash_once(monkeypatch, documents, "discard_entry")  # dies after the FAILED event committed
    with pytest.raises(SimulatedCrash):
        world.restart()
    assert len(world.events(FAILED)) == 1 and len(world.intents()) == 1
    monkeypatch.undo()
    assert world.restart()["closed"] == 1
    assert len(world.events(FAILED)) == 1 and world.intents() == []


def test_the_next_route_call_reconciles_before_it_acts(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _crash_once(monkeypatch, documents, "_replace_final")
    with pytest.raises(SimulatedCrash):
        world.write()
    monkeypatch.undo()
    world.write("# other\n", key="k-other", path="other.md")  # a different document of the same project
    assert (world.folder / "guide.md").read_bytes() == b"one\n" and world.intents() == []
    assert [e["path"] for e in world.events(WRITTEN)] == ["guide.md", "other.md"]


def test_an_external_edit_is_reported_and_never_re_audited(world: World) -> None:
    world.write()
    (world.folder / "guide.md").write_bytes(b"changed in an editor\n")
    status = world.status()
    assert status == {"state": "ATTENTION", "pending": 0, "failed": [],
                      "unaudited": [{"path": "guide.md", "code": "DOCUMENT_UNAUDITED_CHANGE"}]}
    (world.folder / "guide.md").unlink()
    assert world.status()["unaudited"] == [{"path": "guide.md", "code": "DOCUMENT_UNAUDITED_CHANGE"}]
    assert world.restart()["unaudited"] == 1
    assert len(world.events(WRITTEN)) == 1 and world.events(FAILED) == []


def test_a_target_held_open_stays_pending_until_a_retry_can_finish_it(world: World,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    original = documents._replace_final
    attempts = {"count": 0}

    def held(staged: Path, target: Path) -> None:
        attempts["count"] += 1
        if attempts["count"] <= 3:  # an editor holds the file without delete sharing (Windows)
            raise PermissionError("in use")
        original(staged, target)

    monkeypatch.setattr(documents, "_replace_final", held)
    with pytest.raises(ProductControlError) as first:
        world.write()
    assert (first.value.code, first.value.status) == ("DOCUMENT_FINALIZE_PENDING", 503)
    assert world.status() == {"state": "PENDING", "pending": 1, "failed": [], "unaudited": []}
    with pytest.raises(ProductControlError) as still:
        world.write()
    assert (still.value.code, still.value.status) == ("DOCUMENT_FINALIZE_PENDING", 503)
    assert world.write()["replayed"] is True  # the fourth attempt replaces; the retry replays the committed receipt
    assert (world.folder / "guide.md").read_bytes() == b"one\n" and world.intents() == [] and world.staged() == []
    assert len(world.events(WRITTEN)) == 1 and world.events(FAILED) == []
