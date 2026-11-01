# SPDX-License-Identifier: AGPL-3.0-only
"""OP-004: a routing key reused for a different approval, capability or command is refused by name, before any write."""
from __future__ import annotations

from typing import Any

import pytest

from pados.worker import service as worker_service
from pados.worker.schemas import CapabilityId, RouteRequest

APPROVAL = "11111111-1111-1111-1111-111111111111"


class _Rows:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _Rows:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _Session:
    """Answers only the one lookup ``_idempotent_job`` makes; any write would fail loudly."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.statements: list[str] = []

    def execute(self, statement: Any, _params: Any = None) -> _Rows:
        self.statements.append(str(statement))
        return _Rows(self._row)


def _route(**change: Any) -> RouteRequest:
    base: dict[str, Any] = {"approval_id": APPROVAL, "capability_id": CapabilityId.READ_REPOSITORY,
                            "context_query": "q", "command_id": "python_version", "idempotency_key": "route-1"}
    return RouteRequest(**{**base, **change})


def _existing() -> dict[str, Any]:
    return {"id": "job-1", "approval_id": APPROVAL, "capability_id": CapabilityId.READ_REPOSITORY.value,
            "route_payload": {"command_id": "python_version"}}


def test_an_exact_replay_returns_the_existing_job() -> None:
    assert worker_service._idempotent_job(_Session(_existing()), "req", _route())["id"] == "job-1"


def test_an_unused_key_returns_nothing() -> None:
    assert worker_service._idempotent_job(_Session(None), "req", _route()) is None


@pytest.mark.parametrize("change", [
    {"command_id": "other_command"},
    {"approval_id": "22222222-2222-2222-2222-222222222222"},
    {"capability_id": CapabilityId.WRITE_REPOSITORY} if hasattr(CapabilityId, "WRITE_REPOSITORY") else
    {"command_id": None},
])
def test_a_key_reused_for_a_different_route_is_a_named_conflict(change: dict[str, Any]) -> None:
    session = _Session(_existing())
    with pytest.raises(ValueError, match="ROUTE_IDEMPOTENCY_CONTENT_CONFLICT"):
        worker_service._idempotent_job(session, "req", _route(**change))
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in session.statements)
