# SPDX-License-Identifier: AGPL-3.0-only
from pados.config import Settings
from pados.providers.base import LLMProvider
from pados.providers.claude_adapter import ClaudeAdapter
from pados.providers.codex_adapter import CodexAdapter
from pados.providers.mock import MockProvider
from pados.providers.ollama import OllamaProvider


class ProviderNotConfiguredError(RuntimeError):
    pass


def build_chat_provider(settings: Settings, *, allow_mock: bool = False) -> LLMProvider:
    provider_name = settings.chat_provider.lower()
    if provider_name == "mock":
        if not allow_mock:
            raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:mock requires explicit allow_mock")
        return MockProvider()
    if provider_name == "ollama":
        if not settings.chat_model:
            raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:missing OLLAMA model")
        return OllamaProvider(settings.ollama_base_url, model=settings.chat_model)
    if provider_name == "codex":
        raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:codex")
    if provider_name == "claude":
        raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:claude")
    raise ProviderNotConfiguredError(f"PROVIDER_NOT_CONFIGURED:{settings.chat_provider}")


def disabled_provider_health() -> list[dict[str, str]]:
    return [
        {"provider": "codex", **CodexAdapter().healthcheck_sync()},
        {"provider": "claude", **ClaudeAdapter().healthcheck_sync()},
    ]
