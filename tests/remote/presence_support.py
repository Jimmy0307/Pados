"""Test-only software WebAuthn authenticator.

It holds a real P-256 private key and produces genuine ES256 assertions, so the
tests drive the *production* ``WebAuthnPresenceVerifier`` end to end. Nothing in
``src/`` imports this module; there is no verifier bypass anywhere.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import struct
from typing import Any

from pados.remote import presence as webauthn

RP_ID = "localhost"
ORIGIN = "http://localhost:5173"


def cbor(value: Any) -> bytes:
    def head(major: int, length: int) -> bytes:
        if length < 24:
            return bytes([major << 5 | length])
        if length < 256:
            return bytes([major << 5 | 24, length])
        if length < 65536:
            return bytes([major << 5 | 25]) + struct.pack(">H", length)
        return bytes([major << 5 | 26]) + struct.pack(">I", length)

    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        data = value.encode("utf-8")
        return head(3, len(data)) + data
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(cbor(k) + cbor(v) for k, v in value.items())
    raise TypeError(type(value))


def _der_int(value: int) -> bytes:
    body = value.to_bytes((value.bit_length() + 8) // 8 or 1, "big")
    return b"\x02" + bytes([len(body)]) + body


class SoftwareAuthenticator:
    def __init__(self, *, rp_id: str = RP_ID, origin: str = ORIGIN, actor_id: str = "owner") -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.actor_id = actor_id
        self.private_key = secrets.randbelow(webauthn._N - 1) + 1
        point = webauthn._multiply(self.private_key, webauthn._G)
        z_inv = pow(point[2], -1, webauthn._P)
        self.x = point[0] * z_inv * z_inv % webauthn._P
        self.y = point[1] * z_inv * z_inv * z_inv % webauthn._P
        self.credential_id = secrets.token_bytes(16)
        self.sign_count = 0

    @property
    def cose_key(self) -> bytes:
        return cbor({1: 2, 3: -7, -1: 1, -2: self.x.to_bytes(32, "big"), -3: self.y.to_bytes(32, "big")})

    def descriptor(self, label: str = "test-authenticator") -> dict[str, Any]:
        return {"credential_id": webauthn.b64url_encode(self.credential_id), "algorithm": -7,
                "public_key_cose": webauthn.b64url_encode(self.cose_key), "label": label}

    def sign(self, message: bytes) -> bytes:
        e = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(webauthn._N - 1) + 1
            point = webauthn._multiply(k, webauthn._G)
            x = webauthn._affine_x(point)
            assert x is not None
            r = x % webauthn._N
            s = pow(k, -1, webauthn._N) * (e + r * self.private_key) % webauthn._N
            if r and s:
                body = _der_int(r) + _der_int(s)
                return b"\x30" + bytes([len(body)]) + body

    def _auth_data(self, *, rp_id: str, flags: int, extra: bytes = b"") -> bytes:
        self.sign_count += 1
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.sign_count) + extra

    def assertion(
        self,
        public_key: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        user_verified: bool = True,
        user_present: bool = True,
        challenge: str | None = None,
        tamper_signature: bool = False,
    ) -> dict[str, Any]:
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": challenge or public_key["challenge"],
             "origin": origin or self.origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()
        flags = (0x01 if user_present else 0) | (0x04 if user_verified else 0)
        auth_data = self._auth_data(rp_id=rp_id or public_key.get("rpId", self.rp_id), flags=flags)
        signature = self.sign(auth_data + hashlib.sha256(client_data).digest())
        if tamper_signature:
            signature = self.sign(auth_data + b"tampered")
        return {
            "credential_id": webauthn.b64url_encode(self.credential_id),
            "client_data_json": webauthn.b64url_encode(client_data),
            "authenticator_data": webauthn.b64url_encode(auth_data),
            "signature": webauthn.b64url_encode(signature),
            "user_handle": webauthn.b64url_encode(webauthn.user_handle_for(self.actor_id)),
        }

    def registration(self, public_key: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": public_key["challenge"], "origin": origin or self.origin},
            separators=(",", ":"),
        ).encode()
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self.cose_key
        auth_data = self._auth_data(rp_id=public_key["rp"]["id"], flags=0x01 | 0x04 | 0x40, extra=attested)
        attestation = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"client_data_json": webauthn.b64url_encode(client_data),
                "attestation_object": webauthn.b64url_encode(attestation)}


def verifier(origin: str = ORIGIN, rp_id: str = RP_ID) -> webauthn.WebAuthnPresenceVerifier:
    return webauthn.WebAuthnPresenceVerifier(rp_id=rp_id, origins=[origin])
