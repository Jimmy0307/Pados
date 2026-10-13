# SPDX-License-Identifier: AGPL-3.0-only
from collections.abc import Mapping
from typing import Any, Protocol


class LLMProvider(Protocol):
    async def chat(self, prompt: str, *, response_schema: Mapping[str, Any] | None = None) -> str:
        ...

    async def healthcheck(self) -> dict[str, str]:
        ...

    async def estimate_context(self, prompt: str) -> int:
        ...
