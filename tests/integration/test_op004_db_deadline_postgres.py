# SPDX-License-Identifier: AGPL-3.0-only
"""OP-004: a caller's parent deadline reaches PostgreSQL as a transaction-local statement_timeout.

Uses the residual amendment database (PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL). Runs only read statements.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from pados.db import engine as db_engine
from pados.operability.errors import WriteOutcome
from pados.operability.resilience import Deadline, DeadlineExceeded, deadline_scope


@pytest.fixture
def engine() -> Iterator[Engine]:
    url = os.environ.get("PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL to the disposable residual database")
    if make_url(url).host not in {"localhost", "127.0.0.1"}:
        pytest.fail("the deadline test requires a loopback database")
    built = create_engine(url, connect_args={"connect_timeout": 2})
    yield built
    built.dispose()


@pytest.fixture
def scoped(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(db_engine, "get_engine", lambda _settings=None: engine)
    monkeypatch.setattr(engine, "dispose", lambda *a, **k: None)  # the fixture owns the engine
    return db_engine.session_scope


def _timeout(session: Session) -> int:
    """The transaction's statement_timeout in milliseconds (0: none)."""
    return int(session.execute(text("SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'"))
               .scalar_one())


def test_without_a_deadline_no_statement_timeout_is_set(scoped: Any) -> None:
    with scoped() as session:
        assert _timeout(session) == 0


def test_the_remaining_budget_becomes_the_transactions_statement_timeout(scoped: Any) -> None:
    with deadline_scope(Deadline.after(30.0)), scoped() as session:
        assert 25_000 <= _timeout(session) <= 30_000
        session.commit()  # a new transaction in the same scope gets the budget again
        assert 0 < _timeout(session) <= 30_000


def test_a_statement_past_the_budget_is_cancelled_and_named(scoped: Any) -> None:
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded) as caught, deadline_scope(Deadline.after(0.5)), scoped() as session:
        session.execute(text("SELECT pg_sleep(5)"))
    assert caught.value.code == "DB_DEADLINE_EXCEEDED" and caught.value.write_occurred is WriteOutcome.NO
    assert time.monotonic() - started < 3  # cancelled by the server, not waited out


def test_a_spent_budget_refuses_the_transaction_before_its_first_statement(scoped: Any) -> None:
    with pytest.raises(DeadlineExceeded), deadline_scope(Deadline(expires_at=0.0, clock=lambda: 1.0)), \
            scoped() as session:
        session.execute(text("SELECT 1"))


def test_a_cancellation_after_an_earlier_commit_in_the_scope_reports_the_write(scoped: Any) -> None:
    with pytest.raises(DeadlineExceeded) as caught, deadline_scope(Deadline.after(0.8)), scoped() as session:
        session.execute(text("SELECT 1"))
        session.commit()  # durable: the scope has already written something
        session.execute(text("SELECT pg_sleep(5)"))
    assert caught.value.write_occurred is WriteOutcome.YES
