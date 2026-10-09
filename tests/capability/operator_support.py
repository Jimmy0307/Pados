"""Operator-side test seams for human-decision issuance (CR13b-01): the injected terminal session.

The OS account is NEVER injected: ``open_operator_issuer`` reads it from the OS. Tests bind approvers to the
account the test process really runs as (``current_os_account()``), or to a different one to show refusal.
Only the terminal (TTY check and the typed confirmation) is a seam.
"""
from __future__ import annotations

import dataclasses
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from pados.capability import operator_cli, operator_decisions

from pados.capability.operator_decisions import (
    OperatorAccount,
    OperatorBinding,
    OperatorDecisionIssuer,
    current_os_account,
    open_operator_issuer,
)
from pados.enterprise.secrets import SecretBoundary


class TypingHuman:
    """An interactive terminal with a human at it, who types the lines queued with ``will_type``."""

    def __init__(self, *, interactive: bool = True) -> None:
        self.is_interactive = interactive
        self.typed: deque[str] = deque()
        self.prompts: list[str] = []

    def interactive(self) -> bool:
        return self.is_interactive

    def read_confirmation(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.typed.popleft() if self.typed else ""

    def will_type(self, *lines: str) -> TypingHuman:
        self.typed.extend(lines)
        return self

    def confirms(self, task_id: str, plan_hash: str) -> TypingHuman:
        """The human reviewed this plan and types its first 12 hex characters and the task id."""
        return self.will_type(plan_hash[:12] + "\n", task_id + "\n")


def this_account() -> OperatorAccount:
    return current_os_account()


def other_account() -> OperatorAccount:
    """An account the test process does NOT run as (different uid on POSIX, different login elsewhere)."""
    mine = current_os_account()
    if mine.uid is not None:
        return dataclasses.replace(mine, uid=mine.uid + 1)
    return dataclasses.replace(mine, login=mine.login + "x")


def bindings(approver: str, key_ref: str, account: OperatorAccount | None = None) -> dict[str, OperatorBinding]:
    return {approver: OperatorBinding(key_ref=key_ref, account=account or this_account())}


@dataclasses.dataclass
class Approver:
    """An opened issuer plus the terminal it was opened from."""

    issuer: OperatorDecisionIssuer
    human: TypingHuman

    def approve(self, task_id: str, plan_hash: str, *, issued_at: datetime,
                lifetime: timedelta = timedelta(hours=2)) -> dict[str, str]:
        self.human.confirms(task_id, plan_hash)
        return self.issuer.approve(task_id=task_id, plan_hash=plan_hash, issued_at=issued_at, lifetime=lifetime)


@contextmanager
def at_terminal(human: Any) -> Iterator[Any]:
    """Seat ``human`` at the operator terminal: replaces the PRIVATE session seam for the duration (production code
    cannot pass a session; ``open_operator_issuer`` has no session parameter)."""
    previous = operator_decisions._session_factory
    operator_decisions._session_factory = lambda: human
    try:
        yield human
    finally:
        operator_decisions._session_factory = previous


def open_operator_issuer_at(human: Any, **kwargs: Any) -> OperatorDecisionIssuer:
    with at_terminal(human):
        return open_operator_issuer(**kwargs)


def run_cli_at(human: Any, argv: Sequence[str], **kwargs: Any) -> int:
    with at_terminal(human):
        return operator_cli.main(argv, **kwargs)


def open_approver(approver: str, key_ref: str, boundary: SecretBoundary, *,
                  environ: Mapping[str, str] | None = None, human: TypingHuman | None = None) -> Approver:
    terminal = human or TypingHuman()
    issuer = open_operator_issuer_at(terminal, approver=approver, operators=bindings(approver, key_ref),
                                     boundary=boundary, environ=environ or {})
    return Approver(issuer, terminal)
