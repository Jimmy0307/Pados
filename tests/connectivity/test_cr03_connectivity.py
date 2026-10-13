"""Regression tests for Codex review CR-03 (connectivity): CC-01, CC-02, CC-06, CC-07, CC-08, CC-10."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from pados.connectivity import runtime as runtime_module
from pados.connectivity.background import BackgroundProbe, BackgroundProbeSettings
from pados.connectivity.config import ConnectivityConfig, connectivity_status, connectivity_visibility
from pados.connectivity.history import ConnectivityHistory
from pados.connectivity.maker import RequirementGatedMaker, WanObservation
from pados.connectivity.requirements import NetworkRequirement
from pados.connectivity.runtime import (
    CONNECTIVITY_RUNTIME_STILL_STOPPING,
    ConfiguredWan,
    ConnectivityRuntimeError,
    active_runtime,
    build_runtime,
    connectivity_lifespan,
    connectivity_reader,
    start_runtime,
    stop_runtime,
)
from pados.connectivity.state import ConnectivityMonitor, ConnectivityState, Link, write_snapshot
from pados.product_loop.schemas import RuntimeResult, RuntimeStatus
from tests.connectivity.connectivity_support import WAN_HOST, FakeProbes, write_config

BG = {"enabled": True, "interval_seconds": 10, "timeout_seconds": 2, "max_backoff_seconds": 80}


class Clock:
    def __init__(self, start: datetime | None = None, step: float = 0.0) -> None:
        self.now = start or datetime.now(UTC)
        self.step = step

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=self.step)
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class SpyMaker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.approved_root = Path("/approved")

    async def make(self, contract: Any, *, run_id: str, attempt: int) -> RuntimeResult:
        self.calls.append(run_id)
        return RuntimeResult(run_id=run_id, attempt=attempt, runtime_id="spy", status=RuntimeStatus.COMPLETED,
                             started_at="t", completed_at="t", summary="spy")


@pytest.fixture(autouse=True)
def _no_leaked_runtime() -> Iterator[None]:
    yield
    stop_runtime()
    runtime_module._ACTIVE = None


def _make(maker: Any) -> RuntimeResult:
    result: RuntimeResult = asyncio.run(maker.make(None, run_id="run-1", attempt=1))
    return result


def _wan_gate(wan: Any) -> tuple[RequirementGatedMaker, SpyMaker]:
    inner = SpyMaker()
    return RequirementGatedMaker(inner, requirement=lambda: NetworkRequirement.WAN_REQUIRED, wan=wan), inner  # type: ignore[arg-type]


# --- CC-01: WAN_REQUIRED needs a FRESH observation ------------------------------------------------

def test_cr03_cc01_connected_observation_expires_and_offline_is_detected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    probes, clock = FakeProbes(), Clock()
    wan = ConfiguredWan(path, ConnectivityConfig.load(path), probe_factory=probes.factory, clock=clock)
    maker, inner = _wan_gate(wan)
    assert _make(maker).summary == "spy"  # UNKNOWN -> one refresh -> CONNECTED, fresh
    calls = len(probes.calls)
    clock.tick(60)
    assert _make(maker).summary == "spy" and len(probes.calls) == calls  # still fresh: no refresh
    probes.wan = False
    clock.tick(301)  # past stale_after_seconds (300): the old CONNECTED is no longer trusted
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and len(probes.calls) > calls
    assert maker.diagnostics[-1]["reason"] == "WAN_UNAVAILABLE" and inner.calls == ["run-1", "run-1"]


def test_cr03_cc01_offline_observation_expires_and_recovery_is_detected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    probes, clock = FakeProbes(wan=False), Clock()
    wan = ConfiguredWan(path, ConnectivityConfig.load(path), probe_factory=probes.factory, clock=clock)
    maker, inner = _wan_gate(wan)
    assert _make(maker).summary == "PROVIDER_UNREACHABLE"
    probes.wan = True
    clock.tick(301)
    assert _make(maker).summary == "spy" and inner.calls == ["run-1"]  # RECOVERING is usable


def test_cr03_cc01_shared_runtime_stale_observation_is_refreshed_once(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    runtime.probe.run_once()
    probes.wan = False  # WAN drops; the dead/slow background thread has not noticed
    clock.tick(400)
    maker, inner = _wan_gate(ConfiguredWan(path, runtime.config))
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and inner.calls == []
    assert runtime.monitor.state(Link.WAN) is ConnectivityState.OFFLINE  # the shared monitor was refreshed


class _StuckWan:
    max_age_seconds = 300.0

    def __init__(self, *, fail: bool) -> None:
        self.fail, self.refreshes = fail, 0

    def wan_observation(self) -> WanObservation:
        return WanObservation(observable=True, state=ConnectivityState.CONNECTED, age_seconds=10_000.0)

    def refresh(self) -> None:
        self.refreshes += 1
        if self.fail:
            raise OSError("probe store unavailable")


@pytest.mark.parametrize("fail", [True, False])
def test_cr03_cc01_unrefreshable_stale_observation_fails_closed(fail: bool) -> None:
    wan = _StuckWan(fail=fail)
    maker, inner = _wan_gate(wan)
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and inner.calls == []
    assert wan.refreshes == 1 and maker.diagnostics[-1]["reason"] == "WAN_OBSERVATION_STALE"


# --- CC-06: coherent projections, ordered persistence --------------------------------------------

def test_cr03_cc06_projection_is_coherent_and_not_blocked_by_slow_probe() -> None:
    entered, release = threading.Event(), threading.Event()
    state = {"slow": False}

    def wan() -> bool:
        if state["slow"]:
            entered.set()
            release.wait(5)
        return True

    monitor = ConnectivityMonitor({Link.WAN: wan, Link.REMOTE_TRANSPORT: lambda: False})
    first = monitor.observe()
    state["slow"] = True
    worker = threading.Thread(target=monitor.observe)
    worker.start()
    assert entered.wait(5)
    started = time.monotonic()
    during = monitor.projection()  # does not wait for the in-flight probe
    assert time.monotonic() - started < 1.0
    assert during == first  # the previous complete observation, never a half-applied one
    release.set()
    worker.join(5)
    after = monitor.projection()
    assert after["sequence"] == first["sequence"] + 1


def test_cr03_cc06_snapshot_never_overwritten_by_older_observation(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    newer = {"links": {}, "observed_at": "2026-10-02T10:00:00+00:00", "sequence": 9}
    older = {"links": {}, "observed_at": "2026-10-02T09:00:00+00:00", "sequence": 3}
    assert write_snapshot(path, newer) is True
    assert write_snapshot(path, older) is False
    assert json.loads(path.read_text())["observed_at"] == newer["observed_at"]
    assert "sequence" not in json.loads(path.read_text())  # persisted schema unchanged


def test_cr03_cc06_concurrent_observations_persist_in_order(tmp_path: Path) -> None:
    flips = iter([True, False] * 50)
    lock = threading.Lock()

    def wan() -> bool:
        with lock:
            return next(flips)

    history = ConnectivityHistory(tmp_path / "history.json")
    monitor = ConnectivityMonitor({Link.WAN: wan}, snapshot_path=tmp_path / "snapshot.json",
                                  on_transition=history.record)
    threads = [threading.Thread(target=lambda: [monitor.observe() for _ in range(10)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    persisted = json.loads((tmp_path / "snapshot.json").read_text())
    final = monitor.projection()
    assert final["sequence"] == 40 and persisted["observed_at"] == final["observed_at"]
    assert persisted["links"] == final["links"]  # the last observation won, never an older one
    stamps = [row["at"] for row in history.entries()]
    assert stamps == sorted(stamps) and len(stamps) >= 2
    for previous, current in zip(history.entries(), history.entries()[1:], strict=False):
        assert previous["to"] == current["from"]  # transitions chain in observation order


# --- CC-07: runtime ownership until the thread has stopped ---------------------------------------

def test_cr03_cc07_runtime_kept_until_thread_stops_and_restart_refused(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def factory(host: str, port: int, timeout: float) -> Callable[[], bool]:
        def probe() -> bool:
            if host == WAN_HOST:
                entered.set()
                release.wait(10)
            return True

        return probe

    path = write_config(tmp_path, background=BG)
    runtime = start_runtime(path, probe_factory=factory)
    assert runtime is not None and entered.wait(5)
    assert runtime.probe.shutdown_budget() == 2 * 2 + 2.0  # per-probe timeout x probes + margin
    assert stop_runtime(runtime, timeout=0.1) is False  # thread still inside a probe
    assert active_runtime() is None and runtime_module._ACTIVE is runtime and runtime.stopping
    with pytest.raises(ConnectivityRuntimeError) as info:
        start_runtime(path, probe_factory=factory)
    assert info.value.code == CONNECTIVITY_RUNTIME_STILL_STOPPING
    release.set()
    assert stop_runtime(runtime) is True and runtime_module._ACTIVE is None
    assert not any(t.name == "pados-connectivity-probe" and t.is_alive() for t in threading.enumerate())
    replacement = start_runtime(path, start_thread=False, probe_factory=factory)
    assert replacement is not None and replacement is not runtime


def test_cr03_cc07_wait_is_interruptible(tmp_path: Path) -> None:
    probes = FakeProbes()
    path = write_config(tmp_path, background={**BG, "interval_seconds": 3600, "max_backoff_seconds": 3600})
    runtime = start_runtime(path, probe_factory=probes.factory)
    assert runtime is not None
    deadline = time.monotonic() + 5
    while not probes.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    assert stop_runtime(runtime) is True
    assert time.monotonic() - started < 2.0  # not the 3600 s interval


def test_cr03_cc07_lifespan_handles_still_stopping_and_incomplete_stop(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.connectivity import config as connectivity_config

    probes = FakeProbes()
    monkeypatch.setattr(connectivity_config, "tcp_probe", probes.factory)
    path = write_config(tmp_path, background=BG)
    monkeypatch.setattr(runtime_module, "stop_runtime", lambda *_a, **_k: False)

    async def cycle() -> None:
        async with connectivity_lifespan(path)(FastAPI()):
            assert active_runtime(path) is not None

    asyncio.run(cycle())  # an incomplete stop is reported, not raised
    monkeypatch.undo()
    stuck = runtime_module._ACTIVE
    assert stuck is not None
    stuck.stopping = True
    monkeypatch.setattr(stuck.probe, "_thread", threading.Thread(target=lambda: None))
    monkeypatch.setattr(type(stuck.probe), "running", property(lambda _self: True))

    async def refused() -> None:
        async with connectivity_lifespan(path)(FastAPI()):  # PADOS still starts
            assert active_runtime(path) is None

    asyncio.run(refused())
    monkeypatch.undo()
    stuck.probe.stop()


# --- CC-08: age, health and stale marking --------------------------------------------------------

def test_cr03_cc08_views_expose_age_health_and_stale(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    runtime.probe.run_once()
    fresh = connectivity_reader(path)()
    assert fresh["stale"] is False and fresh["observation_age_seconds"] == 0.0
    assert fresh["probe_thread"]["alive"] is False and fresh["probe_thread"]["last_success_at"]
    clock.tick(301)
    for view in (connectivity_reader(path)(), connectivity_visibility(path)):
        assert view["stale"] is True and view["observation_age_seconds"] >= 301
        assert view["probe_thread"]["alive"] is False
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["probe_source"] == "BACKGROUND_PROBE_REFRESHED" and doctor["stale"] is False
    assert doctor["remote_status"] == "REMOTE_REACHABLE"


def test_cr03_cc08_reachability_never_derived_from_stale_data(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    probes = FakeProbes()
    clock = Clock(step=400.0)  # every reading is already older than stale_after_seconds
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    runtime.probe.run_once()
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["stale"] is True and doctor["remote_status"] == "REMOTE_STATUS_STALE"
    assert doctor["remote_transport"] == "CONNECTED"  # reported, but not turned into REACHABLE


def test_cr03_cc08_stale_persisted_snapshot_is_not_trusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.connectivity import config as connectivity_config

    path = write_config(tmp_path, background=BG)
    config = ConnectivityConfig.load(path)
    assert config.snapshot_path is not None
    old = datetime.now(UTC) - timedelta(hours=1)
    ConnectivityMonitor({Link.WAN: lambda: True, Link.REMOTE_TRANSPORT: lambda: True}, clock=lambda: old,
                        snapshot_path=config.snapshot_path).observe()
    probes = FakeProbes(overlay=False)
    monkeypatch.setattr(connectivity_config, "tcp_probe", probes.factory)
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["probe_source"] == "DIRECT_PROBE" and doctor["remote_status"] == "REMOTE_UNREACHABLE"
    mission = connectivity_visibility(path)
    assert mission["status"] == "CONNECTIVITY_OBSERVED" and mission["stale"] is False  # direct probe rewrote it


def test_cr03_cc08_dead_thread_is_reported(tmp_path: Path) -> None:
    probes = FakeProbes()
    path = write_config(tmp_path, background=BG)
    runtime = start_runtime(path, probe_factory=probes.factory)
    assert runtime is not None
    deadline = time.monotonic() + 5
    while not probes.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.view()["probe_thread"]["alive"] is True
    assert runtime.probe.stop() is True
    assert runtime.view()["probe_thread"]["alive"] is False


# --- CC-10: history retention on startup, read and tick ------------------------------------------

def _seed_history(path: Path, ages_days: list[float]) -> None:
    now = datetime.now(UTC)
    rows = [{"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": (now - timedelta(days=d)).isoformat()}
            for d in ages_days]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kind": "CONNECTIVITY_HISTORY", "retention_class": "R1_OPERATIONAL",
                                "entries": rows}), encoding="utf-8")


def test_cr03_cc10_history_read_filters_expired_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    _seed_history(path, [30, 20, 1])
    data = json.loads(path.read_text())
    data["entries"].append({"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": "x"})
    data["entries"].append({"link": "WAN", "from": "BOGUS", "to": "OFFLINE", "at": datetime.now(UTC).isoformat()})
    path.write_text(json.dumps(data), encoding="utf-8")
    history = ConnectivityHistory(path)
    assert len(history.entries()) == 1  # quiet system: expired rows are never returned
    # CR-03b: the read itself removed them from disk; nothing is left for prune()
    assert len(json.loads(path.read_text())["entries"]) == 1 and history.prune() == 0


def test_cr03_cc10_history_pruned_at_startup_and_on_each_tick(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    config = ConnectivityConfig.load(path)
    assert config.history_path is not None
    _seed_history(config.history_path, [30, 1])
    runtime = build_runtime(path, probe_factory=FakeProbes().factory)
    assert runtime is not None
    assert len(json.loads(config.history_path.read_text())["entries"]) == 1  # startup prune
    _seed_history(config.history_path, [40, 2])
    runtime.probe.run_once()  # maintenance runs on every tick, independent of transitions
    cutoff = datetime.now(UTC) - timedelta(days=14)
    rows = json.loads(config.history_path.read_text())["entries"]
    assert rows and all(datetime.fromisoformat(row["at"]) >= cutoff for row in rows)
    assert len([row for row in rows if row["from"] == "CONNECTED"]) == 1  # the 2-day row survives


def test_cr03_cc10_maintenance_failure_does_not_stop_the_probe() -> None:
    def broken() -> None:
        raise OSError("disk full")

    monitor = ConnectivityMonitor({Link.WAN: lambda: True})
    probe = BackgroundProbe(monitor, BackgroundProbeSettings(**BG), maintenance=(broken,))
    assert probe.run_once() == 10 and probe.health()["last_error"] == "PROBE_MAINTENANCE_FAILED"


# --- F-22: future-dated / naive / unparseable observation timestamps are stale -------------------

def _persist_raw(path: str, observed_at: object) -> None:
    config = ConnectivityConfig.load(path)
    assert config.snapshot_path is not None
    config.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    config.snapshot_path.write_text(json.dumps({
        "links": {"WAN": {"link": "WAN", "state": "CONNECTED"},
                  "REMOTE_TRANSPORT": {"link": "REMOTE_TRANSPORT", "state": "CONNECTED"}},
        "observed_at": observed_at, "home_base": "RUNNING", "semantic_authority": "NONE"}), encoding="utf-8")


@pytest.mark.parametrize("observed_at", [
    (datetime.now(UTC) + timedelta(hours=1)).isoformat(),  # future-dated beyond skew
    (datetime.now(UTC) + timedelta(days=365)).isoformat(),
    datetime.now(UTC).replace(tzinfo=None).isoformat(),  # timezone-naive
    "not-a-timestamp", None, 12345, "",
])
def test_f22_mission_control_untrusted_timestamp_is_stale_and_never_crashes(tmp_path: Path,
                                                                           observed_at: object) -> None:
    path = write_config(tmp_path)
    _persist_raw(path, observed_at)
    view = connectivity_visibility(path)
    assert view["status"] == "CONNECTIVITY_OBSERVED"
    assert view["stale"] is True and view["observation_age_seconds"] is None


def test_f22_small_skew_is_tolerated(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    _persist_raw(path, (datetime.now(UTC) + timedelta(seconds=20)).isoformat())
    view = connectivity_visibility(path)
    assert view["stale"] is False and view["observation_age_seconds"] == 0.0


@pytest.mark.parametrize("observed_at", [(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                                         datetime.now(UTC).replace(tzinfo=None).isoformat(), "garbage"])
def test_f22_doctor_never_trusts_untrusted_background_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              observed_at: str) -> None:
    from pados.connectivity import config as connectivity_config

    path = write_config(tmp_path, background=BG)
    _persist_raw(path, observed_at)
    probes = FakeProbes(overlay=False)
    monkeypatch.setattr(connectivity_config, "tcp_probe", probes.factory)
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["probe_source"] == "DIRECT_PROBE" and doctor["remote_status"] == "REMOTE_UNREACHABLE"
    assert probes.calls  # observed for real instead of trusting the forged snapshot


def test_f22_future_dated_snapshot_does_not_block_newer_writes(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    _persist_raw(path, (datetime.now(UTC) + timedelta(days=30)).isoformat())
    config = ConnectivityConfig.load(path)
    assert config.snapshot_path is not None
    snapshot = ConnectivityMonitor({Link.WAN: lambda: False}, snapshot_path=config.snapshot_path).observe()
    assert json.loads(config.snapshot_path.read_text())["observed_at"] == snapshot["observed_at"]


def test_f22_monitor_and_gate_treat_backwards_clock_as_stale(tmp_path: Path) -> None:
    clock = Clock()
    monitor = ConnectivityMonitor({Link.WAN: lambda: True}, clock=clock)
    monitor.observe()
    clock.tick(-3600)  # observation is now an hour in the "future"
    assert monitor.observation_age() is None
    path = write_config(tmp_path)
    probes = FakeProbes()
    wan = ConfiguredWan(path, ConnectivityConfig.load(path), probe_factory=probes.factory, clock=clock)
    wan.refresh()
    clock.tick(-3600)
    assert wan.wan_observation().age_seconds is None  # never "fresh"
    maker, inner = _wan_gate(wan)
    calls = len(probes.calls)
    assert _make(maker).summary == "spy" and len(probes.calls) > calls  # refreshed before dispatch


@pytest.mark.parametrize("payload", ["[1, 2]", "{\"links\": 5, \"observed_at\": \"x\"}",
                                     "{\"links\": {\"REMOTE_TRANSPORT\": 7}}"])
def test_f22_malformed_snapshot_shapes_never_crash(tmp_path: Path, payload: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.connectivity import config as connectivity_config

    path = write_config(tmp_path, background=BG)
    config = ConnectivityConfig.load(path)
    assert config.snapshot_path is not None
    config.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    config.snapshot_path.write_text(payload, encoding="utf-8")
    view = connectivity_visibility(path)
    assert view["status"] in {"CONNECTIVITY_OBSERVED", "CONNECTIVITY_NOT_OBSERVED"}
    assert view.get("stale", True) is True
    monkeypatch.setattr(connectivity_config, "tcp_probe", FakeProbes().factory)
    assert connectivity_status(path, core_ready=True, compute_nodes=None)["status"] == "CONNECTIVITY_CONFIGURED"
