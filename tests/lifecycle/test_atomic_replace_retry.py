# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic store writers survive a transient Windows sharing refusal and still fail closed on a persistent one.

On Windows, ``os.replace`` onto a file that another handle has open without delete sharing fails with
``PermissionError`` (winerror 5). A reader reloading a store, a search indexer or an on-access scanner holds
such handles briefly, so the operational stores, the managed known_hosts file and the owner-only machine
files retry that refusal a bounded number of times. A persistent refusal still propagates, the previous
content stays in place and no temporary file is left behind.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from pados.connectivity.storage import atomic_write_json
from pados.lifecycle import atomic_replace
from pados.machines.fsguard import replace_owner_only
from pados.machines.inventory import write_known_hosts

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing refusals are platform-specific")


def _write_json(path: Path, generation: int) -> None:
    atomic_write_json(path, {"generation": generation})


def _write_known_hosts(path: Path, generation: int) -> None:
    profile = type("Profile", (), {"machine_id": f"machine-{generation}", "host_public_key": "ssh-ed25519 AAAA"})()
    write_known_hosts(path, [profile])


def _write_owner_only(path: Path, generation: int) -> None:
    replace_owner_only(path, json.dumps({"generation": generation}).encode("utf-8"), "TEST_FILE")


WRITERS: dict[str, tuple[Callable[[Path, int], None], str]] = {
    "operational-store": (_write_json, "store.json"),
    "known-hosts": (_write_known_hosts, "pados_known_hosts"),
    "owner-only": (_write_owner_only, "head.json"),
}


def _refusal(winerror: int) -> PermissionError:
    refusal = PermissionError(f"simulated Windows refusal {winerror}")
    refusal.winerror = winerror  # type: ignore[attr-defined]
    return refusal


def _temporaries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


@WINDOWS_ONLY
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_reader_holding_the_target_open_does_not_fail_the_write(tmp_path: Path, name: str) -> None:
    write, filename = WRITERS[name]
    target = tmp_path / filename
    write(target, 1)
    before = target.read_bytes()
    handle = open(target, "rb")  # noqa: SIM115 - a plain open() denies delete sharing on Windows
    releaser = threading.Timer(0.05, handle.close)
    releaser.start()
    try:
        write(target, 2)
    finally:
        releaser.join()
        handle.close()
    assert target.read_bytes() != before
    assert _temporaries(tmp_path) == []


@WINDOWS_ONLY
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_transient_sharing_refusals_are_retried(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    write, filename = WRITERS[name]
    target = tmp_path / filename
    real_replace = os.replace
    attempts = 0

    def transient(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _refusal(32 if attempts == 1 else 5)
        real_replace(source, destination)

    monkeypatch.setattr(atomic_replace.os, "replace", transient)
    write(target, 1)
    assert attempts == 3
    assert target.exists() and _temporaries(tmp_path) == []


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_persistent_refusal_still_fails_closed(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    write, filename = WRITERS[name]
    target = tmp_path / filename
    write(target, 1)
    before = target.read_bytes()
    attempts = 0

    def persistent(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        raise _refusal(5)

    monkeypatch.setattr(atomic_replace.os, "replace", persistent)
    with pytest.raises(PermissionError, match="simulated Windows refusal 5"):
        write(target, 2)
    assert attempts == (atomic_replace.WINDOWS_REPLACE_RETRIES if sys.platform == "win32" else 1)
    assert target.read_bytes() == before
    assert _temporaries(tmp_path) == []


@pytest.mark.parametrize("winerror", sorted(atomic_replace.WINDOWS_REPLACE_RETRYABLE_ERRORS))
def test_the_win32_retry_loop_is_bounded_on_every_platform(tmp_path: Path, winerror: int,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs on Linux CI too: the loop, its bound, its code set and its back-off, with win32 simulated."""
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    sleeps: list[float] = []
    attempts = 0

    def refuse(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        raise _refusal(winerror)

    monkeypatch.setattr(atomic_replace.sys, "platform", "win32")
    monkeypatch.setattr(atomic_replace.time, "sleep", sleeps.append)
    monkeypatch.setattr(atomic_replace.os, "replace", refuse)
    with pytest.raises(PermissionError):
        atomic_replace.replace_with_retry(source, destination)
    assert attempts == atomic_replace.WINDOWS_REPLACE_RETRIES == 8
    assert sleeps == [0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25]
    assert destination.read_bytes() == b"old"
    assert atomic_replace.WINDOWS_REPLACE_RETRYABLE_ERRORS == {5, 32, 33}


def test_the_simulated_win32_loop_does_not_retry_other_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def refuse(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        raise _refusal(1314)

    monkeypatch.setattr(atomic_replace.sys, "platform", "win32")
    monkeypatch.setattr(atomic_replace.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(atomic_replace.os, "replace", refuse)
    with pytest.raises(PermissionError):
        atomic_replace.replace_with_retry(tmp_path / "a", tmp_path / "b")
    assert attempts == 1


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_other_errors_are_never_retried(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    write, filename = WRITERS[name]
    target = tmp_path / filename
    attempts = 0

    def privilege_refusal(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        raise _refusal(1314)

    monkeypatch.setattr(atomic_replace.os, "replace", privilege_refusal)
    with pytest.raises(PermissionError, match="simulated Windows refusal 1314"):
        write(target, 1)
    assert attempts == 1
    assert not target.exists() and _temporaries(tmp_path) == []
