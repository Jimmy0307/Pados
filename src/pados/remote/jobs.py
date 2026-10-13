# SPDX-License-Identifier: AGPL-3.0-only
"""Remote job control.

Remote PAUSE / RESUME / CANCEL are requests to the execution backend; they do
not change semantic state. A backend that cannot pause answers
``PAUSE_UNSUPPORTED`` rather than pretending. Cancellation is tracked as
requested -> acknowledged -> completed (or unsupported). Jobs are owned by
PADOS, not by a remote connection: a client disconnect never cancels a job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ControlAction(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


class ControlState(StrEnum):
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    PAUSE_UNSUPPORTED = "PAUSE_UNSUPPORTED"
    RESUMED = "RESUMED"
    RESUME_UNSUPPORTED = "RESUME_UNSUPPORTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_ACKNOWLEDGED = "CANCEL_ACKNOWLEDGED"
    CANCEL_COMPLETED = "CANCEL_COMPLETED"
    CANCEL_UNSUPPORTED = "CANCEL_UNSUPPORTED"
    JOB_ALREADY_TERMINAL = "JOB_ALREADY_TERMINAL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"


class ExecutionBackend(Protocol):
    supports_pause: bool
    supports_cancel: bool

    def exists(self, run_id: str) -> bool: ...
    def is_terminal(self, run_id: str) -> bool: ...
    def pause(self, run_id: str) -> bool: ...
    def resume(self, run_id: str) -> bool: ...
    def request_cancel(self, run_id: str) -> bool: ...


@dataclass
class ControlRecord:
    run_id: str
    action: ControlAction
    actor_id: str
    history: list[tuple[str, str]] = field(default_factory=list)

    def advance(self, state: ControlState) -> None:
        self.history.append((state.value, datetime.now(UTC).isoformat()))

    @property
    def state(self) -> str:
        return self.history[-1][0] if self.history else ""

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "action": self.action.value,
            "actor_id": self.actor_id,
            "state": self.state,
            "history": [list(item) for item in self.history],
            "changes_semantic_state": False,
        }


class RemoteJobController:
    def __init__(self, backend: ExecutionBackend) -> None:
        self._backend = backend
        self._records: dict[str, ControlRecord] = {}

    def _preflight(self, record: ControlRecord) -> bool:
        if not self._backend.exists(record.run_id):
            record.advance(ControlState.JOB_NOT_FOUND)
            return False
        if self._backend.is_terminal(record.run_id):
            record.advance(ControlState.JOB_ALREADY_TERMINAL)
            return False
        return True

    def control(self, *, run_id: str, action: ControlAction, actor_id: str) -> ControlRecord:
        record = ControlRecord(run_id=run_id, action=action, actor_id=actor_id)
        self._records[f"{run_id}:{action.value}"] = record
        if not self._preflight(record):
            return record
        if action is ControlAction.PAUSE:
            if not self._backend.supports_pause:
                record.advance(ControlState.PAUSE_UNSUPPORTED)
            else:
                record.advance(ControlState.PAUSE_REQUESTED)
                record.advance(
                    ControlState.PAUSED if self._backend.pause(run_id) else ControlState.PAUSE_UNSUPPORTED
                )
        elif action is ControlAction.RESUME:
            ok = self._backend.supports_pause and self._backend.resume(run_id)
            record.advance(ControlState.RESUMED if ok else ControlState.RESUME_UNSUPPORTED)
        else:
            record.advance(ControlState.CANCEL_REQUESTED)
            if not self._backend.supports_cancel:
                record.advance(ControlState.CANCEL_UNSUPPORTED)
            elif self._backend.request_cancel(run_id):
                record.advance(ControlState.CANCEL_ACKNOWLEDGED)
                if self._backend.is_terminal(run_id):
                    record.advance(ControlState.CANCEL_COMPLETED)
        return record

    def refresh(self, run_id: str) -> ControlRecord | None:
        """Promote an acknowledged cancel to completed once the job is terminal."""
        record = self._records.get(f"{run_id}:{ControlAction.CANCEL.value}")
        if record and record.state == ControlState.CANCEL_ACKNOWLEDGED and self._backend.is_terminal(run_id):
            record.advance(ControlState.CANCEL_COMPLETED)
        return record

    def on_client_disconnect(self, session_id: str) -> None:
        """Deliberately a no-op: jobs are PADOS-owned, not connection-owned."""
        del session_id
