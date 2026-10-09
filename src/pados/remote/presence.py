# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh human presence for consequential approvals (WebAuthn, user verification required).

The private credential never leaves the authenticator. PADOS stores only public
keys, registered on the existing remote actor rows (``presence_credentials``), so
the actor registry stays the single identity authority. Verification is
deliberately self-contained (standard library only):

* a bounded CBOR decoder for attestation objects and COSE keys;
* ES256 (ECDSA P-256 / SHA-256) and RS256 (RSASSA-PKCS1-v1_5 / SHA-256) signature
  verification over public data;
* ``authenticatorData`` / ``clientDataJSON`` checks: exact challenge, allowed
  origin, RP ID hash, user-present AND user-verified flags.

There is no bypass mode. Tests drive this same verifier with a software
authenticator that signs real assertions.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ES256 = -7
RS256 = -257
SUPPORTED_ALGORITHMS = frozenset({ES256, RS256})
_FLAG_USER_PRESENT = 0x01
_FLAG_USER_VERIFIED = 0x04
_FLAG_ATTESTED_DATA = 0x40
_FLAG_EXTENSIONS = 0x80
_MAX_CLIENT_DATA = 4096
_MAX_AUTH_DATA = 4096
_MAX_SIGNATURE = 1024
_MAX_CREDENTIAL_ID = 1023
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_RP_ID = re.compile(r"^(localhost|[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+)$")


class PresenceError(ValueError):
    """Presence verification refused; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: object, *, code: str = "PRESENCE_ENCODING_INVALID", limit: int = 8192) -> bytes:
    if not isinstance(value, str) or not value or len(value) > limit or not _B64URL.fullmatch(value):
        raise PresenceError(code)
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise PresenceError(code) from exc


# ---------------------------------------------------------------------------
# Bounded CBOR (RFC 8949) subset: definite lengths only, no tags, no floats.
# ---------------------------------------------------------------------------
class _Cbor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.items = 0

    def _take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise PresenceError("CBOR_TRUNCATED")
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def _argument(self, info: int) -> int:
        if info < 24:
            return info
        if info == 24:
            return self._take(1)[0]
        if info == 25:
            return int(struct.unpack(">H", self._take(2))[0])
        if info == 26:
            return int(struct.unpack(">I", self._take(4))[0])
        if info == 27:
            return int(struct.unpack(">Q", self._take(8))[0])
        raise PresenceError("CBOR_INDEFINITE_UNSUPPORTED")

    def item(self, depth: int = 0) -> Any:
        if depth > 8:
            raise PresenceError("CBOR_TOO_DEEP")
        self.items += 1
        if self.items > 512:
            raise PresenceError("CBOR_TOO_LARGE")
        initial = self._take(1)[0]
        major, info = initial >> 5, initial & 0x1F
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info == 22:
                return None
            raise PresenceError("CBOR_SIMPLE_UNSUPPORTED")
        value = self._argument(info)
        if major == 0:
            return value
        if major == 1:
            return -1 - value
        if major == 2:
            return self._take(value)
        if major == 3:
            try:
                return self._take(value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PresenceError("CBOR_TEXT_INVALID") from exc
        if major == 4:
            if value > 64:
                raise PresenceError("CBOR_TOO_LARGE")
            return [self.item(depth + 1) for _ in range(value)]
        if major == 5:
            if value > 64:
                raise PresenceError("CBOR_TOO_LARGE")
            out: dict[Any, Any] = {}
            for _ in range(value):
                key = self.item(depth + 1)
                if not isinstance(key, (int, str)) or key in out:
                    raise PresenceError("CBOR_MAP_KEY_INVALID")
                out[key] = self.item(depth + 1)
            return out
        raise PresenceError("CBOR_TAG_UNSUPPORTED")


def cbor_decode_prefix(data: bytes) -> tuple[Any, int]:
    """Decode one CBOR item from the start of ``data``; return it and its length."""
    reader = _Cbor(data)
    return reader.item(), reader.pos


def cbor_decode(data: bytes) -> Any:
    value, used = cbor_decode_prefix(data)
    if used != len(data):
        raise PresenceError("CBOR_TRAILING_BYTES")
    return value


# ---------------------------------------------------------------------------
# Signature verification over public data.
# ---------------------------------------------------------------------------
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_Jacobian = tuple[int, int, int]


def _on_curve(x: int, y: int) -> bool:
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _double(p: _Jacobian) -> _Jacobian:
    x, y, z = p
    if y == 0 or z == 0:
        return (0, 1, 0)
    ysq = y * y % _P
    s = 4 * x * ysq % _P
    zsq = z * z % _P
    m = 3 * (x - zsq) * (x + zsq) % _P  # a = -3
    nx = (m * m - 2 * s) % _P
    ny = (m * (s - nx) - 8 * ysq * ysq) % _P
    nz = 2 * y * z % _P
    return (nx, ny, nz)


def _add(p: _Jacobian, q: _Jacobian) -> _Jacobian:
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1sq, z2sq = z1 * z1 % _P, z2 * z2 % _P
    u1, u2 = x1 * z2sq % _P, x2 * z1sq % _P
    s1, s2 = y1 * z2sq * z2 % _P, y2 * z1sq * z1 % _P
    if u1 == u2:
        return _double(p) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % _P, (s2 - s1) % _P
    hsq = h * h % _P
    hcu = hsq * h % _P
    nx = (r * r - hcu - 2 * u1 * hsq) % _P
    ny = (r * (u1 * hsq - nx) - s1 * hcu) % _P
    nz = h * z1 * z2 % _P
    return (nx, ny, nz)


def _multiply(k: int, point: tuple[int, int]) -> _Jacobian:
    result: _Jacobian = (0, 1, 0)
    addend: _Jacobian = (point[0], point[1], 1)
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _double(addend)
        k >>= 1
    return result


def _affine_x(p: _Jacobian) -> int | None:
    if p[2] == 0:
        return None
    return p[0] * pow(p[2] * p[2] % _P, -1, _P) % _P


def _der_ecdsa_signature(signature: bytes) -> tuple[int, int]:
    """Parse a strict DER ``SEQUENCE { INTEGER r, INTEGER s }``."""
    def integer(data: bytes, offset: int) -> tuple[int, int]:
        if offset + 2 > len(data) or data[offset] != 0x02:
            raise PresenceError("SIGNATURE_ENCODING_INVALID")
        length = data[offset + 1]
        start = offset + 2
        if length == 0 or length > 33 or start + length > len(data):
            raise PresenceError("SIGNATURE_ENCODING_INVALID")
        body = data[start : start + length]
        if body[0] & 0x80 or (length > 1 and body[0] == 0 and not body[1] & 0x80):
            raise PresenceError("SIGNATURE_ENCODING_INVALID")
        return int.from_bytes(body, "big"), start + length

    if len(signature) < 8 or signature[0] != 0x30 or signature[1] != len(signature) - 2:
        raise PresenceError("SIGNATURE_ENCODING_INVALID")
    r, offset = integer(signature, 2)
    s, offset = integer(signature, offset)
    if offset != len(signature):
        raise PresenceError("SIGNATURE_ENCODING_INVALID")
    return r, s


def verify_es256(x: int, y: int, message: bytes, signature: bytes) -> bool:
    if not _on_curve(x, y):
        return False
    try:
        r, s = _der_ecdsa_signature(signature)
    except PresenceError:
        return False
    if not (1 <= r < _N and 1 <= s < _N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, _N)
    point = _add(_multiply(e * w % _N, _G), _multiply(r * w % _N, (x, y)))
    affine = _affine_x(point)
    return affine is not None and affine % _N == r


_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_rs256(modulus: int, exponent: int, message: bytes, signature: bytes) -> bool:
    size = (modulus.bit_length() + 7) // 8
    if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0 or len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= modulus:
        return False
    encoded = pow(value, exponent, modulus).to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


# ---------------------------------------------------------------------------
# Registered credentials (public keys only).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PresenceCredential:
    credential_id: str  # base64url
    algorithm: int
    public_key_cose: str  # base64url COSE_Key
    label: str = ""

    def raw_id(self) -> bytes:
        return b64url_decode(self.credential_id, code="PRESENCE_CREDENTIAL_INVALID")

    def verify(self, message: bytes, signature: bytes) -> bool:
        key = parse_cose_key(b64url_decode(self.public_key_cose, code="PRESENCE_CREDENTIAL_INVALID"))
        if key["alg"] != self.algorithm:
            return False
        if self.algorithm == ES256:
            return verify_es256(key["x"], key["y"], message, signature)
        return verify_rs256(key["n"], key["e"], message, signature)

    def to_dict(self) -> dict[str, Any]:
        return {"credential_id": self.credential_id, "algorithm": self.algorithm,
                "public_key_cose": self.public_key_cose, "label": self.label}


_CREDENTIAL_FIELDS = frozenset({"credential_id", "algorithm", "public_key_cose", "label"})


def parse_presence_credential(row: object) -> PresenceCredential:
    """Closed-schema parse of one registered credential (public material only)."""
    if not isinstance(row, Mapping) or set(row) - _CREDENTIAL_FIELDS:
        raise ValueError("REMOTE_PRESENCE_CREDENTIAL_INVALID")
    credential_id = row.get("credential_id")
    algorithm = row.get("algorithm")
    cose = row.get("public_key_cose")
    label = row.get("label", "")
    if not isinstance(label, str) or len(label) > 80:
        raise ValueError("REMOTE_PRESENCE_CREDENTIAL_INVALID")
    if isinstance(algorithm, bool) or algorithm not in SUPPORTED_ALGORITHMS:
        raise ValueError("REMOTE_PRESENCE_CREDENTIAL_ALGORITHM_UNSUPPORTED")
    try:
        raw_id = b64url_decode(credential_id, code="REMOTE_PRESENCE_CREDENTIAL_INVALID")
        key = parse_cose_key(b64url_decode(cose, code="REMOTE_PRESENCE_CREDENTIAL_INVALID", limit=4096))
    except PresenceError as exc:
        raise ValueError("REMOTE_PRESENCE_CREDENTIAL_INVALID") from exc
    if not raw_id or len(raw_id) > _MAX_CREDENTIAL_ID or key["alg"] != algorithm:
        raise ValueError("REMOTE_PRESENCE_CREDENTIAL_INVALID")
    return PresenceCredential(str(credential_id), int(algorithm), str(cose), label)


def parse_cose_key(data: bytes) -> dict[str, int]:
    key = cbor_decode(data)
    if not isinstance(key, dict):
        raise PresenceError("COSE_KEY_INVALID")
    kty, alg = key.get(1), key.get(3)
    if kty == 2 and alg == ES256:
        x, y = key.get(-2), key.get(-3)
        if key.get(-1) != 1 or not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
            raise PresenceError("COSE_KEY_INVALID")
        px, py = int.from_bytes(x, "big"), int.from_bytes(y, "big")
        if not _on_curve(px, py):
            raise PresenceError("COSE_KEY_NOT_ON_CURVE")
        return {"alg": ES256, "x": px, "y": py}
    if kty == 3 and alg == RS256:
        n, e = key.get(-1), key.get(-2)
        if not isinstance(n, bytes) or not isinstance(e, bytes) or not n or not e or len(e) > 8:
            raise PresenceError("COSE_KEY_INVALID")
        modulus, exponent = int.from_bytes(n, "big"), int.from_bytes(e, "big")
        if modulus.bit_length() < 2048 or modulus.bit_length() > 8192:
            raise PresenceError("COSE_KEY_INVALID")
        return {"alg": RS256, "n": modulus, "e": exponent}
    raise PresenceError("COSE_KEY_UNSUPPORTED")


# ---------------------------------------------------------------------------
# Ceremonies.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PresenceVerification:
    credential_id: str
    sign_count: int
    user_verified: bool
    method: str = "WEBAUTHN_USER_VERIFICATION"


def _client_data(raw: bytes, *, expected_type: str, challenge: bytes, origins: frozenset[str]) -> None:
    if len(raw) > _MAX_CLIENT_DATA:
        raise PresenceError("CLIENT_DATA_TOO_LARGE")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PresenceError("CLIENT_DATA_INVALID") from exc
    if not isinstance(value, dict) or value.get("type") != expected_type:
        raise PresenceError("CLIENT_DATA_TYPE_MISMATCH")
    presented = value.get("challenge")
    if not isinstance(presented, str) or not hmac.compare_digest(presented, b64url_encode(challenge)):
        raise PresenceError("CHALLENGE_MISMATCH")
    if value.get("origin") not in origins:
        raise PresenceError("ORIGIN_NOT_ALLOWED")
    if value.get("crossOrigin") is True:
        raise PresenceError("CROSS_ORIGIN_FORBIDDEN")


def _auth_data(raw: bytes, *, rp_id: str) -> tuple[int, int]:
    if len(raw) < 37 or len(raw) > _MAX_AUTH_DATA:
        raise PresenceError("AUTHENTICATOR_DATA_INVALID")
    if not hmac.compare_digest(raw[:32], hashlib.sha256(rp_id.encode("ascii")).digest()):
        raise PresenceError("RP_ID_MISMATCH")
    flags = raw[32]
    if not flags & _FLAG_USER_PRESENT:
        raise PresenceError("USER_PRESENCE_REQUIRED")
    if not flags & _FLAG_USER_VERIFIED:
        raise PresenceError("USER_VERIFICATION_REQUIRED")
    return flags, int(struct.unpack(">I", raw[33:37])[0])


class WebAuthnPresenceVerifier:
    """Production presence verifier. Requires UV; exact challenge, origin and RP ID."""

    def __init__(self, *, rp_id: str, origins: Iterable[str]) -> None:
        if not isinstance(rp_id, str) or not _RP_ID.fullmatch(rp_id):
            raise ValueError("REMOTE_PRESENCE_RP_ID_INVALID")
        allowed = frozenset(origins)
        if not allowed or any(not isinstance(item, str) or not item for item in allowed):
            raise ValueError("REMOTE_PRESENCE_ORIGINS_REQUIRED")
        self.rp_id = rp_id
        self.origins = allowed

    def request_options(self, challenge: bytes, credentials: Iterable[PresenceCredential],
                        *, timeout_ms: int) -> dict[str, Any]:
        return {
            "challenge": b64url_encode(challenge),
            "rpId": self.rp_id,
            "timeout": timeout_ms,
            "userVerification": "required",
            "allowCredentials": [{"type": "public-key", "id": item.credential_id} for item in credentials],
        }

    def verify_assertion(
        self,
        assertion: Mapping[str, Any],
        *,
        challenge: bytes,
        credentials: Iterable[PresenceCredential],
        user_handle: bytes | None = None,
    ) -> PresenceVerification:
        if not isinstance(assertion, Mapping) or set(assertion) - {
            "credential_id", "client_data_json", "authenticator_data", "signature", "user_handle"
        }:
            raise PresenceError("ASSERTION_SHAPE_INVALID")
        credential_id = assertion.get("credential_id")
        known = {item.credential_id: item for item in credentials}
        credential = known.get(credential_id) if isinstance(credential_id, str) else None
        if credential is None:
            raise PresenceError("CREDENTIAL_NOT_REGISTERED")
        client_data = b64url_decode(assertion.get("client_data_json"), code="CLIENT_DATA_INVALID")
        auth_data = b64url_decode(assertion.get("authenticator_data"), code="AUTHENTICATOR_DATA_INVALID")
        signature = b64url_decode(assertion.get("signature"), code="SIGNATURE_ENCODING_INVALID",
                                  limit=_MAX_SIGNATURE * 2)
        _client_data(client_data, expected_type="webauthn.get", challenge=challenge, origins=self.origins)
        _flags, sign_count = _auth_data(auth_data, rp_id=self.rp_id)
        handle = assertion.get("user_handle")
        if handle is not None and user_handle is not None and (
            not hmac.compare_digest(b64url_decode(handle, code="USER_HANDLE_INVALID"), user_handle)
        ):
            raise PresenceError("USER_HANDLE_MISMATCH")
        signed = auth_data + hashlib.sha256(client_data).digest()
        if not credential.verify(signed, signature):
            raise PresenceError("SIGNATURE_INVALID")
        return PresenceVerification(credential_id=credential.credential_id, sign_count=sign_count,
                                    user_verified=True)

    def registration_options(self, challenge: bytes, *, actor_id: str, user_handle: bytes) -> dict[str, Any]:
        return {
            "challenge": b64url_encode(challenge),
            "rp": {"id": self.rp_id, "name": "PADOS Home Base"},
            "user": {"id": b64url_encode(user_handle), "name": actor_id, "displayName": actor_id},
            "pubKeyCredParams": [{"type": "public-key", "alg": ES256}, {"type": "public-key", "alg": RS256}],
            "authenticatorSelection": {"userVerification": "required", "residentKey": "preferred"},
            "attestation": "none",
            "timeout": 120_000,
        }

    def enrollment_descriptor(
        self, registration: Mapping[str, Any], *, challenge: bytes, label: str
    ) -> dict[str, Any]:
        """Verify a registration response and return the public descriptor.

        This changes nothing: the operator adds the descriptor to the actor's
        ``presence_credentials`` in the remote configuration, so enrolment stays
        a deliberate act on the existing identity authority.
        """
        if not isinstance(registration, Mapping) or set(registration) - {"client_data_json", "attestation_object"}:
            raise PresenceError("REGISTRATION_SHAPE_INVALID")
        client_data = b64url_decode(registration.get("client_data_json"), code="CLIENT_DATA_INVALID")
        attestation = cbor_decode(b64url_decode(registration.get("attestation_object"),
                                                code="ATTESTATION_INVALID", limit=16384))
        _client_data(client_data, expected_type="webauthn.create", challenge=challenge, origins=self.origins)
        if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
            raise PresenceError("ATTESTATION_INVALID")
        auth_data: bytes = attestation["authData"]
        flags, _count = _auth_data(auth_data, rp_id=self.rp_id)
        if not flags & _FLAG_ATTESTED_DATA or len(auth_data) < 55:
            raise PresenceError("ATTESTED_CREDENTIAL_MISSING")
        id_length = int(struct.unpack(">H", auth_data[53:55])[0])
        if id_length == 0 or id_length > _MAX_CREDENTIAL_ID or 55 + id_length > len(auth_data):
            raise PresenceError("ATTESTED_CREDENTIAL_INVALID")
        credential_id = auth_data[55 : 55 + id_length]
        key_bytes = auth_data[55 + id_length :]
        _key, used = cbor_decode_prefix(key_bytes)
        if used != len(key_bytes) and not flags & _FLAG_EXTENSIONS:
            raise PresenceError("ATTESTED_CREDENTIAL_TRAILING_BYTES")
        cose = key_bytes[:used]
        parsed = parse_cose_key(cose)
        descriptor = {"credential_id": b64url_encode(credential_id), "algorithm": parsed["alg"],
                      "public_key_cose": b64url_encode(cose), "label": label[:80]}
        parse_presence_credential(descriptor)
        # Attestation statements are not verified: the UV flag is the authenticator's own claim.
        # Enrolment therefore stays an operator decision confirmed out of band.
        return {**descriptor, "attestation_format": str(attestation.get("fmt", "none"))[:32],
                "attestation_verified": False,
                "status": "DESCRIPTOR_ONLY_NOT_ENROLLED_CONFIRM_DEVICE_OUT_OF_BAND"}


def user_handle_for(actor_id: str) -> bytes:
    """Stable, non-secret WebAuthn user handle for a registered actor."""
    return hashlib.sha256(f"pados-remote-actor:{actor_id}".encode()).digest()
