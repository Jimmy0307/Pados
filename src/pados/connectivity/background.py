# SPDX-License-Identifier: AGPL-3.0-only
"""Background connectivity probe: one daemon thread around an existing monitor.

This is NOT a scheduler. It schedules no work, cancels no job and mutates no
semantic state: each tick only calls ``ConnectivityMonitor.observe()``, which
updates the operational projection (CONNECTED / INTERMITTENT / OFFLINE /
RECOVERING) and the snapshot file. While a link is OFFLINE or INTERMITTENT the
next tick backs off exponentially, capped at ``max_backoff_seconds``. Probe
debugging is R0 ephemeral: a bounded in-memory deque holding link states only
(never targets), never persisted. ``maintenance`` callables (retention pruning of
the connectivity history store) run once per tick; they are operational store
maintenance, not work scheduling. ``health()`` exposes thread liveness and the
last successful observation so stale data can be labelled stale.

Shutdown is bounded: the inter-tick wait is ``Event.wait`` (interrupted by
``stop``) and an in-flight tick lasts at most the sum of the per-probe
timeouts, so ``stop()`` joins for ``shutdown_budget()`` seconds by default.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pados.connectivity.state import ConnectivityMonitor, ConnectivityState

INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 5.0, 3600.0, 60.0
TIMEOUT_MAX, TIMEOUT_DEFAULT = 10.0, 3.0
BACKOFF_DEFAULT = 600.0
DEBUG_EVENTS = 64
SHUTDOWN_MARGIN = 2.0
_DEGRADED = frozenset({ConnectivityState.OFFLINE, ConnectivityState.INTERMITTENT})

# ``wait(seconds) -> stop requested``; the default is ``threading.Event.wait``.
Waiter = Callable[[float], bool]


class BackgroundProbeConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _seconds(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackgroundProbeConfigError(f"BACKGROUND_PROBE_{key.upper()}_INVALID")
    return float(value)


@dataclass(frozen=True)
class BackgroundProbeSettings:
    enabled: bool = False
    interval_seconds: float = INTERVAL_DEFAULT
    timeout_seconds: float = TIMEOUT_DEFAULT
    max_backoff_seconds: float = BACKOFF_DEFAULT

    def __post_init__(self) -> None:
        if not INTERVAL_MIN <= self.interval_seconds <= INTERVAL_MAX:
            raise BackgroundProbeConfigError("BACKGROUND_PROBE_INTERVAL_SECONDS_OUT_OF_RANGE")
        if not 0 < self.timeout_seconds <= TIMEOUT_MAX:
            raise BackgroundProbeConfigError("BACKGROUND_PROBE_TIMEOUT_SECONDS_OUT_OF_RANGE")
        if not self.interval_seconds <= self.max_backoff_seconds <= INTERVAL_MAX:
            raise BackgroundProbeConfigError("BACKGROUND_PROBE_MAX_BACKOFF_SECONDS_OUT_OF_RANGE")

    @classmethod
    def from_mapping(cls, data: object) -> BackgroundProbeSettings:
        if not isinstance(data, dict) or set(data) - {"enabled", "interval_seconds", "timeout_seconds",
                                                     "max_backoff_seconds"}:
            raise BackgroundProbeConfigError("BACKGROUND_PROBE_SHAPE_INVALID")
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise BackgroundProbeConfigError("BACKGROUND_PROBE_ENABLED_INVALID")
        return cls(
            enabled=enabled,
            interval_seconds=_seconds(data, "interval_seconds", INTERVAL_DEFAULT),
            timeout_seconds=_seconds(data, "timeout_seconds", TIMEOUT_DEFAULT),
            max_backoff_seconds=_seconds(data, "max_backoff_seconds", BACKOFF_DEFAULT),
        )


class BackgroundProbe:
    def __init__(self, monitor: ConnectivityMonitor, settings: BackgroundProbeSettings, *,
                 waiter: Waiter | None = None, maintenance: Sequence[Callable[[], object]] = ()) -> None:
        self.monitor = monitor
        self.settings = settings
        self.maintenance = tuple(maintenance)
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._wait: Waiter = waiter or self._stop.wait
        self._thread: threading.Thread | None = None
        self._lifecycle = threading.Lock()
        self._degraded_ticks = 0
        self.debug_events: deque[dict[str, Any]] = deque(maxlen=DEBUG_EVENTS)

    def next_delay(self, degraded: bool) -> float:
        if not degraded:
            self._degraded_ticks = 0
            return self.settings.interval_seconds
        self._degraded_ticks = min(self._degraded_ticks + 1, 32)
        delay = self.settings.interval_seconds * 2 ** (self._degraded_ticks - 1)
        return float(min(delay, self.settings.max_backoff_seconds))

    def run_once(self) -> float:
        """One observation; returns the delay before the next one."""
        try:
            snapshot = self.monitor.observe()
            states = {name: str(row.get("state")) for name, row in snapshot["links"].items()}
            degraded = any(state in _DEGRADED for state in states.values())
            observed = snapshot.get("observed_at")
            self.last_success_at = datetime.fromisoformat(observed) if isinstance(observed, str) else None
            self.last_error = None
        except (OSError, ValueError):  # store/probe failure: keep probing, report degraded
            states, degraded = {"PROBE": "STORE_UNAVAILABLE"}, True
            self.last_error = "PROBE_STORE_UNAVAILABLE"
        for task in self.maintenance:
            try:
                task()
            except (OSError, ValueError):
                self.last_error = "PROBE_MAINTENANCE_FAILED"
        delay = self.next_delay(degraded)
        self.debug_events.append({"states": states, "next_delay_seconds": delay})
        return delay

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if self._wait(self.run_once()):
                break

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def shutdown_budget(self) -> float:
        return self.settings.timeout_seconds * max(1, self.monitor.probe_count) + SHUTDOWN_MARGIN

    def health(self) -> dict[str, Any]:
        return {"alive": self.running,
                "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
                "last_error": self.last_error}

    def start(self) -> bool:
        with self._lifecycle:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self.run_forever, name="pados-connectivity-probe",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float | None = None) -> bool:
        """Request shutdown and join; idempotent. Returns True only when no thread is left running."""
        with self._lifecycle:
            self._stop.set()
            thread = self._thread
            if thread is None or thread is threading.current_thread():
                return True
            thread.join(self.shutdown_budget() if timeout is None else timeout)
            if thread.is_alive():
                return False
            self._thread = None
            return True
