# SPDX-License-Identifier: AGPL-3.0-only
"""Provider network requirement, derived from extension-level metadata.

The requirement comes from ``ProviderCapabilityProfile.locality`` (Worker
Router workers via ``profile_for_worker``), from endpoint locality, or from
explicit operator metadata in the connectivity config; never from a provider
name. Missing metadata is conservative (``WAN_REQUIRED``). Invalid or
contradictory metadata fails closed with PROVIDER_CONNECTIVITY_METADATA_INVALID.
A requirement only decides whether dispatch may proceed; it never selects,
ranks or substitutes a provider.
"""
from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pados.enterprise.compute import Locality, ProviderCapabilityProfile
from pados.remote.deployment import classify_bind_address

PROVIDER_CONNECTIVITY_METADATA_INVALID = "PROVIDER_CONNECTIVITY_METADATA_INVALID"


class NetworkRequirement(StrEnum):
    LOCAL_ONLY = "LOCAL_ONLY"
    PRIVATE_NETWORK = "PRIVATE_NETWORK"
    WAN_REQUIRED = "WAN_REQUIRED"
    NETWORK_OPTIONAL = "NETWORK_OPTIONAL"


class ConnectivityGateBlocked(PermissionError):
    """Dispatch refused before execution (fail closed); ``code`` is the reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderMetadataInvalid(ConnectivityGateBlocked):
    def __init__(self) -> None:
        super().__init__(PROVIDER_CONNECTIVITY_METADATA_INVALID)


_REMOTE_FORBIDDEN = frozenset({NetworkRequirement.LOCAL_ONLY, NetworkRequirement.NETWORK_OPTIONAL})


def requirement_for_endpoint(url: str) -> NetworkRequirement:
    host = urlparse(url).hostname or ""
    category = classify_bind_address(host)
    if category == "LOOPBACK":
        return NetworkRequirement.LOCAL_ONLY
    if category == "PRIVATE_OVERLAY":
        return NetworkRequirement.PRIVATE_NETWORK
    return NetworkRequirement.WAN_REQUIRED  # public, hostname or unresolved: conservative


def parse_requirement(value: object) -> NetworkRequirement:
    if not isinstance(value, str):
        raise ProviderMetadataInvalid()
    try:
        return NetworkRequirement(value)
    except ValueError as exc:
        raise ProviderMetadataInvalid() from exc


def resolve_requirement(*, profile: ProviderCapabilityProfile | None = None, endpoint: str | None = None,
                        explicit: object | None = None) -> NetworkRequirement:
    """Explicit operator metadata, then endpoint locality, then profile locality.

    A REMOTE profile may never be declared LOCAL_ONLY or NETWORK_OPTIONAL.
    """
    remote = profile is not None and profile.locality is Locality.REMOTE
    if explicit is not None:
        requirement = parse_requirement(explicit)
        if remote and requirement in _REMOTE_FORBIDDEN:
            raise ProviderMetadataInvalid()
        return requirement
    if endpoint is not None:
        requirement = requirement_for_endpoint(endpoint)
        return NetworkRequirement.WAN_REQUIRED if remote and requirement in _REMOTE_FORBIDDEN else requirement
    if profile is not None and profile.locality is Locality.LOCAL:
        return NetworkRequirement.LOCAL_ONLY
    return NetworkRequirement.WAN_REQUIRED
