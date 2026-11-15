# SPDX-License-Identifier: AGPL-3.0-only
"""The closed identity error codes and their HTTP status (design §3.5)."""
from __future__ import annotations

from typing import Final

IDENTITY_DISABLED: Final = "IDENTITY_DISABLED"
PAIRING_CODE_INVALID: Final = "PAIRING_CODE_INVALID"
PAIRING_CODE_EXPIRED: Final = "PAIRING_CODE_EXPIRED"
PAIRING_RATE_LIMITED: Final = "PAIRING_RATE_LIMITED"
SESSION_REQUIRED: Final = "SESSION_REQUIRED"
SESSION_EXPIRED: Final = "SESSION_EXPIRED"
SESSION_REVOKED: Final = "SESSION_REVOKED"
CSRF_INVALID: Final = "CSRF_INVALID"
ORIGIN_NOT_ALLOWED: Final = "ORIGIN_NOT_ALLOWED"
DEVICE_NOT_FOUND: Final = "DEVICE_NOT_FOUND"
DEVICE_LIMIT: Final = "DEVICE_LIMIT"
IDENTITY_STORE_CORRUPT: Final = "IDENTITY_STORE_CORRUPT"

STATUS_BY_CODE: Final[dict[str, int]] = {
    IDENTITY_DISABLED: 404,
    PAIRING_CODE_INVALID: 403,
    PAIRING_CODE_EXPIRED: 403,
    PAIRING_RATE_LIMITED: 429,
    SESSION_REQUIRED: 403,
    SESSION_EXPIRED: 403,
    SESSION_REVOKED: 403,
    CSRF_INVALID: 403,
    ORIGIN_NOT_ALLOWED: 403,
    DEVICE_NOT_FOUND: 404,
    DEVICE_LIMIT: 409,
    IDENTITY_STORE_CORRUPT: 503,
}


class IdentityError(Exception):
    """A refusal carrying one closed code; the message is the code itself, never request data."""

    def __init__(self, code: str) -> None:
        if code not in STATUS_BY_CODE:
            raise ValueError("unknown identity error code")
        super().__init__(code)
        self.code = code
        self.status_code = STATUS_BY_CODE[code]
