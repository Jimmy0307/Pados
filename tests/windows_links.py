"""Windows-native link fixtures for filesystem boundary tests.

Directory junctions are reparse points and exercise PADOS' Windows link refusal without requiring
SeCreateSymbolicLinkPrivilege. File symlinks may require that Windows privilege; those specific
tests report an explicit skip rather than claiming the file-link behavior was exercised.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def create_link_or_skip(target: Path, link: Path, *, directory: bool) -> None:
    if os.name == "nt" and directory:
        import _winapi

        try:
            _winapi.CreateJunction(os.fspath(target), os.fspath(link))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip(f"WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE: {exc}")
            raise
        return
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"WINDOWS_SYMLINK_PRIVILEGE_UNAVAILABLE: {exc}")
        raise
