# SPDX-License-Identifier: AGPL-3.0-only
"""Shared work meter and optional wall-clock deadline for the algorithm pack (no threads, no timers).

``WorkMeter`` counts elementary steps deterministically and refuses BEFORE its limit would be passed
(``used <= limit`` always holds); each algorithm supplies its own closed codes. ``Deadline`` is an optional
guard set at the adapter/verifier boundary: the meter (and FCM, once per iteration) polls
``time.monotonic`` and the run ABORTS with ``ALGORITHM_DEADLINE_EXCEEDED``. A deadline never changes a
result - it can only stop a run - so a completed result is still a pure function of dataset and
parameters; WHETHER a run completes under a deadline depends on the host and is therefore recorded as
non-deterministic in the reproducibility record.
"""
from __future__ import annotations

import math
import time

from pados.packs.algorithms.contracts import MAX_DEADLINE_SECONDS, AlgorithmError

DEFAULT_DEADLINE_SECONDS = 300.0

monotonic = time.monotonic  # module attribute so tests can substitute a fake clock (no sleeps)


def valid_deadline_seconds(seconds: object) -> bool:
    return (isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds)
            and 0 < seconds <= MAX_DEADLINE_SECONDS)


class Deadline:
    """Abort-only wall-clock guard: ``check`` raises ``ALGORITHM_DEADLINE_EXCEEDED`` once it has passed."""

    def __init__(self, seconds: float) -> None:
        if not valid_deadline_seconds(seconds):
            raise AlgorithmError("ALGORITHM_DEADLINE_INVALID")
        self.seconds = float(seconds)
        self._expires = monotonic() + self.seconds

    def check(self) -> None:
        if monotonic() > self._expires:
            raise AlgorithmError("ALGORITHM_DEADLINE_EXCEEDED")


def optional_deadline(seconds: float | None) -> Deadline | None:
    return None if seconds is None else Deadline(seconds)


class WorkMeter:
    """Deterministic work meter: ``spend`` refuses BEFORE the limit would be exceeded (``used <= limit``)
    and, when a deadline is attached, polls it on every call."""

    def __init__(self, limit: int, *, maximum: int, invalid_code: str, exceeded_code: str,
                 deadline: Deadline | None = None) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise AlgorithmError(invalid_code)
        self.limit = limit
        self.used = 0
        self.deadline = deadline
        self._exceeded = exceeded_code

    def spend(self, steps: int = 1) -> None:
        if self.used + steps > self.limit:
            raise AlgorithmError(self._exceeded)
        if self.deadline is not None:
            self.deadline.check()
        self.used += steps
