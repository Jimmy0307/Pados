# SPDX-License-Identifier: AGPL-3.0-only
"""Round-13: a short low-level write must not let the binding anchor outrun the persisted log."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pados.capability_loop.adapters.openmaic_http import OPENMAIC_PROVIDER_ID
from pados.capability_loop.binding_authority import GovernedSessionBinding
from pados.capability_loop_runtime.binding import anchored_session_binding_authority
from pados.enterprise.classification import DataClassification
from pados.learning import store as learning_store

PROVIDER = OPENMAIC_PROVIDER_ID
SESSION = "job-short-write"


def _binding() -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER,
        external_session_ref=SESSION,
        project_id="proj-a",
        work_contract_id="wc-1",
        classification=DataClassification.INTERNAL,
        capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def test_short_os_write_is_completed_before_binding_anchor_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legal short write must be retried until the full JSONL record is durable before anchoring."""
    authority = anchored_session_binding_authority(
        tmp_path / "log" / "binding.jsonl", tmp_path / "anchor"
    )
    real_write = os.write
    calls = 0

    def short_once(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal calls
        calls += 1
        payload = bytes(data)
        if calls == 1:
            first = max(1, len(payload) // 2)
            return real_write(fd, payload[:first])
        return real_write(fd, payload)

    monkeypatch.setattr(learning_store.os, "write", short_once)

    authority.bind(_binding())
    resolved = authority.resolve(PROVIDER, SESSION)

    assert calls >= 2
    assert resolved is not None
    assert resolved.context_hash == _binding().context_hash
