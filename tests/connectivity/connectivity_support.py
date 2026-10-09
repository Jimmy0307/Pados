"""Simulated remote client over the deterministic remote slice + connectivity adapter."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pados.connectivity.adapter import SimulatedConnectivity, TransportUnavailable
from tests.remote.remote_slice_support import Slice


@dataclass
class SimRemoteClient:
    slice: Slice
    net: SimulatedConnectivity
    client_id: str = "phone-1"
    headers: dict[str, str] | None = None  # e.g. a server-bound session for step-up decisions

    def _guard(self) -> None:
        if not self.net.client_can_reach_home_base(self.client_id):
            raise TransportUnavailable("REMOTE_TRANSPORT_UNAVAILABLE")

    def get(self, path: str) -> Any:
        self._guard()
        return self.slice.client.get(f"/remote/v1{path}", headers=self.headers or self.slice.owner_headers)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self._guard()
        return self.slice.client.post(f"/remote/v1{path}", json=body, headers=self.headers or self.slice.owner_headers)


# Operator-config probe targets used by the closure tests: a public WAN IP and an
# overlay peer. Probes are always injected (FakeProbes), so nothing leaves the host.
WAN_HOST, OVERLAY_HOST = "9.9.9.9", "100.64.0.20"


@dataclass
class FakeProbes:
    wan: bool = True
    overlay: bool = True
    timeouts: list[float] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def factory(self, host: str, port: int, timeout: float) -> Callable[[], bool]:
        assert host in {WAN_HOST, OVERLAY_HOST}, "probe target must come from the operator config"
        self.timeouts.append(timeout)
        link = "WAN" if host == WAN_HOST else "OVERLAY"

        def probe() -> bool:
            self.calls.append(link)
            return self.wan if link == "WAN" else self.overlay

        return probe


def write_config(tmp_path: Path, *, background: dict[str, Any] | None = None, **overrides: Any) -> str:
    data: dict[str, Any] = {
        "mode": "INTERMITTENT_WAN_HOME_BASE",
        "wan_probe": {"host": WAN_HOST, "port": 443},
        "overlay_probe": {"host": OVERLAY_HOST, "port": 8089},
        "snapshot_path": str(tmp_path / "runtime" / "connectivity.json"),
    }
    if background is not None:
        data["background_probe"] = background
    data.update(overrides)
    path = tmp_path / "connectivity-config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)
