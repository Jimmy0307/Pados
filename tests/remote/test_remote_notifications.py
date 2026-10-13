from __future__ import annotations

from typing import Any

from pados.remote.notifications import (
    PUSH_ALLOWED_KEYS,
    Notification,
    NotificationCenter,
    NotificationKind,
)


def test_push_carries_metadata_only() -> None:
    sent: list[dict[str, Any]] = []
    center = NotificationCenter(push=sent.append)
    center.emit(Notification(NotificationKind.APPROVAL_REQUIRED, "proj-secret-name", "run-123",
                             detail={"goal": "Refactor the payroll module"}))
    assert set(sent[0]) <= PUSH_ALLOWED_KEYS
    flat = repr(sent[0])
    for leaked in ("proj-secret-name", "run-123", "payroll"):
        assert leaked not in flat
    assert center.inbox[0]["detail"]["goal"] == "Refactor the payroll module"


def test_in_app_only_without_push_provider() -> None:
    center = NotificationCenter()
    center.emit(Notification(NotificationKind.JOB_FAILED, "p", "r"))
    assert len(center.inbox) == 1
