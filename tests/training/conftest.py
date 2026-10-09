"""Hermetic decision environment: tests never depend on ambient CI markers, the real OS login or a real
terminal (explicit private seams of ``pados.training.decisions`` and ``pados.training.operator``)."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from pados.training import decisions, operator

from . import support

REAL_CURRENT_SESSION = operator._current_session  # captured before any test replaces the seam


@pytest.fixture(autouse=True)
def hermetic_decision_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    environment: dict[str, str] = {}
    monkeypatch.setattr(decisions, "_environment", lambda: environment)
    yield environment


@pytest.fixture(autouse=True)
def operator_terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[support.Terminal]:
    """The approver's own interactive login; replies to the confirmation prompt like an attentive human."""
    terminal = support.Terminal(support.session_for(support.APPROVER))
    monkeypatch.setattr(operator, "_current_session", terminal.current_session)
    monkeypatch.setattr(operator, "_read_confirmation", terminal.read)
    support.TERMINAL.append(terminal)
    yield terminal
    support.TERMINAL.remove(terminal)
