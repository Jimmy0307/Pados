"""Leak markers for tests that guard product code against external or private material.

Generic tokens are public. Organisation, private-model and personal patterns are private data themselves, so
they are never written into tracked files: they are loaded from the untracked marker file named by
``PADOS_PRIVATE_MARKERS_FILE`` (the same file the public-export scan uses) when it is present.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# External orchestration products must never be named by the product package (generic, public tokens).
GENERIC_FORBIDDEN_TOKENS = ("Gas City",)


def private_marker_patterns() -> tuple[re.Pattern[str], ...]:
    """The operator's private marker patterns, or none when the marker file is not configured."""
    configured = os.environ.get("PADOS_PRIVATE_MARKERS_FILE")
    if not configured or not Path(configured).is_file():
        return ()
    rules = json.loads(Path(configured).read_text(encoding="utf-8"))
    return tuple(re.compile(str(pattern), re.IGNORECASE) for pattern in rules.values())


def leaked_markers(text: str) -> list[str]:
    """Names of the markers found in ``text`` (never the matched text itself)."""
    found = [f"generic:{index}" for index, token in enumerate(GENERIC_FORBIDDEN_TOKENS) if token in text]
    found += [f"private:{index}" for index, pattern in enumerate(private_marker_patterns()) if pattern.search(text)]
    return found
