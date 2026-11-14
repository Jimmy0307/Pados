# SPDX-License-Identifier: AGPL-3.0-only
"""``os.replace`` that survives a transient Windows sharing refusal and still fails closed on a persistent one.

On Windows, replacing a file that another handle holds open without delete sharing fails with
``PermissionError`` (winerror 5, or 32/33 for sharing and lock violations). Readers reloading a store,
search indexers and on-access scanners hold such handles briefly. The retry is bounded and covers only
those native codes; every other error, every error on other platforms and a refusal that outlasts the
retries propagate unchanged, so callers keep failing closed and keep the previous file.
"""
from __future__ import annotations

import os
import sys
import time

WINDOWS_REPLACE_RETRIES = 8
WINDOWS_REPLACE_RETRYABLE_ERRORS = frozenset({5, 32, 33})


def replace_with_retry(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Atomically move ``source`` over ``destination``, retrying transient Windows sharing refusals."""
    attempts = WINDOWS_REPLACE_RETRIES if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if (sys.platform != "win32" or getattr(exc, "winerror", None) not in WINDOWS_REPLACE_RETRYABLE_ERRORS
                    or attempt + 1 == attempts):
                raise
            time.sleep(min(0.01 * (2 ** attempt), 0.25))
