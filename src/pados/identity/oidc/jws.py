# SPDX-License-Identifier: AGPL-3.0-only
"""Compact JWS parsing and RS256 verification in pure Python (standard library only).

* The algorithm is pinned to ``RS256`` (RSASSA-PKCS1-v1_5 with SHA-256). ``none``, every ``HS*`` (which would turn a
  public key into an HMAC key), ``ES*`` and ``PS*`` are refused before any key is looked at.
* A header naming a key or a key location (``jwk``, ``jku``, ``x5u``, ``x5c``) or carrying ``crit`` is refused: keys
  only ever come from the provider's JWKS fetched over HTTPS.
* Verification is ``pow(s, e, n)`` followed by an exact comparison with the full EMSA-PKCS1-v1_5 encoding (the DER
  ``DigestInfo`` prefix for SHA-256 included), in constant time, after length checks. Nothing is parsed out of the
  decoded block, so there is no lenient-padding or trailing-garbage acceptance.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from pados.identity.errors import OIDC_TOKEN_INVALID
from pados.identity.oidc.errors import OidcFailure

ALGORITHM = "RS256"
MAX_COMPACT_LENGTH = 16 * 1024
MIN_MODULUS_BITS = 2048
MAX_MODULUS_BITS = 8192
MAX_EXPONENT = 2**32  # a provider key with a huge exponent would make pow() slow; real keys use 65537
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")
_FORBIDDEN_HEADERS = frozenset({"jwk", "jku", "x5u", "x5c", "crit"})
# DER of DigestInfo{ AlgorithmIdentifier{ id-sha256, NULL }, OCTET STRING(32) } (RFC 8017 §9.2, note 1).
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _invalid(reason: str) -> OidcFailure:
    return OidcFailure(OIDC_TOKEN_INVALID, reason)


def b64url_decode(value: str, *, reason: str = "encoding") -> bytes:
    """Unpadded base64url (RFC 7515 §2); padding, whitespace and other alphabets are refused."""
    if not _B64URL.fullmatch(value) or len(value) % 4 == 1:
        raise _invalid(reason)
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _invalid(reason) from exc


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_object(raw: bytes, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid(reason) from exc
    if not isinstance(value, dict):
        raise _invalid(reason)
    return value


@dataclass(frozen=True)
class CompactJws:
    header: dict[str, Any]
    claims: dict[str, Any]
    signing_input: bytes
    signature: bytes

    @property
    def kid(self) -> str:
        return str(self.header["kid"])


def parse_compact(compact: str) -> CompactJws:
    """Split and decode a compact JWS whose header pins RS256 and names a ``kid``; nothing is verified yet."""
    if not isinstance(compact, str) or not compact or len(compact) > MAX_COMPACT_LENGTH or not compact.isascii():
        raise _invalid("shape")
    parts = compact.split(".")
    if len(parts) != 3 or not all(parts):
        raise _invalid("shape")
    header = _json_object(b64url_decode(parts[0], reason="header"), "header")
    if header.get("alg") != ALGORITHM:
        raise _invalid("algorithm")
    if _FORBIDDEN_HEADERS & header.keys():
        raise _invalid("header")
    kid = header.get("kid")
    if not isinstance(kid, str) or not 1 <= len(kid) <= 256:
        raise _invalid("kid")
    claims = _json_object(b64url_decode(parts[1], reason="payload"), "payload")
    signature = b64url_decode(parts[2], reason="signature")
    return CompactJws(header=header, claims=claims, signing_input=f"{parts[0]}.{parts[1]}".encode("ascii"),
                      signature=signature)


@dataclass(frozen=True)
class RsaPublicKey:
    kid: str
    modulus: int
    exponent: int


def _b64url_uint(value: object, reason: str) -> int:
    if not isinstance(value, str) or not value:
        raise _invalid(reason)
    raw = b64url_decode(value, reason=reason)
    if not raw or raw[0] == 0:  # RFC 7518 §6.3.1: minimal big-endian, no leading zero octet
        raise _invalid(reason)
    return int.from_bytes(raw, "big")


def rsa_key_from_jwk(jwk: dict[str, Any]) -> RsaPublicKey | None:
    """An RS256 signing key from one JWKS entry; ``None`` for an entry that is not one (other kty, use or alg)."""
    if jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig" or jwk.get("alg", ALGORITHM) != ALGORITHM:
        return None
    kid = jwk.get("kid")
    if not isinstance(kid, str) or not kid:
        return None
    modulus = _b64url_uint(jwk.get("n"), "jwk")
    exponent = _b64url_uint(jwk.get("e"), "jwk")
    if not MIN_MODULUS_BITS <= modulus.bit_length() <= MAX_MODULUS_BITS:
        raise _invalid("jwk_size")
    if exponent < 3 or exponent % 2 == 0 or exponent >= min(modulus, MAX_EXPONENT):
        raise _invalid("jwk")
    return RsaPublicKey(kid=kid, modulus=modulus, exponent=exponent)


def emsa_pkcs1_v15_sha256(message: bytes, size: int) -> bytes | None:
    """EM = 0x00 || 0x01 || PS (0xff, at least 8) || 0x00 || DigestInfo (RFC 8017 §9.2); ``None`` if too short."""
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return None
    return b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info


def verify_rs256(key: RsaPublicKey, message: bytes, signature: bytes) -> bool:
    size = (key.modulus.bit_length() + 7) // 8
    if key.modulus.bit_length() < MIN_MODULUS_BITS or len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if not 0 < value < key.modulus:
        return False
    expected = emsa_pkcs1_v15_sha256(message, size)
    if expected is None:
        return False
    encoded = pow(value, key.exponent, key.modulus).to_bytes(size, "big")
    return hmac.compare_digest(encoded, expected)
