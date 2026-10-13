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
