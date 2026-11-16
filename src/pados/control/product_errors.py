# SPDX-License-Identifier: AGPL-3.0-only
"""The closed refusal of the product control routes (projects, documents, read projections).

Each refusal carries one closed code and its HTTP status; the route turns it into the ``pados.error.v1`` envelope.
"""
from __future__ import annotations

import uuid


class ProductControlError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def require_uuid(value: str, code: str = "INVALID_IDENTIFIER") -> str:
    """A path identifier must be a UUID; anything else is refused before it reaches the database."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProductControlError(code, 422) from exc
