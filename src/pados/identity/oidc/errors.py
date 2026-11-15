# SPDX-License-Identifier: AGPL-3.0-only
"""An OIDC refusal: one closed code for the caller and one fixed reason word for the server log."""
from __future__ import annotations

import re

from pados.identity.errors import IdentityError

_REASON = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


class OidcFailure(IdentityError):
    """``code`` is a closed identity code; ``reason`` is a word chosen in code, never provider or request text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.reason = reason if _REASON.fullmatch(reason) else "unspecified"
