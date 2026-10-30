# SPDX-License-Identifier: AGPL-3.0-only
"""Round-7: rollback failures are normalized, and the parsed-log cache never masks a tamper."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime.binding import (
    GENESIS,
    AnchoredSessionBindingAuthority,
    _BindingRecord,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import Checkpoint

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str = "job-1", project: str = "proj-a") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id=project, work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at="2026-10-14T00:00:00+00:00")


class _FailingAnchor:
    """record() raises without committing; latest() stays at the old head."""

    def __init__(self) -> None:
        self._latest: Checkpoint | None = None

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        raise OSError("anchor write failed")

    def latest(self) -> Checkpoint | None:
        return self._latest

    def markers(self) -> tuple[Checkpoint, ...]:
        return ()


# -- R7-F1: rollback failures are normalized -------------------------------
def test_rollback_failure_is_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = AnchoredSessionBindingAuthority(tmp_path / "binding.jsonl", _FailingAnchor())

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("rollback failed")

    monkeypatch.setattr(auth._file, "rewrite", boom)
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.bind(_binding())
    assert excinfo.value.code == "BINDING_LOG_WRITE_FAILED"  # not a raw OSError


# -- R7-F2: cache avoids re-parsing, but never masks a tamper --------------
def test_repeated_resolve_reuses_the_parsed_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor")
    auth.bind(_binding())
    calls = [0]
    original = auth._file.parse_payload

    def counting(payload: bytes) -> list[dict]:
        calls[0] += 1
        return original(payload)

    monkeypatch.setattr(auth._file, "parse_payload", counting)
    assert auth.resolve(PROVIDER, "job-1") is not None  # first lookup parses once
    assert auth.resolve(PROVIDER, "job-1") is not None  # second lookup hits the cache
    assert calls[0] == 1  # the unchanged log was parsed only once


def test_cache_does_not_mask_a_later_tamper(tmp_path: Path) -> None:
    log = tmp_path / "log" / "binding.jsonl"
    auth = anchored_session_binding_authority(log, tmp_path / "anchor")
    auth.bind(_binding(project="proj-a"))
    assert auth.resolve(PROVIDER, "job-1").project_id == "proj-a"  # type: ignore[union-attr]  # populates cache
    # Tamper the log on the SAME instance; the changed file stamp forces a re-parse, so the anchor mismatch
    # is detected rather than served stale from the cache.
    tampered = _BindingRecord.seal(_binding(project="proj-evil"), seq=1, prev_hash=GENESIS).model_dump(mode="json")
    log.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(BindingAuthorityError) as excinfo:
        auth.resolve(PROVIDER, "job-1")
    assert excinfo.value.code == "BINDING_HEAD_NOT_ANCHORED"
