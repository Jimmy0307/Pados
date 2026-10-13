"""Connectivity Closure A + D: background probe, shared runtime, probe-target validation, history."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI

from pados.connectivity.background import BackgroundProbe, BackgroundProbeConfigError, BackgroundProbeSettings
from pados.connectivity.config import (
    ConnectivityConfig,
    ConnectivityConfigError,
    connectivity_status,
    connectivity_visibility,
)
from pados.connectivity.history import ENTRY_KEYS, ConnectivityHistory
from pados.connectivity.runtime import (
    active_runtime,
    connectivity_lifespan,
    connectivity_reader,
    start_runtime,
    stop_runtime,
)
from pados.connectivity.state import ConnectivityMonitor, ConnectivityState, Link
from pados.connectivity.storage import assert_record_schema
from pados.connectivity.targets import ProbeTargetError, validate_probe_target
from pados.lifecycle.minimization import DataMinimizationViolation
from tests.connectivity.connectivity_support import OVERLAY_HOST, WAN_HOST, FakeProbes, write_config

BG = {"enabled": True, "interval_seconds": 10, "timeout_seconds": 2, "max_backoff_seconds": 80}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float = 30) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _no_leaked_runtime() -> Iterator[None]:
    yield
    stop_runtime()


def _probe(probes: FakeProbes, clock: Clock, settings: BackgroundProbeSettings | None = None) -> BackgroundProbe:
    monitor = ConnectivityMonitor({Link.WAN: probes.factory(WAN_HOST, 443, 2),
                                   Link.REMOTE_TRANSPORT: probes.factory(OVERLAY_HOST, 8089, 2)}, clock=clock)
    return BackgroundProbe(monitor, settings or BackgroundProbeSettings(**BG))


# --- A. probe mechanism -------------------------------------------------------------------------

def test_run_once_updates_projection_and_backs_off_exponentially_while_offline() -> None:
    probes, clock = FakeProbes(), Clock()
    probe = _probe(probes, clock)
    assert probe.run_once() == 10 and probe.monitor.state(Link.WAN) is ConnectivityState.CONNECTED
    probes.wan = False
    delays = []
    for _ in range(6):
        clock.tick()
        delays.append(probe.run_once())
    assert delays == [10, 20, 40, 80, 80, 80]  # exponential, capped at max_backoff_seconds
    assert probe.monitor.state(Link.WAN) is ConnectivityState.OFFLINE
    probes.wan = True
    clock.tick()
    assert probe.run_once() == 10 and probe.monitor.state(Link.WAN) is ConnectivityState.RECOVERING
    clock.tick()
    probe.run_once()
    assert probe.monitor.state(Link.WAN) is ConnectivityState.CONNECTED


def test_probe_debug_is_bounded_in_memory_and_carries_no_targets() -> None:
    probes, clock = FakeProbes(wan=False), Clock()
    probe = _probe(probes, clock)
    for _ in range(200):
        probe.run_once()
    assert len(probe.debug_events) == 64
    assert WAN_HOST not in repr(list(probe.debug_events)) and OVERLAY_HOST not in repr(list(probe.debug_events))


def test_run_forever_uses_injected_waiter_and_stops() -> None:
    probes, clock = FakeProbes(), Clock()
    waits: list[float] = []

    def waiter(seconds: float) -> bool:
        waits.append(seconds)
        clock.tick(seconds)
        return len(waits) >= 3

    monitor = ConnectivityMonitor({Link.WAN: probes.factory(WAN_HOST, 443, 2)}, clock=clock)
    BackgroundProbe(monitor, BackgroundProbeSettings(**BG), waiter=waiter).run_forever()
    assert waits == [10, 10, 10] and probes.calls == ["WAN", "WAN", "WAN"]


def test_thread_start_stop_is_clean_and_idempotent() -> None:
    probes = FakeProbes()
    probe = _probe(probes, Clock())
    assert probe.start() is True and probe.running
    assert probe.start() is False  # one thread only
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while not probes.calls and datetime.now(UTC) < deadline:
        threading.Event().wait(0.01)
    assert probes.calls
    assert probe.stop(timeout=5) is True and not probe.running
    assert probe.stop() is True  # idempotent
    assert not any(t.name == "pados-connectivity-probe" for t in threading.enumerate())


@pytest.mark.parametrize("override", [
    {"interval_seconds": 4}, {"interval_seconds": 3601}, {"timeout_seconds": 0}, {"timeout_seconds": 11},
    {"max_backoff_seconds": 5}, {"max_backoff_seconds": 4000}, {"enabled": "yes"}, {"interval_seconds": True},
    {"unknown": 1},
])
def test_background_settings_are_bounded(override: dict[str, object]) -> None:
    with pytest.raises(BackgroundProbeConfigError):
        BackgroundProbeSettings.from_mapping({**BG, **override})


def test_probe_timeout_from_config_is_bounded_and_used(tmp_path: Path) -> None:
    probes = FakeProbes()
    ConnectivityConfig.load(write_config(tmp_path, background=BG)).monitor(probe_factory=probes.factory)
    assert probes.timeouts == [2.0, 2.0]
    probes = FakeProbes()
    ConnectivityConfig.load(write_config(tmp_path)).monitor(probe_factory=probes.factory)
    assert probes.timeouts == [1.5, 1.5]  # existing default when background is not configured


# --- A. SSRF / probe target defence -------------------------------------------------------------

@pytest.mark.parametrize(("link", "target", "code"), [
    (Link.WAN, {"host": "provider.example", "port": 443}, "WAN_PROBE_HOST_MUST_BE_LITERAL_IP"),
    (Link.WAN, {"host": "localhost", "port": 443}, "WAN_PROBE_HOST_MUST_BE_LITERAL_IP"),
    (Link.WAN, {"host": "127.0.0.1", "port": 443}, "WAN_PROBE_TARGET_LOOPBACK_FORBIDDEN"),
    (Link.WAN, {"host": "::1", "port": 443}, "WAN_PROBE_TARGET_LOOPBACK_FORBIDDEN"),
    (Link.WAN, {"host": "169.254.169.254", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "fe80::1", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "224.0.0.1", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "0.0.0.0", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "255.255.255.255", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "240.0.0.1", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "::ffff:169.254.169.254", "port": 80}, "WAN_PROBE_TARGET_FORBIDDEN_RANGE"),
    (Link.WAN, {"host": "10.0.0.5", "port": 443}, "WAN_PROBE_MUST_BE_PUBLIC"),
    (Link.WAN, {"host": "100.64.0.5", "port": 443}, "WAN_PROBE_MUST_BE_PUBLIC"),
    (Link.WAN, {"host": "203.0.113.10", "port": 443}, "WAN_PROBE_MUST_BE_PUBLIC"),  # documentation range
    (Link.WAN, {"host": WAN_HOST, "port": 0}, "WAN_PROBE_PORT_INVALID"),
    (Link.WAN, {"host": WAN_HOST, "port": 65536}, "WAN_PROBE_PORT_INVALID"),
    (Link.WAN, {"host": WAN_HOST, "port": "443"}, "WAN_PROBE_PORT_INVALID"),
    (Link.WAN, {"host": WAN_HOST, "port": 443, "path": "/x"}, "WAN_PROBE_SHAPE_INVALID"),
    (Link.REMOTE_TRANSPORT, {"host": WAN_HOST, "port": 8089}, "OVERLAY_PROBE_MUST_BE_PRIVATE_OVERLAY"),
    (Link.REMOTE_TRANSPORT, {"host": "127.0.0.1", "port": 8089}, "OVERLAY_PROBE_TARGET_LOOPBACK_FORBIDDEN"),
    (Link.REMOTE_TRANSPORT, {"host": "169.254.1.1", "port": 8089}, "OVERLAY_PROBE_TARGET_FORBIDDEN_RANGE"),
])
def test_probe_target_validation_rejects_unsafe_targets(link: Link, target: dict[str, object], code: str) -> None:
    with pytest.raises(ProbeTargetError) as info:
        validate_probe_target(target, link=link)
    assert info.value.code == code


def test_probe_target_validation_accepts_public_wan_and_overlay_peer() -> None:
    assert validate_probe_target({"host": WAN_HOST, "port": 443}, link=Link.WAN) == (WAN_HOST, 443)
    assert validate_probe_target({"host": "2620:fe::fe", "port": 443}, link=Link.WAN) == ("2620:fe::fe", 443)
    for host in (OVERLAY_HOST, "10.1.2.3", "192.168.1.20", "fd00::5"):
        assert validate_probe_target({"host": host, "port": 8089}, link=Link.REMOTE_TRANSPORT)[0] == host


def test_invalid_target_makes_config_invalid_and_nothing_is_probed(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG, wan_probe={"host": "169.254.169.254", "port": 80})
    with pytest.raises(ConnectivityConfigError):
        ConnectivityConfig.load(path)
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["status"] == "CONNECTIVITY_CONFIG_INVALID" and doctor["reason"] == "WAN_PROBE_TARGET_FORBIDDEN_RANGE"
    assert "169.254" not in json.dumps(doctor)
    probes = FakeProbes()
    assert start_runtime(path, probe_factory=probes.factory) is None and active_runtime() is None
    assert probes.calls == [] and not (tmp_path / "runtime").exists()
    assert connectivity_reader(path)()["status"] == "CONNECTIVITY_CONFIG_INVALID"


# --- A. single process-wide runtime -------------------------------------------------------------

def test_not_configured_starts_nothing_and_stays_byte_compatible(tmp_path: Path) -> None:
    assert start_runtime("") is None and active_runtime() is None
    assert connectivity_status("", core_ready=True, compute_nodes=None) == {
        "status": "CONNECTIVITY_NOT_CONFIGURED", "home_base_local_services": "PADOS_HEALTHY",
        "local_compute": "COMPUTE_NODES_NOT_CONFIGURED", "local_compute_depends_on_wan": False,
        "semantic_authority": "NONE"}
    assert connectivity_visibility("") == {"status": "CONNECTIVITY_NOT_CONFIGURED", "semantic_authority": "NONE"}
    disabled = write_config(tmp_path, background={"enabled": False})
    assert start_runtime(disabled) is None and active_runtime() is None


def test_doctor_mission_control_remote_route_and_gate_share_one_monitor(tmp_path: Path) -> None:
    from pados.connectivity.runtime import ConfiguredWan

    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(wan=False), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None and start_runtime(path) is runtime  # process-wide singleton
    runtime.probe.run_once()
    calls = len(probes.calls)
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["probe_source"] == "BACKGROUND_PROBE" and doctor["wan"] == "OFFLINE"
    assert doctor["remote_status"] == "REMOTE_REACHABLE"
    mission = connectivity_visibility(path)
    assert mission["links"]["WAN"]["state"] == "OFFLINE" and mission["semantic_authority"] == "NONE"
    route = connectivity_reader(path)()
    assert route == runtime.view()
    wan = ConfiguredWan(path, runtime.config)
    assert wan.wan_observation().state is ConnectivityState.OFFLINE
    assert len(probes.calls) == calls  # readers never probe on their own while the runtime is active
    stop_runtime(runtime)
    assert active_runtime() is None


def test_doctor_reads_fresh_background_snapshot_from_another_process(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    config = ConnectivityConfig.load(path)
    assert config.snapshot_path is not None
    ConnectivityMonitor({Link.WAN: lambda: False}, snapshot_path=config.snapshot_path).observe()
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["probe_source"] == "BACKGROUND_SNAPSHOT" and doctor["wan"] == "OFFLINE"


def test_lifespan_starts_and_stops_background_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.connectivity import config as connectivity_config

    probes = FakeProbes()
    monkeypatch.setattr(connectivity_config, "tcp_probe", probes.factory)
    path = write_config(tmp_path, background=BG)

    async def cycle() -> None:
        async with connectivity_lifespan(path)(FastAPI()):
            runtime = active_runtime(path)
            assert runtime is not None and runtime.probe.running
        assert active_runtime() is None

    asyncio.run(cycle())
    assert not any(t.name == "pados-connectivity-probe" for t in threading.enumerate())

    async def unconfigured() -> None:
        async with connectivity_lifespan("")(FastAPI()):
            assert active_runtime() is None

    asyncio.run(unconfigured())


# --- D. connectivity data lifecycle -------------------------------------------------------------

def test_history_records_transitions_only_bounded_by_count_and_age(tmp_path: Path) -> None:
    clock = Clock()
    history = ConnectivityHistory(tmp_path / "history.json", max_entries=5, clock=clock)
    history.record(Link.WAN, ConnectivityState.CONNECTED, ConnectivityState.OFFLINE, clock.now)
    assert history.entries() == [{"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": clock.now.isoformat()}]
    for _ in range(10):
        clock.tick()
        history.record(Link.WAN, ConnectivityState.OFFLINE, ConnectivityState.RECOVERING, clock.now)
    assert len(history.entries()) == 5  # count bound, pruned on write
    clock.tick(15 * 24 * 3600)  # past the R1 default (14 days)
    history.record(Link.REMOTE_TRANSPORT, ConnectivityState.CONNECTED, ConnectivityState.OFFLINE, clock.now)
    assert [row["link"] for row in history.entries()] == ["REMOTE_TRANSPORT"]  # age bound
    document = json.loads((tmp_path / "history.json").read_text())
    assert document["kind"] == "CONNECTIVITY_HISTORY" and document["retention_class"] == "R1_OPERATIONAL"
    if os.name != "nt":
        assert (tmp_path / "history.json").stat().st_mode & 0o777 == 0o600
    assert ConnectivityHistory(tmp_path / "h2.json", retention=timedelta(days=90)).retention == timedelta(days=14)


def test_history_schema_and_minimization_guard() -> None:
    entry = {"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": datetime.now(UTC).isoformat()}
    assert_record_schema(entry, ENTRY_KEYS)
    with pytest.raises(DataMinimizationViolation):
        assert_record_schema({**entry, "public_ip": WAN_HOST}, ENTRY_KEYS)  # outside the schema
    with pytest.raises(DataMinimizationViolation):
        assert_record_schema({**entry, "at": "sk-" + "a" * 24}, ENTRY_KEYS)  # credential-shaped value


def test_runtime_files_contain_no_probe_target_ips(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    for wan in (True, False, True, True, False):
        probes.wan = probes.overlay = wan
        clock.tick()
        runtime.probe.run_once()
    runtime_dir = tmp_path / "runtime"
    written = sorted(p.name for p in runtime_dir.iterdir())
    # no temp files left behind; the lock sidecars are empty
    assert written == ["connectivity-history.json", "connectivity-history.json.lock", "connectivity.json",
                       "connectivity.json.lock"]
    assert all((runtime_dir / name).read_bytes() == b"" for name in written if name.endswith(".lock"))
    history = json.loads((runtime_dir / "connectivity-history.json").read_text())["entries"]
    assert history and all(set(row) == {"link", "from", "to", "at"} for row in history)
    for file in runtime_dir.iterdir():
        text = file.read_text()
        assert WAN_HOST not in text and OVERLAY_HOST not in text
        for word in ("ssid", "wireguard", "tailscale", "public_ip", "password", "key"):
            assert word not in text.lower()
    doctor = json.dumps(connectivity_status(path, core_ready=True, compute_nodes=None))
    mission = json.dumps(connectivity_visibility(path))
    for text in (doctor, mission, json.dumps(runtime.view())):
        assert WAN_HOST not in text and OVERLAY_HOST not in text
