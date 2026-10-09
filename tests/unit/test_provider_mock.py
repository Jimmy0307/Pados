import asyncio

from pados.providers.mock import MockProvider


def test_mock_provider_healthcheck() -> None:
    provider = MockProvider()
    assert asyncio.run(provider.healthcheck()) == {"status": "ok", "provider": "mock"}
