# SPDX-License-Identifier: AGPL-3.0-only
"""Remote deployment guard: private overlay only, fail closed on defaults.

V1 supports reaching PADOS over a private overlay (WireGuard, a Tailscale-like
mesh, or a trusted LAN). Direct public Internet exposure is unsupported, router
port forwarding is never required, and no vendor is assumed. Remote mode
refuses to start when development defaults are still in place.

A request that arrives on a non-loopback local address is served only when the
guarded launcher (``python -m pados.remote.serve``) created, bound and verified
the listening socket itself and recorded a listener attestation in this process.
A plain ``uvicorn`` launch is therefore loopback-only.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from pados.config import Settings


class NetworkModel(StrEnum):
    PRIVATE_OVERLAY_ONLY = "PRIVATE_OVERLAY_ONLY"
    DIRECT_PUBLIC_INTERNET_UNSUPPORTED = "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"


# Development defaults shipped in ``pados.config.Settings``. Remote mode must
# never run with them; the values are read from the Settings model itself so
# this guard cannot drift from the shipped defaults.
def _shipped_default(field_name: str) -> str:
    return str(Settings.model_fields[field_name].default)


# Explicit allowlist: RFC 1918, the carrier-grade NAT range used by common
# overlay meshes, and IPv6 unique-local. Everything else (including
# documentation/benchmark ranges that ``is_private`` would accept) is PUBLIC.
_PRIVATE_OVERLAY_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)


def classify_bind_address(host: str) -> str:
    """LOOPBACK | PRIVATE_OVERLAY | WILDCARD | PUBLIC | UNRESOLVED."""
    if host in {"localhost"}:
        return "LOOPBACK"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "UNRESOLVED"
    if address.is_unspecified:
        return "WILDCARD"
    if address.is_loopback:
        return "LOOPBACK"
    if any(address.version == net.version and address in net for net in _PRIVATE_OVERLAY_NETWORKS):
        return "PRIVATE_OVERLAY"
    return "PUBLIC"


@dataclass(frozen=True)
class RemoteDeploymentCheck:
    ok: bool
    network_model: NetworkModel
    violations: tuple[str, ...]
    bind_host: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "network_model": self.network_model.value,
            "violations": list(self.violations),
        }


def check_remote_deployment(settings: Settings, *, remote_bind_host: str) -> RemoteDeploymentCheck:
    violations: list[str] = []
    if settings.control_api_token_hash.upper() == _shipped_default("control_api_token_hash").upper():
        violations.append("DEFAULT_CONTROL_CREDENTIAL_FORBIDDEN")
    if settings.db_password == _shipped_default("db_password"):
        violations.append("DEFAULT_DATABASE_CREDENTIAL_FORBIDDEN")
    if classify_bind_address(settings.db_host) != "LOOPBACK":
        violations.append("DATABASE_MUST_NOT_BE_REMOTELY_EXPOSED")
    ollama_host = urlparse(settings.ollama_base_url).hostname or ""
    if classify_bind_address(ollama_host) == "WILDCARD":
        violations.append("MODEL_ENDPOINT_MUST_NOT_BE_WILDCARD")
    bind_class = classify_bind_address(remote_bind_host)
    if bind_class == "PUBLIC":
        violations.append("DIRECT_PUBLIC_INTERNET_UNSUPPORTED")
    elif bind_class == "WILDCARD":
        violations.append("WILDCARD_BIND_FORBIDDEN")
    elif bind_class == "UNRESOLVED":
        violations.append("BIND_ADDRESS_MUST_BE_LITERAL_IP")
    return RemoteDeploymentCheck(
        ok=not violations,
        network_model=NetworkModel.PRIVATE_OVERLAY_ONLY,
        violations=tuple(violations),
        bind_host=remote_bind_host,
    )


@dataclass(frozen=True)
class ListenerAttestation:
    """The verified local address of the one socket the guarded launcher listens on."""

    bind_host: str
    port: int


_ATTESTATION: ListenerAttestation | None = None


def attest_listener(sock: socket.socket, *, bind_host: str) -> ListenerAttestation:
    """Record the launcher's listening socket after verifying what it is actually bound to.

    Called only by ``pados.remote.serve`` after it created and bound the socket itself. The
    bound address must be exactly ``bind_host`` (a literal loopback or private-overlay IP).
    """
    global _ATTESTATION
    if classify_bind_address(bind_host) not in {"LOOPBACK", "PRIVATE_OVERLAY"}:
        raise PermissionError("REMOTE_LISTENER_BIND_REFUSED")
    name = sock.getsockname()
    try:
        bound = ipaddress.ip_address(str(name[0]).split("%", 1)[0])
        expected = ipaddress.ip_address(bind_host)
    except (ValueError, IndexError, TypeError) as exc:
        raise PermissionError("REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS") from exc
    if bound != expected or bound.is_unspecified:
        raise PermissionError("REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS")
    _ATTESTATION = ListenerAttestation(bind_host=str(expected), port=int(name[1]))
    return _ATTESTATION


def listener_attestation() -> ListenerAttestation | None:
    return _ATTESTATION


def _reset_listener_attestation() -> None:
    """Test hook: forget the attestation (a launcher process never calls this)."""
    global _ATTESTATION
    _ATTESTATION = None


def listener_violation(server: object, *, bind_host: str,
                       attestation: ListenerAttestation | None = None) -> str | None:
    """Check the local address a request arrived on (``scope["server"]``).

    ``scope["server"]`` is the connection's local address, not the listener's bind: a wildcard
    listener reports the concrete interface. So a request on any non-loopback address is served
    only when it arrived on the attested launcher socket at the configured overlay address.
    Loopback, a unix socket (no server value) and in-process test clients (non-IP value) are local.
    """
    if not isinstance(server, (tuple, list)) or not server or not isinstance(server[0], str):
        return None
    host = server[0]
    category = classify_bind_address(host)
    if category == "UNRESOLVED" or category == "LOOPBACK":
        return None
    if category in {"WILDCARD", "PUBLIC"}:
        return "REMOTE_LISTENER_NOT_PRIVATE"
    try:
        local = ipaddress.ip_address(host)
        same = local == ipaddress.ip_address(bind_host)
    except ValueError:
        return "REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"
    if not same:
        return "REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"
    if attestation is None:
        return "REMOTE_LISTENER_NOT_ATTESTED"
    if ipaddress.ip_address(attestation.bind_host) != local or len(server) < 2 or server[1] != attestation.port:
        return "REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"
    return None


def assert_remote_deployment_safe(settings: Settings, *, remote_bind_host: str) -> None:
    result = check_remote_deployment(settings, remote_bind_host=remote_bind_host)
    if not result.ok:
        raise PermissionError(",".join(result.violations))
