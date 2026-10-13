# SPDX-License-Identifier: AGPL-3.0-only
"""Process-wide connectivity runtime.

When ``background_probe.enabled`` is set, one ``ConnectivityMonitor`` driven by
one ``BackgroundProbe`` is shared by doctor, Mission Control, the remote
``/remote/v1/connectivity`` route and the provider gate, so they all read the
same operational projection. When it is disabled or the config is absent,
nothing is started and each caller keeps its existing behaviour. An invalid
config never starts probing (fail closed) and never prevents PADOS startup.

Every view carries its observation age, a ``stale`` flag and the probe thread
health, so a dead or stuck probe is never presented as current. The runtime
keeps ownership until its thread has actually stopped; a new runtime is refused
(CONNECTIVITY_RUNTIME_STILL_STOPPING) while an old probe thread is alive.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from pados.connectivity.background import BackgroundProbe, Waiter
from pados.connectivity.config import (
    CONNECTIVITY_CONFIG_INVALID,
    ConnectivityConfig,
    ProbeFactory,
    load_config,
)
from pados.connectivity.history import ConnectivityHistory
from pados.logging_config import get_logger
from pados.connectivity.maker import WanObservation
from pados.connectivity.state import Clock, ConnectivityMonitor, ConnectivityState, Link, freshness

CONNECTIVITY_RUNTIME_STILL_STOPPING = "CONNECTIVITY_RUNTIME_STILL_STOPPING"


class ConnectivityRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ConnectivityRuntime:
    config_path: str
    config: ConnectivityConfig
    monitor: ConnectivityMonitor
    probe: BackgroundProbe
    history: ConnectivityHistory | None
    stopping: bool = field(default=False)

    def view(self) -> dict[str, Any]:
        observation = self.monitor.observation()  # snapshot and age from one locked read
        return {**observation.snapshot, **freshness(observation.age_seconds, self.config.stale_after_seconds),
                "probe_mode": "BACKGROUND", "probe_thread": self.probe.health()}


_LOCK = threading.Lock()
_ACTIVE: ConnectivityRuntime | None = None


def build_runtime(config_path: str, *, probe_factory: ProbeFactory | None = None, clock: Clock | None = None,
                  waiter: Waiter | None = None) -> ConnectivityRuntime | None:
    """A runtime for an enabled background probe, else ``None`` (also for invalid config)."""
    if not config_path:
        return None
    config = load_config(config_path)
    if not isinstance(config, ConnectivityConfig) or not config.background_enabled or config.background is None:
        return None
    history = None
    if config.history_path:
        history = (ConnectivityHistory(config.history_path, clock=clock) if clock is not None
                   else ConnectivityHistory(config.history_path))
        try:
            history.prune()  # retention enforced at startup, not only on the next transition
        except OSError:
            pass
    monitor = config.monitor(probe_factory=probe_factory, clock=clock,
                             on_transition=history.record if history else None)
    probe = BackgroundProbe(monitor, config.background, waiter=waiter,
                            maintenance=(history.prune,) if history else ())
    return ConnectivityRuntime(config_path, config, monitor, probe, history)


def start_runtime(config_path: str, *, start_thread: bool = True, probe_factory: ProbeFactory | None = None,
                  clock: Clock | None = None, waiter: Waiter | None = None) -> ConnectivityRuntime | None:
    """Start (or return) the single process-wide runtime for ``config_path``.

    Raises ``ConnectivityRuntimeError(CONNECTIVITY_RUNTIME_STILL_STOPPING)`` while a
    previous runtime's probe thread is still alive.
    """
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE.stopping:
            if _ACTIVE.probe.running:
                raise ConnectivityRuntimeError(CONNECTIVITY_RUNTIME_STILL_STOPPING)
            _ACTIVE = None
        if _ACTIVE is not None:
            return _ACTIVE if _ACTIVE.config_path == config_path else None
        runtime = build_runtime(config_path, probe_factory=probe_factory, clock=clock, waiter=waiter)
        if runtime is None:
            return None
        if start_thread:
            runtime.probe.start()
        _ACTIVE = runtime
        return runtime


def active_runtime(config_path: str | None = None) -> ConnectivityRuntime | None:
    runtime = _ACTIVE
    if runtime is None or runtime.stopping or (config_path is not None and runtime.config_path != config_path):
        return None
    return runtime


def stop_runtime(runtime: ConnectivityRuntime | None = None, timeout: float | None = None) -> bool:
    """Stop the active runtime (only ``runtime`` when given); idempotent.

    Returns False when the probe thread did not stop within ``timeout`` (default:
    the probe's shutdown budget); the runtime then stays registered as stopping.
    """
    global _ACTIVE
    with _LOCK:
        current = _ACTIVE
        if current is None or (runtime is not None and runtime is not current):
            return True
        current.stopping = True
    stopped = current.probe.stop(timeout)
    with _LOCK:
        if stopped and _ACTIVE is current:
            _ACTIVE = None
    return stopped


def connectivity_lifespan(config_path: str) -> Callable[[Any], AbstractAsyncContextManager[None]]:
    """FastAPI lifespan: start the background probe at startup, stop it at shutdown."""

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        logger = get_logger("pados.connectivity")
        runtime = None
        if config_path:
            try:
                runtime = start_runtime(config_path)
            except ConnectivityRuntimeError as exc:  # PADOS starts; only the probe does not
                logger.warning("connectivity_probe_not_started", code=exc.code)
        try:
            yield
        finally:
            if runtime is not None and not await asyncio.to_thread(stop_runtime, runtime):
                logger.warning("connectivity_probe_stop_incomplete", code=CONNECTIVITY_RUNTIME_STILL_STOPPING)

    return lifespan


def connectivity_reader(config_path: str) -> Callable[[], dict[str, Any]]:
    """Projection reader for the remote route: the shared runtime when active, else the
    existing per-route monitor (observe on read). Invalid config reads as INVALID."""
    config = load_config(config_path)
    loaded = config if isinstance(config, ConnectivityConfig) else None
    fallback = loaded.monitor() if loaded is not None else None

    def read() -> dict[str, Any]:
        runtime = active_runtime(config_path)
        if runtime is not None:
            return runtime.view()
        if fallback is None or loaded is None:
            return {"status": CONNECTIVITY_CONFIG_INVALID, "home_base": "RUNNING", "semantic_authority": "NONE"}
        fallback.observe()
        observation = fallback.observation()
        return {**observation.snapshot, **freshness(observation.age_seconds, loaded.stale_after_seconds),
                "probe_mode": "ON_DEMAND", "probe_thread": None}

    return read


class ConfiguredWan:
    """WAN observations for the provider gate: the shared runtime monitor when active,
    else a monitor built from the operator config (refreshed on demand). Freshness is
    bounded by ``stale_after_seconds``; a config without ``wan_probe`` is
    configured-but-unobservable."""

    def __init__(self, config_path: str, config: ConnectivityConfig, *,
                 probe_factory: ProbeFactory | None = None, clock: Clock | None = None) -> None:
        self._config_path = config_path
        self._config = config
        self._probe_factory = probe_factory
        self._clock = clock
        self._fallback: ConnectivityMonitor | None = None
        self.max_age_seconds = float(config.stale_after_seconds)

    def _monitor(self) -> ConnectivityMonitor:
        runtime = active_runtime(self._config_path)
        if runtime is not None:
            return runtime.monitor
        if self._fallback is None:
            self._fallback = self._config.monitor(probe_factory=self._probe_factory, clock=self._clock)
        return self._fallback

    def wan_observation(self) -> WanObservation:
        observation = self._monitor().observation()  # state and age from the same snapshot
        if not observation.probed(Link.WAN):
            return WanObservation(observable=False, state=ConnectivityState.UNKNOWN, age_seconds=None)
        return WanObservation(observable=True, state=observation.state(Link.WAN),
                              age_seconds=observation.age_seconds)

    def refresh(self) -> None:
        self._monitor().observe()
