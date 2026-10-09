# SPDX-License-Identifier: AGPL-3.0-only
"""Artifact-only data exchange: explicit staged inputs in, artifacts out.

Sandboxes never receive host mounts. The denylist below is enforced on every
requested mount source, and the only allowed sources are per-run staging
directories owned by PADOS.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath

from pados.sandbox.policy import PolicyError

_WINDOWS_PROFILE = re.compile(r"(?i)^[a-z]:[\\/]+users([\\/]|$)")
_WINDOWS_DRIVE_ROOT = re.compile(r"(?i)^[a-z]:[\\/]*$")
_POSIX_FORBIDDEN_PREFIXES = ("/Users", "/home", "/root", "/etc", "/var/lib/postgresql", "/mnt", "/proc", "/sys", "/dev")


def _denied_reason(source: str) -> str:
    """Specific reason code from the literal source (platform-neutral)."""
    posix = source.replace("\\", "/")
    if _WINDOWS_PROFILE.match(source):
        return "MOUNT_WINDOWS_USER_PROFILE_FORBIDDEN"
    if _WINDOWS_DRIVE_ROOT.match(source) or posix in {"/", ""}:
        return "MOUNT_WHOLE_DRIVE_FORBIDDEN"
    if PureWindowsPath(source).drive and not source.startswith("/"):
        return "MOUNT_HOST_DRIVE_FORBIDDEN"
    if any(posix == p or posix.startswith(p + "/") for p in _POSIX_FORBIDDEN_PREFIXES):
        return "MOUNT_HOST_PATH_FORBIDDEN"
    return "MOUNT_OUTSIDE_STAGING_FORBIDDEN"


def assert_mount_allowed(source: str, *, staging_root: Path, protected: tuple[Path, ...] = ()) -> Path:
    """Only a directory strictly inside ``staging_root`` may be mounted into a sandbox.

    The path is resolved first (symlink escapes resolve to their target). Anything
    that is not inside the staging root, or that overlaps a protected path, is
    refused; the reason code names the matching denylist class.
    """
    if not source or "\x00" in source:
        raise PolicyError("MOUNT_SOURCE_INVALID")
    resolved = Path(os.path.realpath(source))
    root = Path(os.path.realpath(staging_root))
    for guarded in protected:
        g = Path(os.path.realpath(guarded))
        if resolved == g or g in resolved.parents or resolved in g.parents:
            raise PolicyError("MOUNT_PROTECTED_PATH_FORBIDDEN")
    if resolved != root and root in resolved.parents:
        return resolved
    raise PolicyError(_denied_reason(source))
