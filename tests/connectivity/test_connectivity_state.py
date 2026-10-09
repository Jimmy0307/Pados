from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pados.connectivity.adapter import SimulatedConnectivity
from pados.connectivity.egress import (
    PROVIDER_UNREACHABLE,
    EndpointLocality,
    ProviderUnreachable,
    WanEgressGate,
    endpoint_locality,
)
from pados.connectivity.state import ConnectivityMonitor, Link
from pados.remote.notifications import (
    DeliveryStatus,
    Notification,
    NotificationCenter,
    NotificationKind,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: int = 30) -> None:
        self.now += timedelta(seconds=seconds)


def _monitor(net: SimulatedConnectivity, clock: Clock, tmp_path: Path | None = None) -> ConnectivityMonitor:
    return ConnectivityMonitor({Link.WAN: net.wan_probe, Link.REMOTE_TRANSPORT: net.overlay_probe}, clock=clock,
                               snapshot_path=(tmp_path / "connectivity.json") if tmp_path else None)


def test_connected_offline_recovering_connected(tmp_path: Path) -> None:
    net, clock = SimulatedConnectivity(), Clock()
    mon = _monitor(net, clock, tmp_path)
    assert mon.observe()["links"]["WAN"]["state"] == "CONNECTED"
    net.go_offline()
    clock.tick()
    snap = mon.observe()
    assert snap["links"]["WAN"]["state"] == "OFFLINE" and snap["links"]["REMOTE_TRANSPORT"]["state"] == "OFFLINE"
    assert snap["links"]["WAN"]["last_disconnected"] == clock.now.isoformat()
    assert snap["home_base"] == "RUNNING" and snap["semantic_authority"] == "NONE" and snap["affects_job_state"] is False
    net.recover()
    clock.tick()
    assert mon.observe()["links"]["WAN"]["state"] == "RECOVERING"
    clock.tick()
    snap = mon.observe()
    assert snap["links"]["WAN"]["state"] == "CONNECTED" and snap["links"]["WAN"]["recovered_at"]
    assert json.loads((tmp_path / "connectivity.json").read_text())["links"]["WAN"]["state"] == "CONNECTED"


def test_repeated_flaps_become_intermittent() -> None:
    net, clock = SimulatedConnectivity(), Clock()
    mon = _monitor(net, clock)
    mon.observe()
    states = []
    for _ in range(3):
        net.go_offline()
        clock.tick()
        states.append(mon.observe()["links"]["WAN"]["state"])
        net.recover()
        clock.tick()
        mon.observe()
    assert states[0] == "OFFLINE" and states[-1] == "INTERMITTENT"
    net.go_offline()
    clock.tick(3600)  # flaps age out of the window
    assert mon.observe()["links"]["WAN"]["state"] == "OFFLINE"


def test_provider_only_outage_and_remote_client_only_disconnect() -> None:
    net = SimulatedConnectivity(provider_down={"api.provider.example"}, clients_down={"phone-1"})
    assert net.wan and net.overlay
    assert not net.endpoint_reachable("https://api.provider.example/v1")
    assert net.endpoint_reachable("https://other.example")
    assert not net.client_can_reach_home_base("phone-1") and net.client_can_reach_home_base("laptop-1")


@pytest.mark.parametrize(("url", "locality"), [
    ("http://127.0.0.1:11434", EndpointLocality.LOCAL),
    ("http://192.168.1.20:8767", EndpointLocality.LOCAL),
    ("http://100.64.0.5:8767", EndpointLocality.LOCAL),
    ("https://api.provider.example", EndpointLocality.CLOUD),
    ("https://203.0.113.10", EndpointLocality.CLOUD),
])
def test_endpoint_locality(url: str, locality: EndpointLocality) -> None:
    assert endpoint_locality(url) is locality


def test_wan_gate_blocks_cloud_only() -> None:
    net = SimulatedConnectivity()
    net.go_offline()
    gate = WanEgressGate(wan_available=lambda: net.wan, endpoint_reachable=net.endpoint_reachable)
    assert gate.check("http://127.0.0.1:11434") is EndpointLocality.LOCAL
    with pytest.raises(ProviderUnreachable) as info:
        gate.check("https://api.provider.example")
    assert info.value.code == PROVIDER_UNREACHABLE


def test_notification_delivery_failure_is_not_job_failure() -> None:
    net = SimulatedConnectivity()
    sent: list[dict] = []

    def push(payload: dict) -> None:
        if not net.wan:
            raise ConnectionError("push provider unreachable")
        sent.append(payload)

    center = NotificationCenter(push=push)
    net.go_offline()
    job_status = "WAITING_APPROVAL"
    status = center.emit(Notification(NotificationKind.APPROVAL_REQUIRED, "proj", "run-1"))
    assert status is DeliveryStatus.PENDING and job_status == "WAITING_APPROVAL"
    assert center.inbox[0]["delivery_status"] == "PENDING" and len(center.outbox) == 1
    assert center.flush() == 0
    net.recover()
    assert center.flush() == 1 and center.inbox[0]["delivery_status"] == "DELIVERED" and not center.outbox
    assert len(sent) == 1
