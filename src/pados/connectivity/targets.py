# SPDX-License-Identifier: AGPL-3.0-only
"""Probe target validation (SSRF / arbitrary-target defence).

Probe targets come only from the operator connectivity config file; no API or
remote client can supply one. A target must be a literal IP (no hostname, so
no DNS rebinding) with a port in 1..65535. Loopback, link-local (including the
169.254.169.254 cloud metadata address), multicast, unspecified, broadcast,
reserved and IPv4-embedding IPv6 forms are refused. The WAN probe must be a
globally routable PUBLIC address; the overlay probe must be PRIVATE_OVERLAY.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping

from pados.connectivity.state import Link
from pados.remote.deployment import classify_bind_address

_BROADCAST = ipaddress.IPv4Address("255.255.255.255")


class ProbeTargetError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_probe_target(value: object, *, link: Link) -> tuple[str, int]:
    """Return the canonical ``(ip, port)`` or raise ``ProbeTargetError``."""
    prefix = "WAN_PROBE" if link is Link.WAN else "OVERLAY_PROBE"
    if not isinstance(value, Mapping) or set(value) - {"host", "port"}:
        raise ProbeTargetError(f"{prefix}_SHAPE_INVALID")
    host, port = value.get("host"), value.get("port")
    if not isinstance(host, str):
        raise ProbeTargetError(f"{prefix}_HOST_MUST_BE_LITERAL_IP")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ProbeTargetError(f"{prefix}_HOST_MUST_BE_LITERAL_IP") from exc
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProbeTargetError(f"{prefix}_PORT_INVALID")
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped or address.sixtofour or address.teredo or address.scope_id
    ):
        raise ProbeTargetError(f"{prefix}_TARGET_FORBIDDEN_RANGE")
    if address.is_loopback:
        raise ProbeTargetError(f"{prefix}_TARGET_LOOPBACK_FORBIDDEN")
    if (address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
            or address == _BROADCAST):
        raise ProbeTargetError(f"{prefix}_TARGET_FORBIDDEN_RANGE")
    canonical = str(address)
    category = classify_bind_address(canonical)
    if link is Link.WAN:
        if category != "PUBLIC" or not address.is_global:
            raise ProbeTargetError("WAN_PROBE_MUST_BE_PUBLIC")
    elif category != "PRIVATE_OVERLAY":
        raise ProbeTargetError("OVERLAY_PROBE_MUST_BE_PRIVATE_OVERLAY")
    return canonical, port
