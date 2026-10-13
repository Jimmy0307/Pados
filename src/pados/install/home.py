# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS_HOME resolution and layout (mirrors ``packages/cli/lib/home.js``; shared vectors keep them equal).

Defaults follow each platform's per-user application-data convention:

* Windows: ``%LOCALAPPDATA%\\PADOS`` (per-user, not roaming: runtimes and databases must not roam);
* Linux: ``$XDG_DATA_HOME/pados`` (absolute values only), else ``~/.local/share/pados``;
* macOS: ``~/Library/Application Support/PADOS``.

``PADOS_HOME`` overrides the default and must be absolute. The drive-root ``pados`` directory of the system
drive is reserved (a developer checkout location) and is always refused.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from collections.abc import Mapping
from pathlib import Path

LAYOUT = ("config", "runtime", "data", "logs", "cache", "state")
EPHEMERAL_DIRECTORIES = ("cache",)
CONFIG_FILE = "pados.env"
STORAGE_PROFILE_FILE = "storage-profile.json"
CONTROL_TOKEN_FILE = "control.token"


class HomeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_home(platform: str, environ: Mapping[str, str], user_home: str) -> str:
    """Resolve PADOS_HOME for ``platform`` (``sys.platform`` style) without touching the filesystem."""
    paths = ntpath if platform == "win32" else posixpath
    override = environ.get("PADOS_HOME", "").strip()
    if override:
        if not paths.isabs(override):
            raise HomeError("PADOS_HOME_NOT_ABSOLUTE")
        candidate = paths.normpath(override)
    elif platform == "win32":
        local = environ.get("LOCALAPPDATA", "").strip()
        base = local if local and ntpath.isabs(local) else ntpath.join(user_home, "AppData", "Local")
        candidate = ntpath.normpath(ntpath.join(base, "PADOS"))
    elif platform == "darwin":
        candidate = posixpath.join(user_home, "Library", "Application Support", "PADOS")
    else:
        xdg = environ.get("XDG_DATA_HOME", "").strip()
        base = xdg if xdg and posixpath.isabs(xdg) else posixpath.join(user_home, ".local", "share")
        candidate = posixpath.normpath(posixpath.join(base, "pados"))
    if platform == "win32" and candidate.rstrip("\\").casefold() == "c:\\pados":
        raise HomeError("PADOS_HOME_RESERVED_PATH")
    return str(candidate)


def resolve_home(environ: Mapping[str, str] | None = None) -> Path:
    active = os.environ if environ is None else environ
    return Path(default_home(sys.platform, active, str(Path.home())))


def config_file(home: Path) -> Path:
    return home / "config" / CONFIG_FILE


def storage_profile_file(home: Path) -> Path:
    return home / "config" / STORAGE_PROFILE_FILE


def ensure_layout(home: Path) -> None:
    for name in LAYOUT:
        (home / name).mkdir(parents=True, exist_ok=True)
