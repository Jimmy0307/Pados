# SPDX-License-Identifier: AGPL-3.0-only
"""Round-14 regressions for the three latest-head Codex P2 findings."""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop.contracts import FailureCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore
from pados.capability_loop_runtime.binding import AnchoredSessionBindingAuthority, anchored_session_binding_authority
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import Checkpoint
from pados.learning import store as learning_store

PROVIDER = "openmaic.adaptive_learning"
SESSION = "job-r14"


def _binding(*, project_id: str = "proj-a") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER,
        external_session_ref=SESSION,
        project_id=project_id,
        work_contract_id="wc-1",
        classification=DataClassification.INTERNAL,
        capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def test_failed_provider_session_is_not_authoritatively_bound(tmp_path: Path) -> None:
    http = FakeHttp({**HEALTH_OK, **generate_ok(step="failed", progress=100, job_id=SESSION)})
    provider = OpenMaicLearningProvider(ENDPOINT, http)
    authority = anchored_session_binding_authority(
        tmp_path / "binding-log" / "binding.jsonl", tmp_path / "binding-anchor"
    )
    service = LearningService(
        provider,
        LoopStore(tmp_path / "loop.json"),
        capability=LEARNING_CAPABILITY,
        adapter=LEARNING_ADAPTER,
        authority=authority,
    )

    receipt = service.create_session(
        learning_request(), contract=contract(), grant=grant(), classification=DataClassification.INTERNAL
    )

    assert receipt.result is OperationResult.PROVIDER_FAILED
    assert receipt.failure is FailureCode.MALFORMED_RESPONSE
    assert authority.resolve(PROVIDER, SESSION) is None


def test_windows_binding_cache_does_not_trust_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "binding-log" / "binding.jsonl"
    authority = anchored_session_binding_authority(log_path, tmp_path / "binding-anchor")
    authority.bind(_binding())
    assert authority.resolve(PROVIDER, SESSION) is not None  # populate the parsed-log cache

    stable = os.stat(log_path)
    original = log_path.read_bytes()
    tampered = original.replace(b'"project_id":"proj-a"', b'"project_id":"proj-b"')
    assert tampered != original and len(tampered) == len(original)
    log_path.write_bytes(tampered)
    os.utime(log_path, ns=(stable.st_atime_ns, stable.st_mtime_ns))

    # Simulate Windows metadata semantics: ctime is creation time and therefore unchanged by the rewrite.
    monkeypatch.setattr(authority._file, "stat", lambda: stable)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(BindingAuthorityError):
        authority.resolve(PROVIDER, SESSION)


class _RecordingAnchor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.head: Checkpoint | None = None

    def latest(self) -> Checkpoint | None:
        return self.head

    def markers(self) -> tuple[Checkpoint, ...]:
        return ()

    def record(self, previous: Checkpoint | None, new: Checkpoint, *, marker: bool = False) -> None:
        assert not marker
        assert previous == self.head
        self.events.append("anchor")
        self.head = new


def test_new_binding_log_directory_entry_is_fsynced_before_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(learning_store, "fsync_directory", lambda _path: events.append("dir"))
    authority = AnchoredSessionBindingAuthority(
        tmp_path / "binding-log" / "binding.jsonl", _RecordingAnchor(events)
    )

    authority.bind(_binding())

    assert "dir" in events
    assert events.index("dir") < events.index("anchor")
