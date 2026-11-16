# SPDX-License-Identifier: AGPL-3.0-only
"""Secret primitives: pairing codes, session ids, digests, the derived CSRF token and the control-token binding.

Nothing here logs. Callers store only ``digest(...)`` of a code or a session id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata

# Crockford base32: no I, L, O or U. 10 characters = 50 bits.
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 10
_CROCKFORD_ALIASES = str.maketrans({"I": "1", "L": "1", "O": "0"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
DEFAULT_DEVICE_LABEL = "Browser"


def new_pairing_code() -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(CODE_LENGTH))


def display_code(code: str) -> str:
    return f"{code[:5]}-{code[5:]}"


def normalize_code(presented: str) -> str | None:
    """The canonical code for operator input; ``None`` if malformed.

    Every non-alphanumeric ASCII character (the display dash, spaces, dots) is dropped, case is folded and the
    Crockford aliases I/L -> 1 and O -> 0 are applied before the code is hashed.
    """
    candidate = "".join(ch for ch in presented.upper() if ch.isascii() and ch.isalnum()).translate(
        _CROCKFORD_ALIASES)
    if len(candidate) != CODE_LENGTH or any(ch not in CROCKFORD for ch in candidate):
        return None
    return candidate


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_id_shape_ok(value: str) -> bool:
    return bool(_SESSION_ID.fullmatch(value))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(server_key: bytes, session_id: str) -> str:
    mac = hmac.new(server_key, b"csrf:" + session_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def csrf_matches(server_key: bytes, session_id: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(server_key, session_id).encode("ascii"),
                               presented.encode("utf-8", "replace"))


def control_binding(server_key: bytes, control_token_hash: str) -> str:
    """Bind a session to the control token configured when it was created (its hash, never the token)."""
    message = b"control-binding:" + control_token_hash.upper().encode("ascii", "replace")
    return hmac.new(server_key, message, hashlib.sha256).hexdigest()


def sanitize_label(raw: str | None) -> str | None:
    """1-64 printable characters: control and format characters dropped, whitespace collapsed; ``None`` if empty."""
    if raw is None:
        return None
    kept = "".join(" " if ch.isspace() else ch for ch in raw
                   if ch.isspace() or not unicodedata.category(ch).startswith(("C", "Z")))
    label = " ".join(kept.split())[:64].strip()
    return label or None
