# SPDX-License-Identifier: AGPL-3.0-only
"""WAN egress gate for cloud providers.

A provider endpoint on loopback / private ranges is LOCAL and is never gated by
WAN state (WAN connectivity != local compute health). A PUBLIC endpoint while WAN
is unavailable fails closed with PROVIDER_UNREACHABLE. There is no fallback:
the gate never selects or substitutes another provider.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from urllib.parse import urlparse

from pados.remote.deployment import classify_bind_address

PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"


class EndpointLocality(StrEnum):
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


def endpoint_locality(url: str) -> EndpointLocality:
    host = urlparse(url).hostname or ""
    return (EndpointLocality.LOCAL if classify_bind_address(host) in {"LOOPBACK", "PRIVATE_OVERLAY"}
            else EndpointLocality.CLOUD)


def endpoint_is_loopback(url: str) -> bool:
    """True only when the endpoint host is true loopback (127.0.0.1, ::1, localhost).

    Stricter than ``endpoint_locality``: a loopback-only runtime must not accept the private/overlay ranges
    (RFC1918, CGNAT, IPv6 ULA) that LOCAL also covers. The host is classified, never DNS-resolved, so no
    rebinding can turn a non-loopback name into an accepted one. This does not change ``endpoint_locality``.
    """
    host = urlparse(url).hostname or ""
    return classify_bind_address(host) == "LOOPBACK"


class ProviderUnreachable(ConnectionError):
    code = PROVIDER_UNREACHABLE


class WanEgressGate:
    def __init__(self, *, wan_available: Callable[[], bool],
                 endpoint_reachable: Callable[[str], bool] | None = None) -> None:
        self._wan_available = wan_available
        self._endpoint_reachable = endpoint_reachable

    def check(self, endpoint: str) -> EndpointLocality:
        locality = endpoint_locality(endpoint)
        if locality is EndpointLocality.CLOUD and not self._wan_available():
            raise ProviderUnreachable(PROVIDER_UNREACHABLE)
        if self._endpoint_reachable is not None and not self._endpoint_reachable(endpoint):
            raise ProviderUnreachable(PROVIDER_UNREACHABLE)
        return locality
