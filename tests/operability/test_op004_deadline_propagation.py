# SPDX-License-Identifier: AGPL-3.0-only
"""OP-004: a caller's parent deadline reaches registered command workers; it can only shorten their own timeout."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from pados.operability.resilience import Deadline, deadline_scope
from pados.worker import command as command_worker
from pados.worker.schemas import CommandInvocation


def _invocation(tmp_path: Path, timeout: int = 15) -> CommandInvocation:
    return CommandInvocation(command_id="python_version", working_directory=tmp_path, timeout_seconds=timeout)


class _Run:
    def __init__(self, *, expire: bool = False) -> None:
        self.timeouts: list[float] = []
        self.expire = expire

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.timeouts.append(kwargs["timeout"])
        if self.expire:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "Python 3\n", "")


def test_without_a_parent_deadline_the_command_keeps_its_own_timeout(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(command_worker.subprocess, "run", run)
    assert command_worker.run_registered_command(_invocation(tmp_path), project_root=tmp_path)["status"] == "SUCCEEDED"
    assert run.timeouts == [15]


def test_the_parent_budget_shortens_the_command_and_names_itself_when_it_fires(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(expire=True)
    monkeypatch.setattr(command_worker.subprocess, "run", run)
    with deadline_scope(Deadline.after(2.0)):
        result = command_worker.run_registered_command(_invocation(tmp_path), project_root=tmp_path)
    assert 0 < run.timeouts[0] <= 2.0
    assert (result["status"], result["error_code"]) == ("TIMED_OUT", "PARENT_DEADLINE_EXCEEDED")


def test_the_commands_own_timeout_firing_inside_a_larger_budget_stays_its_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run(expire=True)
    monkeypatch.setattr(command_worker.subprocess, "run", run)
    with deadline_scope(Deadline.after(300.0)):
        result = command_worker.run_registered_command(_invocation(tmp_path, timeout=15), project_root=tmp_path)
    assert run.timeouts == [15] and result["error_code"] == "TIMED_OUT"


def test_a_spent_budget_refuses_before_the_process_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _Run()
    monkeypatch.setattr(command_worker.subprocess, "run", run)
    with deadline_scope(Deadline(expires_at=0.0, clock=lambda: 1.0)):
        result = command_worker.run_registered_command(_invocation(tmp_path), project_root=tmp_path)
    assert run.timeouts == [] and result["error_code"] == "PARENT_DEADLINE_EXCEEDED"
