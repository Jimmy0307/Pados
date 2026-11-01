# SPDX-License-Identifier: AGPL-3.0-only
"""Lightweight documentation lint (OP-010): references in operator docs must resolve in the tree.

Two checks, both mechanical, so a finding is always a real dangling reference and never a style opinion:

* ``LINK_TARGET_MISSING``: a relative Markdown link ``[text](path)`` names a file that does not exist;
* ``CODE_PATH_MISSING``: a backticked repository path (``src/...``, ``tests/...``, ``scripts/...``, ``docs/...``,
  ``web/...``, ``packages/...``) names a file or directory that does not exist.

Placeholders (``<sha>``, ``*``, ``…``) and paths under ``runtime/`` (machine-local evidence) are not checked. A doc
whose purpose is to name paths that are deliberately absent (an export exclusion list) carries the marker
``<!-- doc-lint: names-absent-paths -->``; that turns off only its ``CODE_PATH_MISSING`` check, visibly in the doc.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`\s]+)`")
_REPO_PREFIXES = ("src/", "tests/", "scripts/", "docs/", "web/", "packages/", ".github/")
_PLACEHOLDER = re.compile(r"[<>*…{}$]|\.\.\.")
_FENCE = re.compile(r"^\s*(```|~~~)")
ABSENT_PATHS_MARKER = "<!-- doc-lint: names-absent-paths -->"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    target: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.rule} {self.target}"


def _prose_lines(text: str) -> Iterable[tuple[int, str]]:
    """Lines outside fenced code blocks (commands and examples there may name files that do not exist yet)."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield number, line


_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


def _clean(target: str) -> str:
    """Drop anchors, pytest node ids and ``:line`` / ``:start-end`` suffixes: the file is what must exist."""
    return _LINE_SUFFIX.sub("", target.split("#", 1)[0].split("::", 1)[0].rstrip(".,;:"))


def _repo_path_exists(root: Path, target: str) -> bool:
    if (root / target).exists():
        return True
    # A dotted Python symbol (``tests/x/module.Name``) resolves through its module file.
    module, _, symbol = target.rpartition(".")
    return bool(module and symbol.isidentifier() and (root / f"{module}.py").is_file())


def lint_file(root: Path, doc: Path) -> list[Finding]:
    relative = doc.relative_to(root).as_posix()
    text = doc.read_text(encoding="utf-8")
    check_code_paths = ABSENT_PATHS_MARKER not in text
    findings: list[Finding] = []
    for number, line in _prose_lines(text):
        for raw in _LINK.findall(line):
            target = _clean(raw)
            if not target or "://" in raw or raw.startswith(("#", "mailto:")) or _PLACEHOLDER.search(target):
                continue
            if not (doc.parent / target).exists():
                findings.append(Finding(relative, number, "LINK_TARGET_MISSING", raw))
        for raw in _CODE.findall(line) if check_code_paths else ():
            target = _clean(raw)
            if not target.startswith(_REPO_PREFIXES) or _PLACEHOLDER.search(target):
                continue
            if not _repo_path_exists(root, target):
                findings.append(Finding(relative, number, "CODE_PATH_MISSING", raw))
    return findings


def lint(root: Path, docs: Sequence[Path]) -> list[Finding]:
    return [finding for doc in docs for finding in lint_file(root, doc)]


def default_docs(root: Path) -> list[Path]:
    """Every Markdown doc under ``docs/`` plus the root operator runbooks."""
    docs = sorted((root / "docs").rglob("*.md"))
    return docs + [path for path in (root / "PADOS_WEB_RUNBOOK.md", root / "README.md") if path.is_file()]
