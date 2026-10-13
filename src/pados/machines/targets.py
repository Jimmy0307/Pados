# SPDX-License-Identifier: AGPL-3.0-only
"""Machine connection target validation (arbitrary-target / SSRF defence).

Connection targets come only from the operator-owned machine inventory; no API
or remote client can supply one. A host is either a literal private / LAN /
overlay IP (RFC 1918, carrier-grade NAT overlay, IPv6 unique-local: the
``pados.remote.deployment`` allowlist) or a DNS hostname the operator explicitly
confirmed. Public, loopback, link-local (including the 169.254.169.254 cloud
metadata address), multicast, unspecified, reserved, broadcast and
IPv4-embedding IPv6 forms are refused. A confirmed hostname must still resolve
only to allowed addresses at probe time (``validate_address``).
"""
from __future__ import annotations

import ipaddress
import re

from pados.machines.errors import MachineError
from pados.remote.deployment import classify_bind_address

_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def validate_address(value: str) -> str:
    """Canonical form of a literal IP that is an allowed machine target, else ``MachineError``."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise MachineError("HOST_MUST_BE_LITERAL_IP_OR_CONFIRMED_HOSTNAME") from None
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped or address.sixtofour or address.teredo or address.scope_id
    ):
        raise MachineError("HOST_FORBIDDEN_RANGE")
    if address.is_loopback:
        raise MachineError("HOST_LOOPBACK_FORBIDDEN")
    if (address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
            or address == _BROADCAST):
        raise MachineError("HOST_FORBIDDEN_RANGE")
    canonical = str(address)
    if classify_bind_address(canonical) != "PRIVATE_OVERLAY":
        raise MachineError("HOST_MUST_BE_PRIVATE_LAN_OR_OVERLAY")
    return canonical


def is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def validate_machine_host(host: object, *, hostname_confirmed: bool) -> str:
    """Return the canonical host (IP literal or lower-case hostname) or raise ``MachineError``."""
    if not isinstance(host, str) or not 1 <= len(host) <= 253:
        raise MachineError("HOST_INVALID")
    if is_ip_literal(host):
        return validate_address(host)
    name = host.lower().removesuffix(".")
    labels = name.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise MachineError("HOST_INVALID")
    if labels[-1].isdigit() or all(label.isdigit() or label.startswith("0x") for label in labels):
        # Numeric shorthand (``10.1``, ``0x7f.1``) is an address in disguise, not a hostname.
        raise MachineError("HOST_NUMERIC_FORM_FORBIDDEN")
    if name in _LOCAL_NAMES or name.endswith(".localhost"):
        raise MachineError("HOST_LOOPBACK_FORBIDDEN")
    if not hostname_confirmed:
        raise MachineError("HOSTNAME_REQUIRES_OPERATOR_CONFIRMATION")
    return name


def validate_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise MachineError("PORT_INVALID")
    return port


def validate_username(username: object) -> str:
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise MachineError("USERNAME_INVALID")
    return username
