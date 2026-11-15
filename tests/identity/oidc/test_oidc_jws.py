# SPDX-License-Identifier: AGPL-3.0-only
"""Pure-Python RS256 and compact JWS parsing (design §4): algorithm pinning, exact EMSA-PKCS1-v1_5 comparison and
length checks, against TEST ONLY keys generated in-process."""
from __future__ import annotations

import hashlib
import json

import pytest

from pados.identity.oidc.errors import OidcFailure
from pados.identity.oidc.jws import (
    SHA256_DIGEST_INFO_PREFIX,
    RsaPublicKey,
    b64url_decode,
    parse_compact,
    rsa_key_from_jwk,
    verify_rs256,
)
from tests.identity.oidc.oidc_support import b64u, compact_jws, test_rsa_key

KEY = test_rsa_key(1, "kid-primary")
PUBLIC = RsaPublicKey(kid=KEY.kid, modulus=KEY.n, exponent=KEY.e)
MESSAGE = b"header.payload"
SIZE = 256


def _reason(action: object) -> str:
    with pytest.raises(OidcFailure) as caught:
        action()  # type: ignore[operator]
    assert caught.value.code == "OIDC_TOKEN_INVALID"
    return caught.value.reason


def _raw_sign(encoded: bytes) -> bytes:
    return pow(int.from_bytes(encoded, "big"), KEY.d, KEY.n).to_bytes(SIZE, "big")


def test_a_genuine_signature_verifies_and_any_change_does_not() -> None:
    signature = KEY.sign(MESSAGE)
    assert verify_rs256(PUBLIC, MESSAGE, signature)
    assert not verify_rs256(PUBLIC, MESSAGE + b"x", signature)
    flipped = bytes([signature[0] ^ 1]) + signature[1:]
    assert not verify_rs256(PUBLIC, MESSAGE, flipped)
    assert not verify_rs256(RsaPublicKey("other", test_rsa_key(9, "x").n, 65537), MESSAGE, signature)


def test_length_and_range_checks() -> None:
    signature = KEY.sign(MESSAGE)
    assert not verify_rs256(PUBLIC, MESSAGE, signature[1:])  # one byte short
    assert not verify_rs256(PUBLIC, MESSAGE, b"\x00" + signature)  # one byte long
    assert not verify_rs256(PUBLIC, MESSAGE, KEY.n.to_bytes(SIZE, "big"))  # s == n
    assert not verify_rs256(PUBLIC, MESSAGE, b"\x00" * SIZE)  # s == 0
    assert not verify_rs256(PUBLIC, MESSAGE, b"")
    small = RsaPublicKey("small", (1 << 1023) + 1155, 65537)
    assert not verify_rs256(small, MESSAGE, b"\x01" * 128)  # a modulus under 2048 bits is never used


@pytest.mark.parametrize("encoding", [
    # DigestInfo without the NULL parameters (a lenient parser would accept it)
    lambda d: b"\x00\x01" + b"\xff" * (SIZE - 51 - 3 + 2) + b"\x00"
    + bytes.fromhex("302f300b0609608648016503040201") + b"\x04\x20" + d,
    # garbage after the digest, padding shortened to keep the length
    lambda d: b"\x00\x01" + b"\xff" * (SIZE - 51 - 3 - 4) + b"\x00" + SHA256_DIGEST_INFO_PREFIX + d + b"junk",
    # padding with a non-0xff byte
    lambda d: b"\x00\x01" + b"\xff" * 100 + b"\xfe" + b"\xff" * (SIZE - 51 - 3 - 101) + b"\x00"
    + SHA256_DIGEST_INFO_PREFIX + d,
    # block type 2 instead of 1
    lambda d: b"\x00\x02" + b"\xff" * (SIZE - 51 - 3) + b"\x00" + SHA256_DIGEST_INFO_PREFIX + d,
])
def test_only_the_exact_emsa_encoding_verifies(encoding: object) -> None:
    digest = hashlib.sha256(MESSAGE).digest()
    encoded = encoding(digest)  # type: ignore[operator]
    assert len(encoded) == SIZE
    assert not verify_rs256(PUBLIC, MESSAGE, _raw_sign(encoded))


def _token(header: dict[str, object], claims: dict[str, object] | None = None) -> str:
    return compact_jws(header, claims or {"sub": "s"}, KEY)


@pytest.mark.parametrize(("header", "reason"), [
    ({"alg": "none", "kid": "kid-primary"}, "algorithm"),
    ({"alg": "HS256", "kid": "kid-primary"}, "algorithm"),
    ({"alg": "HS512", "kid": "kid-primary"}, "algorithm"),
    ({"alg": "ES256", "kid": "kid-primary"}, "algorithm"),
    ({"alg": "PS256", "kid": "kid-primary"}, "algorithm"),
    ({"alg": "rs256", "kid": "kid-primary"}, "algorithm"),
    ({"kid": "kid-primary"}, "algorithm"),
    ({"alg": "RS256"}, "kid"),
    ({"alg": "RS256", "kid": ""}, "kid"),
    ({"alg": "RS256", "kid": "kid-primary", "jwk": {"kty": "RSA"}}, "header"),
    ({"alg": "RS256", "kid": "kid-primary", "jku": "https://attacker.example/keys"}, "header"),
    ({"alg": "RS256", "kid": "kid-primary", "x5u": "https://attacker.example/cert"}, "header"),
    ({"alg": "RS256", "kid": "kid-primary", "crit": ["exp"]}, "header"),
])
def test_headers_are_pinned(header: dict[str, object], reason: str) -> None:
    assert _reason(lambda: parse_compact(_token(header))) == reason


def test_compact_shape_and_encoding() -> None:
    good = _token({"alg": "RS256", "kid": "kid-primary"})
    assert parse_compact(good).kid == "kid-primary"
    head, body, sig = good.split(".")
    assert _reason(lambda: parse_compact(f"{head}.{body}")) == "shape"
    assert _reason(lambda: parse_compact(f"{head}.{body}.")) == "shape"  # an empty signature
    assert _reason(lambda: parse_compact(f"{head}.{body}.{sig}.x")) == "shape"
    assert _reason(lambda: parse_compact(f"{head}=.{body}.{sig}")) == "header"  # padding is not base64url
    assert _reason(lambda: parse_compact(f"{head}.{body}+.{sig}")) == "payload"
    assert _reason(lambda: parse_compact("a" * 20000)) == "shape"
    array_payload = b64u(json.dumps(["not", "an", "object"]).encode())
    assert _reason(lambda: parse_compact(f"{head}.{array_payload}.{sig}")) == "payload"
    assert _reason(lambda: b64url_decode("abcde")) == "encoding"  # impossible length


def test_jwk_selection_and_bounds() -> None:
    jwk = KEY.jwk()
    parsed = rsa_key_from_jwk(jwk)
    assert parsed == PUBLIC
    assert rsa_key_from_jwk({**jwk, "kty": "EC"}) is None
    assert rsa_key_from_jwk({**jwk, "use": "enc"}) is None
    assert rsa_key_from_jwk({**jwk, "alg": "RS512"}) is None
    assert rsa_key_from_jwk({k: v for k, v in jwk.items() if k != "kid"}) is None
    small = {**jwk, "n": b64u(((1 << 1023) + 1155).to_bytes(128, "big"))}
    assert _reason(lambda: rsa_key_from_jwk(small)) == "jwk_size"
    assert _reason(lambda: rsa_key_from_jwk({**jwk, "e": b64u(b"\x02")})) == "jwk"
    padded = b64u(bytes(1) + KEY.n.to_bytes(SIZE, "big"))
    assert _reason(lambda: rsa_key_from_jwk({**jwk, "n": padded})) == "jwk"  # a leading zero octet
