# SPDX-License-Identifier: AGPL-3.0-only
from typing import Protocol


class LLMProvider(Protocol):
    async def chat(self, prompt: str) -> str:
        ...

    async def healthcheck(self) -> dict[str, str]:
        ...

    async def estimate_context(self, prompt: str) -> int:
        ...

