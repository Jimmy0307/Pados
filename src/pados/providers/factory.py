# SPDX-License-Identifier: AGPL-3.0-only
from pados.config import Settings
from pados.providers.base import LLMProvider
from pados.providers.claude_adapter import ClaudeAdapter
from pados.providers.codex_adapter import CodexAdapter
from pados.providers.execution_binding import (
    MEASURED_EXECUTION_INTENT_REQUIRED,
    ExecutionIntent,
    refuse,
    resolve_bound_execution,
)
from pados.providers.mock import MockProvider
from pados.providers.ollama import MeasuredChatProvider


class ProviderNotConfiguredError(RuntimeError):
    pass


def check_chat_provider_configured(settings: Settings, *, allow_mock: bool = False) -> None:
    """Cheap configuration check (no live observation, no call); raises ``ProviderNotConfiguredError``.

    A configured local provider can still be refused at call time by the measured execution binding."""
    provider_name = settings.chat_provider.lower()
    if provider_name == "mock":
        if not allow_mock:
            raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:mock requires explicit allow_mock")
        return
    if provider_name == "ollama":
        if not settings.chat_model:
            raise ProviderNotConfiguredError("PROVIDER_NOT_CONFIGURED:missing OLLAMA model")
        return
    if provider_name in {"codex", "claude"}:
        raise ProviderNotConfiguredError(f"PROVIDER_NOT_CONFIGURED:{provider_name}")
    raise ProviderNotConfiguredError(f"PROVIDER_NOT_CONFIGURED:{settings.chat_provider}")


def build_chat_provider(settings: Settings, *, allow_mock: bool = False, intent: ExecutionIntent | None = None,
                        project_id: str | None = None) -> LLMProvider:
    """The chat provider for ``settings``. A local model is returned only under a measured execution binding
    for ``intent`` in ``project_id`` (``ExecutionBindingRefused`` otherwise); there is no unbound fallback."""
    check_chat_provider_configured(settings, allow_mock=allow_mock)
    if settings.chat_provider.lower() == "mock":
        return MockProvider()
    if intent is None or not project_id:
        raise refuse(MEASURED_EXECUTION_INTENT_REQUIRED)
    return MeasuredChatProvider(resolve_bound_execution(settings, intent, project_id=project_id))


def disabled_provider_health() -> list[dict[str, str]]:
    return [
        {"provider": "codex", **CodexAdapter().healthcheck_sync()},
        {"provider": "claude", **ClaudeAdapter().healthcheck_sync()},
    ]
