# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 3: the OpenMAIC adapter must accept only true loopback, never private/overlay networks.

Core's ``endpoint_locality`` LOCAL bucket includes RFC1918 / CGNAT / IPv6 ULA (``PRIVATE_OVERLAY``). This
adapter is a loopback-only runtime, so it must classify the host with ``classify_bind_address`` and accept
only ``LOOPBACK`` (127.0.0.1, ::1, localhost). Anything else fails closed before any HTTP call.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import pytest
from loop_learning_support import learning_request

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.contracts import FailureCode

_ROUTES: dict[tuple[str, str], tuple[int, Any]] = {
    ("GET", "/api/health"): (200, {"status": "ok", "version": "1.2.0"}),
    ("POST", "/api/generate-classroom"): (202, {"jobId": "job-1", "step": "queued", "progress": 0}),
}


class HostAgnosticHttp:
    """Records (method, path) and matches by path only, so any loopback host form is exercised equally."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, body: Mapping[str, Any] | None,
                 timeout: float) -> tuple[int, Any]:
        path = urlparse(url).path
        self.calls.append((method, path))
        for (route_method, route_path), result in _ROUTES.items():
            if route_method == method and path.startswith(route_path):
                return result
        return 404, None


LOOPBACK_ENDPOINTS = ["http://127.0.0.1:3000", "http://[::1]:3000", "http://localhost:3000"]
NON_LOOPBACK_ENDPOINTS = [
    "http://192.168.1.10:3000", "http://10.1.2.3:3000", "http://172.16.5.5:3000",
    "http://100.64.1.1:3000",   # CGNAT
    "http://[fd00::1]:3000",    # IPv6 ULA
    "http://8.8.8.8:3000",      # public
]


@pytest.mark.parametrize("endpoint", LOOPBACK_ENDPOINTS)
def test_loopback_endpoint_is_accepted(endpoint: str) -> None:
    http = HostAgnosticHttp()
    provider = OpenMaicLearningProvider(endpoint, http)
    outcome = provider.create_session(learning_request())
    assert outcome.failure is None  # loopback proceeds to the runtime
    assert http.calls  # an HTTP call was attempted


@pytest.mark.parametrize("endpoint", NON_LOOPBACK_ENDPOINTS)
def test_non_loopback_endpoint_fails_closed(endpoint: str) -> None:
    http = HostAgnosticHttp()
    provider = OpenMaicLearningProvider(endpoint, http)
    outcome = provider.create_session(learning_request())
    assert outcome.failure is FailureCode.UNAUTHORIZED
    assert http.calls == []  # never contacted off-loopback


@pytest.mark.parametrize("endpoint", NON_LOOPBACK_ENDPOINTS)
def test_non_loopback_health_is_degraded(endpoint: str) -> None:
    provider = OpenMaicLearningProvider(endpoint, HostAgnosticHttp())
    health = provider.health()
    assert health.code == FailureCode.UNAUTHORIZED.value
