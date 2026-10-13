"""Phase G pipeline tests: pin the algorithm pack's only wall-clock reading for RECEIPT determinism.

``pados.packs.algorithms.adapters.AlgorithmAdapter`` reports ``runtime_ms`` (``time.perf_counter_ns``) as a
stage METRIC; stage receipts bind their metrics, so byte-identical receipt envelopes across runs need a fixed
reading (like the executor's injected clock). Artifacts never contain timing:
``test_pipeline_determinism.py::test_wall_clock_variance_never_changes_artifacts`` runs unpinned timing and
shows identical artifacts and a PASSED replay.
"""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from pados.packs.algorithms import adapters as algorithm_adapters


@pytest.fixture(autouse=True)
def pinned_algorithm_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(algorithm_adapters, "time", SimpleNamespace(perf_counter_ns=lambda: 0))
    yield
