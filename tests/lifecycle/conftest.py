"""Lifecycle tests build ``Settings`` explicitly; ambient PADOS_* configuration must not leak in.

Gate runners export disposable runtime roots (PADOS_PRODUCT_WORKSPACE_ROOT, ...) for live gates;
without this fixture the storage-location registry would also pick those up.
"""
from __future__ import annotations

import os

from collections.abc import Iterator

import pytest

from pados.config import get_settings


@pytest.fixture(autouse=True)
def _hermetic_pados_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in [key for key in os.environ if key.startswith("PADOS_")]:
        monkeypatch.delenv(name)
    # get_settings() is cached process-wide: never let a settings object built from this
    # stripped environment leak into later test modules (e.g. their database port).
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
