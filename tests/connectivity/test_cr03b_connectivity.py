"""Regression tests for Codex verification CR-03b (connectivity): CC-01/CC-06 coherence, NEW-01, CC-10."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pados.connectivity import runtime as runtime_module
from pados.connectivity.config import ConnectivityConfig, connectivity_status
from pados.connectivity.history import ConnectivityHistory
from pados.connectivity.runtime import ConfiguredWan, connectivity_reader, start_runtime, stop_runtime
from pados.connectivity.state import ConnectivityMonitor, ConnectivityState, Link, observation_age_seconds
from tests.connectivity.connectivity_support import FakeProbes, write_config

BG = {"enabled": True, "interval_seconds": 10, "timeout_seconds": 2, "max_backoff_seconds": 80}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _no_leaked_runtime() -> Iterator[None]:
    yield
    stop_runtime()
    runtime_module._ACTIVE = None


def _forbid_split_reads(monkeypatch: pytest.MonkeyPatch, monitor: ConnectivityMonitor) -> None:
    def split(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("state and age must come from one coherent observation()")

    monkeypatch.setattr(monitor, "state", split)
    monkeypatch.setattr(monitor, "observation_age", split)
    monkeypatch.setattr(monitor, "projection", split)


# --- CC-01 / CC-06: one coherent observation object -----------------------------------------------

def test_cr03b_cc01_observation_state_and_age_come_from_one_snapshot() -> None:
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    wan_up = {"value": True}

    def wan() -> bool:
        if not wan_up["value"]:
            entered.set()
            release.wait(5)
        return wan_up["value"]

    monitor = ConnectivityMonitor({Link.WAN: wan}, clock=clock)
    monitor.observe()  # CONNECTED at t0
    clock.tick(400)  # that CONNECTED observation is now expired
    wan_up["value"] = False
    worker = threading.Thread(target=monitor.observe)
    worker.start()  # a newer OFFLINE observation is in progress
    assert entered.wait(5)
    during = monitor.observation()
    # never "CONNECTED" paired with the newer observation's fresh age
    assert during.state(Link.WAN) is ConnectivityState.CONNECTED and during.age_seconds == 400.0
    release.set()
    worker.join(5)
    after = monitor.observation()
    assert after.state(Link.WAN) is ConnectivityState.OFFLINE and after.age_seconds == 0.0
    assert after.snapshot["links"]["WAN"]["state"] == "OFFLINE"
    assert observation_age_seconds(after.snapshot["observed_at"], clock()) == after.age_seconds
    assert after.sequence == during.sequence + 1


def test_cr03b_cc01_gate_reads_one_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path)
    probes, clock = FakeProbes(), Clock()
    wan = ConfiguredWan(path, ConnectivityConfig.load(path), probe_factory=probes.factory, clock=clock)
    wan.refresh()
    monitor = wan._monitor()
    _forbid_split_reads(monkeypatch, monitor)
    observed = wan.wan_observation()
    assert observed.observable and observed.state is ConnectivityState.CONNECTED and observed.age_seconds == 0.0


def test_cr03b_cc06_runtime_view_route_and_doctor_read_one_observation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(wan=False), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    runtime.probe.run_once()
    _forbid_split_reads(monkeypatch, runtime.monitor)
    view = runtime.view()
    assert view["links"]["WAN"]["state"] == "OFFLINE" and view["observation_age_seconds"] == 0.0
    assert connectivity_reader(path)()["sequence"] == view["sequence"]
    doctor = connectivity_status(path, core_ready=True, compute_nodes=None)
    assert doctor["wan"] == "OFFLINE" and doctor["stale"] is False


def test_cr03b_cc06_view_freshness_matches_its_own_snapshot(tmp_path: Path) -> None:
    path = write_config(tmp_path, background=BG)
    probes, clock = FakeProbes(), Clock()
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory, clock=clock)
    assert runtime is not None
    runtime.probe.run_once()
    clock.tick(42)
    view = runtime.view()
    assert view["observation_age_seconds"] == observation_age_seconds(view["observed_at"], clock()) == 42.0


# --- NEW-01: history validator never raises on unhashable JSON ----------------------------------

@pytest.mark.parametrize("forged", [{"link": []}, {"from": {}}, {"to": []}, {"at": []}, {"at": {"x": 1}},
                                    {"link": 5}, {"at": "x" * 100}])
def test_cr03b_new01_history_drops_unhashable_fields_without_raising(tmp_path: Path,
                                                                    forged: dict[str, Any]) -> None:
    path = tmp_path / "history.json"
    good = {"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": datetime.now(UTC).isoformat()}
    path.write_text(json.dumps({"entries": [good, {**good, **forged}]}), encoding="utf-8")
    history = ConnectivityHistory(path)
    assert history.entries() == [good]
    assert json.loads(path.read_text())["entries"] == [good]  # removed from disk too


# --- CC-10: reading history removes expired rows from disk --------------------------------------

def test_cr03b_cc10_history_read_removes_expired_rows_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    now = datetime.now(UTC)
    rows = [{"link": "WAN", "from": "CONNECTED", "to": "OFFLINE", "at": (now - timedelta(days=d)).isoformat()}
            for d in (30, 1)]
    path.write_text(json.dumps({"entries": rows}), encoding="utf-8")
    assert len(ConnectivityHistory(path).entries()) == 1
    assert [r["at"] for r in json.loads(path.read_text())["entries"]] == [rows[1]["at"]]


def test_cr03b_cc10_corrupt_history_document_is_rewritten_on_read(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"entries": "Refactor the payroll module"}), encoding="utf-8")
    assert ConnectivityHistory(path).entries() == []
    assert json.loads(path.read_text())["entries"] == []
