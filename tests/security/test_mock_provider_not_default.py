# SPDX-License-Identifier: AGPL-3.0-only
"""RC2 MODEL-1: the mock chat provider is never a silent default. Unset means unconfigured (cognitive actions are
disabled with a reason); mock runs only when explicitly selected in a local/test/validation environment, stays
labeled MOCK_CONTRACT_TEST_ONLY, and the doctor warns about it."""
from __future__ import annotations

from typing import Any

import pytest

from pados.config import Settings
from pados.graph import service as graph_service
from pados.providers.factory import chat_provider_mode


def _settings(monkeypatch: pytest.MonkeyPatch, **values: str) -> Settings:
    monkeypatch.delenv("PADOS_CHAT_PROVIDER", raising=False)
    return Settings.model_validate(values) if values else Settings(_env_file=None)  # type: ignore[call-arg]


def test_unset_chat_provider_is_unconfigured_not_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    assert settings.env == "local" and settings.chat_provider == ""
    assert chat_provider_mode(settings) == "UNCONFIGURED"
    reason = graph_service._provider_reason(settings)
    assert reason is not None and reason.code == "COGNITIVE_PROVIDER_NOT_CONFIGURED"


def test_explicit_mock_is_allowed_locally_and_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, PADOS_ENV="local", PADOS_CHAT_PROVIDER="mock")
    assert chat_provider_mode(settings) == "MOCK_CONTRACT_TEST_ONLY"
    assert graph_service._provider_reason(settings) is None
    assert graph_service._cognitive_runtime_status(settings, None) == "MOCK_CONTRACT_TEST_ONLY"


def test_explicit_mock_outside_local_test_validation_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, PADOS_ENV="production", PADOS_CHAT_PROVIDER="mock")
    reason = graph_service._provider_reason(settings)
    assert reason is not None and reason.code == "COGNITIVE_PROVIDER_NOT_CONFIGURED"


def test_doctor_warns_when_the_mock_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.execution import readiness as readiness_module
    from pados.install import doctor
    from pados.operations import service as operations_service

    health: dict[str, Any] = {"dimensions": {"provider_configured": True}, "reasons": []}
    monkeypatch.setattr(operations_service, "system_health", lambda _settings: health)
    monkeypatch.setattr(readiness_module, "readiness", lambda _report: {"state": "ready"})
    mocked, _ = doctor.check_readiness(_settings(monkeypatch, PADOS_CHAT_PROVIDER="mock"), True)
    assert mocked["status"] == "WARNING" and mocked["code"] == "MOCK_CHAT_PROVIDER_SELECTED"
    real, _ = doctor.check_readiness(_settings(monkeypatch, PADOS_CHAT_PROVIDER="ollama"), True)
    assert real["code"] == "PROVIDER_CONFIGURED"
