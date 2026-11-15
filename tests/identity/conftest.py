# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for the Unified Identity & Session tests: a unique control token, a temporary store, a fake clock."""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.identity.identity_support import CONTROL_VALUE, FakeClock, Harness


def _set_env(monkeypatch: pytest.MonkeyPatch, store_dir: Path, enabled: bool, extra: dict[str, str]) -> None:
    from pados import config

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    monkeypatch.setenv("PADOS_API_HOST", "127.0.0.1")
    monkeypatch.setenv("PADOS_IDENTITY_SESSIONS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("PADOS_IDENTITY_STORE_DIR", str(store_dir))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()


@pytest.fixture
def make_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    from pados import config

    def build(*, enabled: bool = True, base_url: str = "http://testserver", clock: FakeClock | None = None,
              store_dir: Path | None = None, **extra: str) -> Harness:
        from pados.api import app as api_app
        from pados.identity.sessions import IdentityService

        directory = store_dir or tmp_path / "identity"
        _set_env(monkeypatch, directory, enabled, extra)
        active_clock = clock or FakeClock()
        app = api_app.create_app()
        app.state.pados_identity = IdentityService(config.get_settings(), clock=active_clock)
        return Harness(app=app, clock=active_clock, store_dir=directory, base_url=base_url)

    yield build
    config.get_settings.cache_clear()


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.api import app as api_app

    @contextlib.contextmanager
    def no_db(_settings: object = None) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(api_app, "session_scope", no_db)
