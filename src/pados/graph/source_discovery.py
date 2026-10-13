# SPDX-License-Identifier: AGPL-3.0-only
"""Future-facing source discovery seam.

P6.17 deliberately does not implement web search or crawling.  The seam makes
that limitation explicit instead of routing SEARCH to graph CONNECT.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class SourceDiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SourceDiscovery(Protocol):
    def discover(self, query: str, *, limit: int = 5) -> Sequence[dict[str, Any]]:
        """Return bounded source candidates for a future configured provider."""


class UnconfiguredSourceDiscovery:
    def discover(self, query: str, *, limit: int = 5) -> Sequence[dict[str, Any]]:
        del query, limit
        raise SourceDiscoveryError(
            "SEARCH_PROVIDER_NOT_CONFIGURED",
            "目前尚未設定公開資料搜尋來源。",
        )
