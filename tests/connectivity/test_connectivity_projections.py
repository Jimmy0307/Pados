from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.connectivity import config as connectivity_config
from pados.connectivity.adapter import SimulatedConnectivity
from pados.connectivity.config import connectivity_status, connectivity_visibility
from pados.connectivity.state import ConnectivityMonitor, Link
from pados.remote.deployment import check_remote_deployment
from pados.remote.routes import register_remote_routes
from tests.remote.remote_slice_support import build_slice, hardened_settings


def _listener() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    return sock, sock.getsockname()[1]


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# Probe targets must be a PUBLIC WAN IP and a PRIVATE_OVERLAY peer (loopback is refused);
# the probe itself is redirected to local listeners so the test never leaves the host.
WAN_HOST, OVERLAY_HOST = "9.9.9.9", "100.64.0.20"


@pytest.fixture(autouse=True)
def _local_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    real = connectivity_config.tcp_probe

    def redirected(host: str, port: int, timeout: float = 1.5) -> Callable[[], bool]:
        assert host in {WAN_HOST, OVERLAY_HOST}
        return real("127.0.0.1", port, timeout)

    monkeypatch.setattr(connectivity_config, "tcp_probe", redirected)


def _config(tmp_path: Path, wan_port: int, overlay_port: int) -> str:
    path = tmp_path / "connectivity.json"
    path.write_text(json.dumps({"mode": "INTERMITTENT_WAN_HOME_BASE",
                                "wan_probe": {"host": WAN_HOST, "port": wan_port},
                                "overlay_probe": {"host": OVERLAY_HOST, "port": overlay_port},
                                "snapshot_path": str(tmp_path / "snapshot.json")}), encoding="utf-8")
    return str(path)


def test_doctor_distinguishes_remote_unreachable_from_pados_unhealthy(tmp_path: Path) -> None:
    listener, up = _listener()
    try:
        down = _closed_port()
        healthy_wan_down = connectivity_status(_config(tmp_path, down, up), core_ready=True,
                                               compute_nodes={"status": "COMPUTE_NODES_READY"})
        assert healthy_wan_down["wan"] == "OFFLINE" and healthy_wan_down["remote_status"] == "REMOTE_REACHABLE"
        assert healthy_wan_down["home_base_local_services"] == "PADOS_HEALTHY"
        assert healthy_wan_down["local_compute"] == "COMPUTE_NODES_READY"
        assert healthy_wan_down["local_compute_depends_on_wan"] is False
        remote_down = connectivity_status(_config(tmp_path, down, down), core_ready=True, compute_nodes=None)
        assert remote_down["remote_status"] == "REMOTE_UNREACHABLE"
        assert remote_down["home_base_local_services"] == "PADOS_HEALTHY"
        unhealthy = connectivity_status(_config(tmp_path, up, up), core_ready=False, compute_nodes=None)
        assert unhealthy["remote_status"] == "REMOTE_REACHABLE"
        assert unhealthy["home_base_local_services"] == "PADOS_UNHEALTHY"
    finally:
        listener.close()
    assert connectivity_status("", core_ready=True, compute_nodes=None)["status"] == "CONNECTIVITY_NOT_CONFIGURED"


def test_mission_control_block_marks_stale_snapshot(tmp_path: Path) -> None:
    assert connectivity_visibility("")["status"] == "CONNECTIVITY_NOT_CONFIGURED"
    config = _config(tmp_path, _closed_port(), _closed_port())
    assert connectivity_visibility(config)["status"] == "CONNECTIVITY_NOT_OBSERVED"
    old = datetime.now(UTC) - timedelta(hours=2)
    net = SimulatedConnectivity()
    ConnectivityMonitor({Link.WAN: net.wan_probe}, clock=lambda: old,
                        snapshot_path=tmp_path / "snapshot.json").observe()
    view = connectivity_visibility(config)
    assert view["status"] == "CONNECTIVITY_OBSERVED" and view["stale"] is True
    assert view["semantic_authority"] == "NONE"
    ConnectivityMonitor({Link.WAN: net.wan_probe}, snapshot_path=tmp_path / "snapshot.json").observe()
    assert connectivity_visibility(config)["stale"] is False


def test_remote_connectivity_route_and_home_base(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    net = SimulatedConnectivity()
    net.wan = False  # overlay up (remote connected), WAN down
    monitor = ConnectivityMonitor({Link.WAN: net.wan_probe, Link.REMOTE_TRANSPORT: net.overlay_probe})
    settings = hardened_settings()
    app = FastAPI()
    register_remote_routes(app, settings, service=s.service,
                           deployment=check_remote_deployment(settings, remote_bind_host="100.64.0.10"),
                           nodes=lambda: {"nodes": []}, connectivity=monitor.observe)
    client = TestClient(app)
    view = client.get("/remote/v1/connectivity", headers=s.owner_headers).json()
    assert view["links"]["WAN"]["state"] == "OFFLINE" and view["links"]["REMOTE_TRANSPORT"]["state"] == "CONNECTED"
    assert view["home_base"] == "RUNNING"
    home = client.get("/remote/v1/home-base", headers=s.owner_headers).json()
    assert home["home_base"] == "RUNNING" and home["connectivity"]["affects_job_state"] is False
    assert client.get("/remote/v1/connectivity").status_code == 401
    unconfigured = s.client.get("/remote/v1/connectivity", headers=s.owner_headers).json()
    assert unconfigured["status"] == "CONNECTIVITY_NOT_CONFIGURED"
