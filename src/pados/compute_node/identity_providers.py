# SPDX-License-Identifier: AGPL-3.0-only
"""Node identity providers and deployment security profiles.

``HMAC_SHARED_SECRET`` (the accepted V1 mechanism in ``identity.py``) is the
TRUSTED_LAN profile: loopback, home LAN, controlled private LAN. It is not an
Internet-grade identity. ``ASYMMETRIC_IDENTITY`` (Ed25519 / mTLS) is the
hardened/remote profile; it is designed here but not implemented in Core, and
it fails closed until an optional security extension provides it.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pados.compute_node.identity import identity_proof, verify


class SecurityProfile(StrEnum):
    LOCAL_SINGLE_USER = "LOCAL_SINGLE_USER"
    TRUSTED_LAN = "TRUSTED_LAN"
    HARDENED_ENTERPRISE = "HARDENED_ENTERPRISE"


class IdentityProfile(StrEnum):
    HMAC_SHARED_SECRET = "HMAC_SHARED_SECRET"
    ASYMMETRIC_IDENTITY = "ASYMMETRIC_IDENTITY"


# Which identity profiles each deployment profile accepts.
ACCEPTED_IDENTITY = {
    SecurityProfile.LOCAL_SINGLE_USER: frozenset({IdentityProfile.HMAC_SHARED_SECRET}),
    SecurityProfile.TRUSTED_LAN: frozenset({IdentityProfile.HMAC_SHARED_SECRET}),
    SecurityProfile.HARDENED_ENTERPRISE: frozenset({IdentityProfile.ASYMMETRIC_IDENTITY}),
}


class NodeIdentityProvider(Protocol):
    profile: IdentityProfile

    def available(self) -> bool: ...

    def prove(self, node_id: str, nonce: str) -> str: ...

    def verify(self, node_id: str, nonce: str, proof: object) -> bool: ...


class HmacSharedSecretIdentity:
    """TRUSTED_LAN identity: HMAC over a SecretRef-backed shared credential."""

    profile = IdentityProfile.HMAC_SHARED_SECRET

    def __init__(self, credential: str) -> None:
        self._credential = credential

    def available(self) -> bool:
        return bool(self._credential)

    def prove(self, node_id: str, nonce: str) -> str:
        return identity_proof(self._credential, node_id, nonce)

    def verify(self, node_id: str, nonce: str, proof: object) -> bool:
        return verify(identity_proof(self._credential, node_id, nonce), proof)


class AsymmetricIdentity:
    """Hardened/remote identity (Ed25519 signatures or mTLS client certificates).

    Design only in V1. Implementations belong in an optional security extension
    (e.g. ``pados[node-security]`` bringing ``cryptography``), never as a
    mandatory Core dependency. Until then it is unavailable and fails closed.
    """

    profile = IdentityProfile.ASYMMETRIC_IDENTITY

    def available(self) -> bool:
        return False

    def prove(self, node_id: str, nonce: str) -> str:
        raise PermissionError("ASYMMETRIC_IDENTITY_NOT_AVAILABLE")

    def verify(self, node_id: str, nonce: str, proof: object) -> bool:
        return False


def assert_profile_allowed(security: SecurityProfile, identity: IdentityProfile) -> None:
    """Fail closed when a deployment profile would rely on an insufficient identity."""
    if identity not in ACCEPTED_IDENTITY[security]:
        raise PermissionError(f"IDENTITY_PROFILE_{identity.value}_NOT_ALLOWED_FOR_{security.value}")
