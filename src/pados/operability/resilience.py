# SPDX-License-Identifier: AGPL-3.0-only
"""Parent deadlines and bounded retries for existing boundaries (OP-004).

``Deadline`` is a monotonic budget carried in a context variable. A child call takes ``min(its own timeout,
remaining budget)`` and is refused before it starts once the budget is spent. ``X-Request-Deadline-Ms`` on an API
request sets it (1 ms to 10 min); without it nothing changes.

``retry_call`` retries only failures that declare themselves retryable, bounded by an attempt count and by the
remaining budget, with exponential backoff and jitter. Authorization, validation, authority, not-found and conflict
failures are never retried. Every retry and every exhaustion is logged. Neither primitive schedules, authorizes or
owns work: the TaskLoop, worker jobs and their own deadlines stay authoritative.
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

from pados.logging_config import get_logger
from pados.operability.context import error_code_of
from pados.operability.errors import NEVER_RETRYABLE, ErrorCategory, PadosError, WriteOutcome
from pados.operability.metrics import METRICS

DEADLINE_HEADER = "X-Request-Deadline-Ms"
MAX_DEADLINE_MS = 600_000
_LOG = get_logger("pados.resilience")
_CURRENT: ContextVar[Deadline | None] = ContextVar("pados_deadline", default=None)
T = TypeVar("T")


class DeadlineExceeded(PadosError):
    def __init__(self, code: str = "DEADLINE_EXCEEDED") -> None:
        super().__init__(code, ErrorCategory.TIMEOUT, message="The operation's deadline had already passed.",
                         retryable=False, write_occurred=WriteOutcome.NO)


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, clock: Callable[[], float] = time.monotonic) -> Deadline:
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def cap(self, timeout: float) -> float:
        """The child timeout within this budget; refuses before the call when nothing is left."""
        remaining = self.remaining()
        if remaining <= 0.0:
            raise DeadlineExceeded()
        return min(timeout, remaining)


def current_deadline() -> Deadline | None:
    return _CURRENT.get()


@contextmanager
def deadline_scope(deadline: Deadline | None) -> Iterator[Deadline | None]:
    """Make ``deadline`` current; an inner budget never extends an outer one."""
    outer = _CURRENT.get()
    effective = deadline
    if outer is not None and (deadline is None or outer.expires_at < deadline.expires_at):
        effective = outer
    reset_handle = _CURRENT.set(effective)
    try:
        yield effective
    finally:
        _CURRENT.reset(reset_handle)


def capped_timeout(timeout: float) -> float:
    deadline = _CURRENT.get()
    return timeout if deadline is None else deadline.cap(timeout)


def deadline_from_header(value: str | None) -> Deadline | None:
    """A deadline from ``X-Request-Deadline-Ms`` when it is an integer in 1..600000; anything else is ignored."""
    if value is None or not value.strip().isdigit():
        return None
    milliseconds = int(value.strip())
    if not 1 <= milliseconds <= MAX_DEADLINE_MS:
        return None
    return Deadline.after(milliseconds / 1000)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0


def is_retryable(exc: BaseException) -> bool:
    """Only failures that declare themselves retryable, and never a refusal category."""
    category = getattr(exc, "category", None)
    if isinstance(category, ErrorCategory) and category in NEVER_RETRYABLE:
        return False
    if isinstance(exc, PermissionError | ValueError) and getattr(exc, "retryable", None) is not True:
        return False
    return getattr(exc, "retryable", False) is True


def retry_call(fn: Callable[[], T], *, operation: str, policy: RetryPolicy = RetryPolicy(),
               retryable: Callable[[BaseException], bool] = is_retryable,
               sleep: Callable[[float], None] = time.sleep, rand: Callable[[], float] = random.random) -> T:
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as exc:
            code = error_code_of(exc)
            if not retryable(exc):
                raise
            delay = min(policy.max_delay_s, policy.base_delay_s * 2 ** (attempt - 1)) * (0.5 + rand() / 2)
            deadline = _CURRENT.get()
            out_of_budget = deadline is not None and deadline.remaining() <= delay
            if attempt >= policy.max_attempts or out_of_budget:
                _LOG.warning("retry.exhausted", operation=operation, attempts=attempt, error_code=code,
                             reason="DEADLINE" if out_of_budget else "ATTEMPTS")
                METRICS.inc("pados_retry_attempts_total", {"operation": operation, "result": "exhausted"})
                raise
            _LOG.info("retry.attempt", operation=operation, attempt=attempt, error_code=code,
                      delay_ms=round(delay * 1000, 1))
            METRICS.inc("pados_retry_attempts_total", {"operation": operation, "result": "retried"})
            sleep(delay)
            attempt += 1
