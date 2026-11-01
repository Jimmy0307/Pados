# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only probe: does every relative Markdown reference in the project resolve inside the project?

Run by the registered ``markdown_reference_check`` command as ``python -I <this file>`` with the project root as the
working directory. Standard library only, so isolated mode never imports project code. Standard output is exactly a
``pados.capability-outcome.v1`` document; the evidence (counts and broken references) goes to standard error.
COMPLETE only when at least one Markdown file was checked and nothing is broken; anything else is UNRESOLVED.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

SCHEMA = "pados.capability-outcome.v1"
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}
MAX_FILES = 5000
MAX_FILE_BYTES = 1_000_000
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
FENCE = re.compile(r"^\s*(```|~~~)")


def _markdown_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, subdirs, files in os.walk(root):  # never follows directory links
        subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS)
        found.extend(Path(directory) / name for name in sorted(files) if name.lower().endswith(".md"))
        if len(found) > MAX_FILES:
            raise OverflowError("TOO_MANY_MARKDOWN_FILES")
    return found


def _targets(text: str) -> list[str]:
    targets: list[str] = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            targets.extend(LINK.findall(line))
    return targets


def _resolves(root: Path, source: Path, target: str) -> bool | None:
    """True/False for a local reference; None for an external link or a same-page anchor (not checked)."""
    if target.startswith("#") or SCHEME.match(target) or target.startswith("//"):
        return None
    path = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not path:
        return None
    candidate = (root / path.lstrip("/")) if path.startswith("/") else (source.parent / path)
    resolved = candidate.resolve()
    return (resolved == root or root in resolved.parents) and resolved.exists()


def check(root: Path) -> tuple[str, dict[str, object]]:
    root = root.resolve()
    files = _markdown_files(root)
    broken: list[dict[str, str]] = []
    checked = 0
    for source in files:
        if source.is_symlink() or source.stat().st_size > MAX_FILE_BYTES:
            broken.append({"file": source.relative_to(root).as_posix(), "target": "<unreadable>"})
            continue
        for target in _targets(source.read_text(encoding="utf-8", errors="replace")):
            verdict = _resolves(root, source, target)
            if verdict is None:
                continue
            checked += 1
            if not verdict:
                broken.append({"file": source.relative_to(root).as_posix(), "target": target})
    outcome = "COMPLETE" if files and not broken else "UNRESOLVED"
    return outcome, {"files_checked": len(files), "references_checked": checked, "broken": broken}


def main() -> int:
    try:
        outcome, report = check(Path.cwd())
    except (OSError, OverflowError) as exc:
        outcome, report = "UNRESOLVED", {"error": type(exc).__name__, "detail": str(exc)[:200]}
    sys.stdout.write(json.dumps({"schema_version": SCHEMA, "outcome": outcome}, sort_keys=True))
    sys.stderr.write(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
