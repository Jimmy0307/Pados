# SPDX-License-Identifier: AGPL-3.0-only
"""Round-9: normalize stat/lock failures, roll back on first-anchor-init failure, tamper-resistant cache key."""
from __future__ import annotations

from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import (
    AnchoredSessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import AnchorError, Checkpoint
from pados.learning.store import LearningStoreError

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str = "job-1") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id="proj-a", work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at="2026-10-14T00:00:00+00:00")


# -- R9-F1: filesystem stat failure is normalized --------------------------
def test_stat_failure_is_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")

    def insecure() -> None:
        raise LearningStoreError("LEARNING_STORE_INSECURE")

    monkeypatch.setattr(auth._file, "stat", insecure)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_LOG_CORRUPT"


# -- R9-F2: a failed first-anchor init rolls the orphan record back --------
class _InitFailAnchor:
    """record() marks the anchor initialized (sentinel) then fails the head write; latest() then reports lost."""

    def __init__(self) -> None:
        self._initialized = False

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        self._initialized = True
        raise OSError("head write failed during initialization")

    def latest(self) -> Checkpoint | None:
        if self._initialized:
            raise AnchorError("ANCHOR_HEAD_LOST")
        return None

    def markers(self) -> tuple[Checkpoint, ...]:
        return ()


def test_failed_first_anchor_init_rolls_the_log_back(tmp_path: Path) -> None:
    log = tmp_path / "binding.jsonl"
    auth = AnchoredSessionBindingAuthority(log, _InitFailAnchor())
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.bind(_binding())
    assert excinfo.value.code == "BINDING_ANCHOR_UNAVAILABLE"
    # the orphan record was rolled back: the log holds no binding records.
    assert log.read_text(encoding="utf-8").strip() == ""


# -- R9-F4: the cache key includes ctime (which utime cannot restore) ------
def test_cache_stamp_includes_ctime(tmp_path: Path) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    auth.bind(_binding())
    stamp = auth._stamp()
    assert stamp is not None and len(stamp) == 4  # (ino, size, mtime_ns, ctime_ns)
