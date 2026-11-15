# SPDX-License-Identifier: AGPL-3.0-only
"""An in-memory circuit breaker per provider (CLOSED -> OPEN -> HALF_OPEN -> CLOSED).

CLOSED: calls pass; ``failure_threshold`` consecutive dependency failures open the circuit.
OPEN: calls are refused before any network traffic until ``open_seconds`` have passed.
HALF_OPEN: exactly one trial call passes; success closes the circuit (for example after a VPN reconnect), failure
opens it again for another ``open_seconds``.

State is process memory only (it is observation, never authority) and visible through ``snapshot``. Only dependency
failures count: an authorization refusal, an unknown model or a rate limit says the gateway is up and does not open
the circuit. Nothing here retries or schedules.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    open_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _state: CircuitState = CircuitState.CLOSED
    _failures: int = 0
    _opened_at: float | None = None
    _trial_in_flight: bool = False
    _last_failure: str | None = None
    _last_failure_at: str | None = None
    _last_success_at: str | None = None
    _transitions: list[dict[str, str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _move(self, state: CircuitState) -> None:
        if state is not self._state:
            self._transitions.append({"from": self._state.value, "to": state.value, "at": _iso()})
            del self._transitions[:-20]
            self._state = state

    def _current(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._opened_at is not None \
                and self.clock() - self._opened_at >= self.open_seconds:
            self._move(CircuitState.HALF_OPEN)
            self._trial_in_flight = False
        return self._state

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._current()

    def acquire(self, *, probe: bool = False) -> bool:
        """Whether a call may go out now. ``probe`` (a read-only health check an operator asked for) may act as the
        half-open trial even while the circuit is OPEN, so a reconnect is noticed without waiting."""
        with self._lock:
            state = self._current()
            if state is CircuitState.CLOSED:
                return True
            if state is CircuitState.OPEN and not probe:
                return False
            if self._trial_in_flight:
                return False
            if state is CircuitState.OPEN:
                self._move(CircuitState.HALF_OPEN)
            self._trial_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trial_in_flight = False
            self._opened_at = None
            self._last_success_at = _iso()
            self._move(CircuitState.CLOSED)

    def record_failure(self, code: str) -> None:
        with self._lock:
            state = self._current()
            self._last_failure, self._last_failure_at = code, _iso()
            self._trial_in_flight = False
            self._failures += 1
            if state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._opened_at = self.clock()
                self._move(CircuitState.OPEN)

    def record_neutral(self, code: str) -> None:
        """A refusal that proves the gateway answered (auth, unknown model, rate limit): ends a trial, keeps counts."""
        with self._lock:
            self._last_failure, self._last_failure_at = code, _iso()
            if self._current() is CircuitState.HALF_OPEN:
                self._trial_in_flight = False
                self._failures = 0
                self._opened_at = None
                self._move(CircuitState.CLOSED)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._current()
            remaining = None
            if state is CircuitState.OPEN and self._opened_at is not None:
                remaining = round(max(0.0, self.open_seconds - (self.clock() - self._opened_at)), 3)
            return {"state": state.value, "consecutive_failures": self._failures,
                    "failure_threshold": self.failure_threshold, "open_seconds": self.open_seconds,
                    "retry_in_seconds": remaining, "last_failure_code": self._last_failure,
                    "last_failure_at": self._last_failure_at, "last_success_at": self._last_success_at,
                    "transitions": list(self._transitions)}


def _iso() -> str:
    return datetime.now(UTC).isoformat()


_CIRCUITS: dict[str, CircuitBreaker] = {}
_CIRCUITS_LOCK = threading.Lock()


def circuit_for(provider_id: str, *, failure_threshold: int = 3, open_seconds: float = 30.0) -> CircuitBreaker:
    """The process-wide breaker of ``provider_id`` (created on first use; its settings follow the latest contract)."""
    with _CIRCUITS_LOCK:
        breaker = _CIRCUITS.get(provider_id)
        if breaker is None:
            breaker = CircuitBreaker(failure_threshold=failure_threshold, open_seconds=open_seconds)
            _CIRCUITS[provider_id] = breaker
        else:
            breaker.failure_threshold, breaker.open_seconds = failure_threshold, open_seconds
        return breaker


def circuit_snapshot(provider_id: str) -> dict[str, Any] | None:
    with _CIRCUITS_LOCK:
        breaker = _CIRCUITS.get(provider_id)
    return breaker.snapshot() if breaker is not None else None


def reset_circuits() -> None:
    """Tests only: forget every breaker."""
    with _CIRCUITS_LOCK:
        _CIRCUITS.clear()
