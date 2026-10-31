"""RC2 SEC-3: pause, resume and cancel must never rewrite a loop that already finished.

Live dogfood showed it: a COMPLETED loop accepted ``/pause`` and ``/cancel`` (200) and read back CANCELLED after a
restart. The UPDATE now carries the status predicate itself (no read-then-write race), and a refused transition is
the closed ``LOOP_TRANSITION_REFUSED`` (HTTP 409 through the existing ``guarded`` mapping) with no event written.
"""
from __future__ import annotations

from typing import Any

import pytest

from pados.loop import service

TERMINAL = ("COMPLETED", "PARTIALLY_COMPLETED", "FAILED_TERMINAL", "CANCELLED", "TIMED_OUT")
LOOP_ID = "00000000-0000-0000-0000-000000000001"


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row

    def one(self) -> dict[str, Any]:
        if self._row is None:
            raise AssertionError("one() on an empty result: the refusal must be explicit")
        return self._row

    def all(self) -> list[dict[str, Any]]:
        return [] if self._row is None else [self._row]

    def scalar(self) -> object:
        return None


class _Session:
    """Records SQL; the first UPDATE on task_loops returns ``row`` (None = the predicate matched nothing)."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row, self.sql = row, []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        text = str(statement)
        self.sql.append(text)
        if "UPDATE task_loops" in text:
            return _Result(self.row)
        return _Result(None)


@pytest.mark.parametrize("action", [service.pause_loop, service.cancel_loop, service.resume_loop])
def test_a_refused_transition_is_closed_and_writes_no_event(action: Any) -> None:
    session = _Session(row=None)
    with pytest.raises(ValueError, match="LOOP_TRANSITION_REFUSED"):
        action(session, LOOP_ID)
    assert not any("task_loop_events" in sql for sql in session.sql)
    assert not any("UPDATE worker_jobs" in sql for sql in session.sql)


@pytest.mark.parametrize("action", [service.pause_loop, service.cancel_loop])
def test_pause_and_cancel_exclude_every_terminal_status_in_the_update_itself(action: Any) -> None:
    session = _Session(row=None)
    with pytest.raises(ValueError):
        action(session, LOOP_ID)
    update = next(sql for sql in session.sql if "UPDATE task_loops" in sql)
    assert all(status in update for status in TERMINAL)


def test_resume_only_continues_a_paused_loop() -> None:
    session = _Session(row=None)
    with pytest.raises(ValueError, match="LOOP_TRANSITION_REFUSED"):
        service.resume_loop(session, LOOP_ID)
    assert "status = 'PAUSED'" in next(sql for sql in session.sql if "UPDATE task_loops" in sql)
