# SPDX-License-Identifier: AGPL-3.0-only
"""Bind built package files to the exact source they were built from, or verify such a binding.

Usage:
  package_provenance.py record --platform P --dist DIR --expected-commit SHA
      Fails unless the checkout HEAD equals SHA and no tracked file differs from HEAD; then writes
      DIR/provenance-P.json (SOURCE_COMMIT, SOURCE_TREE, BUILD_PLATFORM, BUILD_TIMESTAMP, per-file
      PACKAGE_SHA256) and DIR/SHA256SUMS-P.txt (sha256sum format) for every *.tgz in DIR.
  package_provenance.py verify --platform P --dist DIR [--expected-commit SHA] [--expected-tree TREE]
      Recomputes every hash, checks the manifest against the provenance file and the expected identity.

The provenance is an unsigned, local statement: it proves which source a CI run packaged, not that the
package is trusted. Release promotion stays a human decision (docs/supply_chain/SECURE_CICD_V1.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "pados.package-provenance.v1"


class ProvenanceError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ProvenanceError("SOURCE_IDENTITY_UNPROVABLE", f"git {' '.join(args)} failed")
    return done.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _packages(dist: Path) -> list[Path]:
    files = sorted(path for path in dist.glob("*.tgz") if path.is_file())
    if not files:
        raise ProvenanceError("NO_PACKAGES", str(dist))
    return files


def source_identity(expected_commit: str) -> dict[str, str]:
    head = _git("rev-parse", "HEAD")
    if head != expected_commit:
        raise ProvenanceError("SOURCE_COMMIT_MISMATCH", f"checkout={head} expected={expected_commit}")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise ProvenanceError("SOURCE_TREE_MODIFIED", "tracked files differ from HEAD")
    return {"source_commit": head, "source_tree": _git("rev-parse", "HEAD^{tree}")}


def record(platform_name: str, dist: Path, expected_commit: str) -> dict[str, Any]:
    identity = source_identity(expected_commit)
    files = _packages(dist)
    statement: dict[str, Any] = {
        "schema": SCHEMA,
        "SOURCE_COMMIT": identity["source_commit"],
        "SOURCE_TREE": identity["source_tree"],
        "BUILD_PLATFORM": platform_name,
        "BUILD_HOST": f"{platform.system()}-{platform.machine()}",
        "BUILD_TIMESTAMP": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "PACKAGES": [{"file": path.name, "PACKAGE_SHA256": _sha256(path), "bytes": path.stat().st_size}
                     for path in files],
    }
    (dist / f"provenance-{platform_name}.json").write_text(
        json.dumps(statement, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (dist / f"SHA256SUMS-{platform_name}.txt").write_text(
        "".join(f"{item['PACKAGE_SHA256']}  {item['file']}\n" for item in statement["PACKAGES"]), encoding="utf-8")
    return statement


def verify(platform_name: str, dist: Path, expected_commit: str | None, expected_tree: str | None) -> dict[str, Any]:
    statement: dict[str, Any] = json.loads((dist / f"provenance-{platform_name}.json").read_text(encoding="utf-8"))
    if statement.get("schema") != SCHEMA or statement.get("BUILD_PLATFORM") != platform_name:
        raise ProvenanceError("PROVENANCE_INVALID")
    if expected_commit and statement["SOURCE_COMMIT"] != expected_commit:
        raise ProvenanceError("SOURCE_COMMIT_MISMATCH", statement["SOURCE_COMMIT"])
    if expected_tree and statement["SOURCE_TREE"] != expected_tree:
        raise ProvenanceError("SOURCE_TREE_MISMATCH", statement["SOURCE_TREE"])
    sums = {}
    for line in (dist / f"SHA256SUMS-{platform_name}.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        sums[name] = digest
    recorded = {item["file"]: item["PACKAGE_SHA256"] for item in statement["PACKAGES"]}
    if sums != recorded:
        raise ProvenanceError("MANIFEST_PROVENANCE_MISMATCH")
    for name, digest in recorded.items():
        if _sha256(dist / name) != digest:
            raise ProvenanceError("PACKAGE_HASH_MISMATCH", name)
    return statement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("record", "verify"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tree")
    args = parser.parse_args(argv)
    try:
        if args.mode == "record":
            if not args.expected_commit:
                raise ProvenanceError("EXPECTED_COMMIT_REQUIRED")
            statement = record(args.platform, args.dist, args.expected_commit)
        else:
            statement = verify(args.platform, args.dist, args.expected_commit, args.expected_tree)
    except (ProvenanceError, OSError, ValueError, KeyError) as error:
        print(f"PACKAGE_PROVENANCE_FAIL {getattr(error, 'code', type(error).__name__)} {error}")
        return 1
    print(f"PACKAGE_PROVENANCE_OK {args.mode} {args.platform} commit={statement['SOURCE_COMMIT']} "
          f"tree={statement['SOURCE_TREE']} packages={len(statement['PACKAGES'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
