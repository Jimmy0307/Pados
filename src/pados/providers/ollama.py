# SPDX-License-Identifier: AGPL-3.0-only
import httpx


class OllamaProvider:
    def __init__(self, base_url: str, *, model: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def chat(self, prompt: str) -> str:
        if not self.model:
            raise RuntimeError("PROVIDER_NOT_CONFIGURED:missing OLLAMA model")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False, "format": "json"},
            )
            response.raise_for_status()
            payload = response.json()
        return str(payload.get("response", ""))

    async def healthcheck(self) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
        return {"status": "ok", "provider": "ollama"}

    async def estimate_context(self, prompt: str) -> int:
        return max(1, len(prompt.split()))
