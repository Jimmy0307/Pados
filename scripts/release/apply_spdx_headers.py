# SPDX-License-Identifier: AGPL-3.0-only
"""Check or apply SPDX-License-Identifier headers on PADOS-owned source files.

Usage:
    apply_spdx_headers.py [--root DIR] --check   exit 1 and list every file without the expected header
    apply_spdx_headers.py [--root DIR]           insert the missing headers (idempotent)

Scope (tracked files when ``DIR`` is a git checkout, otherwise a filesystem walk):

* ``src/**/*.py`` and ``scripts/**/*.py``          -> ``# SPDX-License-Identifier: AGPL-3.0-only``
* ``web/src/**/*.ts`` and ``web/src/**/*.tsx``      -> ``// SPDX-License-Identifier: AGPL-3.0-only``
* ``sdk/**`` (designated Apache-2.0 area, LICENSING.md) -> the same comment styles with ``Apache-2.0``

The header goes on the first line, after a shebang and/or a PEP 263 encoding line when present, and before any
module docstring, so it is never written inside a docstring. A UTF-8 BOM and the file's newline style are kept.
Third-party, vendored and generated paths are never touched. A file that already carries a different SPDX
identifier is reported as a mismatch and left unchanged (a human decides). Not legal advice.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CORE_LICENSE = "AGPL-3.0-only"
SDK_LICENSE = "Apache-2.0"
SDK_ROOT = "sdk/"
TAG = "SPDX-License-Identifier:"
BOM = b"\xef\xbb\xbf"
# Lines inspected for an existing identifier (shebang + encoding + header).
HEADER_WINDOW = 3

CORE_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("src/", (".py",), "#"),
    ("scripts/", (".py",), "#"),
    ("web/src/", (".ts", ".tsx"), "//"),
)
SDK_SUFFIXES: dict[str, str] = {".py": "#", ".ts": "//", ".tsx": "//", ".js": "//", ".mjs": "//"}
# Never touched: third-party, vendored, generated or build output.
EXCLUDED_PARTS = frozenset({
    "THIRD_PARTY_LICENSES", "third_party", "third-party", "vendor", "vendored", "node_modules", "dist", "build",
    "__pycache__", ".venv", "venv",
})
_SPDX_LINE = re.compile(r"^\s*(?:#|//)\s*SPDX-License-Identifier:\s*(\S.*?)\s*$")
_ENCODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")


@dataclass(frozen=True)
class Target:
    path: str  # repository-relative POSIX path
    license_id: str
    comment: str

    @property
    def header(self) -> str:
        return f"{self.comment} {TAG} {self.license_id}"


@dataclass(frozen=True)
class Finding:
    path: str
    status: str  # MISSING | MISMATCH
    detail: str


def classify(relative: str) -> Target | None:
    """The header a repository-relative path needs, or None when it is out of scope."""
    posix = PurePosixPath(relative)
    if any(part in EXCLUDED_PARTS for part in posix.parts):
        return None
    text = posix.as_posix()
    if text.startswith(SDK_ROOT):
        comment = SDK_SUFFIXES.get(posix.suffix)
        return Target(text, SDK_LICENSE, comment) if comment else None
    for prefix, suffixes, comment in CORE_RULES:
        if text.startswith(prefix) and posix.suffix in suffixes:
            return Target(text, CORE_LICENSE, comment)
    return None


def _is_checkout_root(root: Path) -> bool:
    """True only when ``root`` is itself the top level of a git work tree (not a folder inside one)."""
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    top = done.stdout.strip()
    return bool(top) and Path(top).resolve() == root.resolve()


def candidate_paths(root: Path) -> list[str]:
    """Tracked files when ``root`` is a git checkout, otherwise every file found by a walk."""
    if _is_checkout_root(root):
        done = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
        listed = [p for p in done.stdout.decode("utf-8").split("\0") if p]
        return sorted(p for p in listed if (root / p).is_file())
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_PARTS and d != ".git"]
        for name in filenames:
            found.append(Path(directory, name).relative_to(root).as_posix())
    return sorted(found)


def _split(data: bytes) -> tuple[bytes, str, str]:
    """(BOM, text, newline) of a UTF-8 source file."""
    bom = BOM if data.startswith(BOM) else b""
    text = data[len(bom):].decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    return bom, text, newline


def _prologue_length(lines: list[str], comment: str) -> int:
    """Number of leading lines that must stay above the header (shebang, PEP 263 encoding)."""
    count = 0
    if lines and lines[0].startswith("#!"):
        count = 1
    if comment == "#" and len(lines) > count and count < 2 and _ENCODING.match(lines[count]):
        count += 1
    return count


def existing_identifier(text: str) -> str | None:
    for line in text.splitlines()[:HEADER_WINDOW]:
        match = _SPDX_LINE.match(line)
        if match:
            return match.group(1)
    return None


def with_header(data: bytes, target: Target) -> bytes:
    """The file content with the header inserted; unchanged when any identifier is already present."""
    bom, text, newline = _split(data)
    if existing_identifier(text) is not None:
        return data
    lines = text.splitlines(keepends=True)
    keep = _prologue_length([line.rstrip("\r\n") for line in lines], target.comment)
    head = "".join(lines[:keep])
    if head and not head.endswith(("\n", "\r")):
        head += newline
    return bom + (head + target.header + newline + "".join(lines[keep:])).encode("utf-8")


def inspect(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in candidate_paths(root):
        target = classify(relative)
        if target is None:
            continue
        _, text, _ = _split((root / relative).read_bytes())
        found = existing_identifier(text)
        if found is None:
            findings.append(Finding(relative, "MISSING", target.license_id))
        elif found != target.license_id:
            findings.append(Finding(relative, "MISMATCH", f"found {found}, expected {target.license_id}"))
    return findings


def apply(root: Path) -> list[str]:
    """Insert missing headers; returns the changed paths. Mismatches are never rewritten."""
    changed: list[str] = []
    for relative in candidate_paths(root):
        target = classify(relative)
        if target is None:
            continue
        path = root / relative
        before = path.read_bytes()
        after = with_header(before, target)
        if after != before:
            path.write_bytes(after)
            changed.append(relative)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--check", action="store_true", help="report files without the expected header")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.check:
        findings = inspect(root)
        for finding in findings:
            print(f"{finding.status} {finding.path} ({finding.detail})")
        print(f"spdx-check findings={len(findings)}")
        return 1 if findings else 0
    changed = apply(root)
    for relative in changed:
        print(f"ADDED {relative}")
    remaining = [f for f in inspect(root) if f.status == "MISMATCH"]
    for finding in remaining:
        print(f"MISMATCH {finding.path} ({finding.detail})")
    print(f"spdx-apply changed={len(changed)} mismatches={len(remaining)}")
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
