# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic connectivity adapter for CI (no OS firewall manipulation).

Models the distinct failure classes the addendum requires: WAN offline,
private-overlay (remote transport) offline, a single provider outage, and a
remote-client-only disconnect, while the Home Base LAN stays up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from pados.remote.deployment import classify_bind_address


class TransportUnavailable(ConnectionError):
    """The remote client cannot reach the Home Base (transport, not PADOS health)."""


@dataclass
class SimulatedConnectivity:
    wan: bool = True
    overlay: bool = True
    lan: bool = True
    provider_down: set[str] = field(default_factory=set)
    clients_down: set[str] = field(default_factory=set)

    # Probes for ConnectivityMonitor
    def wan_probe(self) -> bool:
        return self.wan

    def overlay_probe(self) -> bool:
        return self.overlay

    def endpoint_reachable(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        if host in self.provider_down:
            return False
        if classify_bind_address(host) in {"LOOPBACK", "PRIVATE_OVERLAY"}:
            return self.lan
        return self.wan

    def client_can_reach_home_base(self, client_id: str) -> bool:
        return self.overlay and client_id not in self.clients_down

    # Scenario helpers
    def go_offline(self) -> None:
        self.wan = False
        self.overlay = False

    def recover(self) -> None:
        self.wan = True
        self.overlay = True
