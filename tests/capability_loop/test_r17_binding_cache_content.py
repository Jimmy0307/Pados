# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [22][27][40]: the parsed binding-log cache is keyed on CONTENT, never on file metadata.

Metadata keys (inode/size/mtime, POSIX ctime) are not tamper-resistant: a writer can restore mtime, Windows
``st_ctime_ns`` is creation time, and coarse-resolution filesystems can leave ctime unchanged across a same-size
rewrite. The cache must therefore be keyed on a digest of the exact bytes read, which is platform-independent:
an unchanged log reuses its parsed records on every platform (the 100k-binding lookup cost), and any byte change
-- whatever the metadata says -- is re-parsed and detected.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop_runtime import binding as binding_runtime
from pados.capability_loop_runtime.binding import anchored_session_binding_authority
from pados.enterprise.classification import DataClassification

PROVIDER = "openmaic.adaptive_learning"


def _binding(session: str = "job-cache") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id="proj-a", work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def test_same_size_rewrite_with_identical_metadata_is_detected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_path = tmp_path / "binding-log" / "binding.jsonl"
    authority = anchored_session_binding_authority(log_path, tmp_path / "binding-anchor")
    authority.bind(_binding())
    assert authority.resolve(PROVIDER, "job-cache") is not None  # populate the cache

    stable = os.stat(log_path)
    original = log_path.read_bytes()
    tampered = original.replace(b'"project_id":"proj-a"', b'"project_id":"proj-b"')
    assert tampered != original and len(tampered) == len(original)
    log_path.write_bytes(tampered)
    os.utime(log_path, ns=(stable.st_atime_ns, stable.st_mtime_ns))
    # Worst case for a metadata-keyed cache: every stat field (incl. ctime) is identical to the cached parse.
    monkeypatch.setattr(authority._file, "stat", lambda: stable)

    with pytest.raises(BindingAuthorityError):
        authority.resolve(PROVIDER, "job-cache")


def test_unchanged_log_reuses_parsed_records_on_every_platform(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = anchored_session_binding_authority(
        tmp_path / "binding-log" / "binding.jsonl", tmp_path / "binding-anchor")
    for index in range(3):
        authority.bind(_binding(f"job-{index}"))
    assert authority.resolve(PROVIDER, "job-0") is not None

    parsed: list[Any] = []
    real = binding_runtime._BindingRecord.model_validate

    def counting(value: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(value)
        return real(value, *args, **kwargs)

    monkeypatch.setattr(binding_runtime._BindingRecord, "model_validate", counting)
    for _ in range(5):
        assert authority.resolve(PROVIDER, "job-2") is not None
    assert parsed == []  # identical bytes: no re-validation of the whole log per lookup
