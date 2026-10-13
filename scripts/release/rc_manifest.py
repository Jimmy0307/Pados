# SPDX-License-Identifier: AGPL-3.0-only
"""RC manifest: bind the RC CI packages of every platform to the single commit and tree RC CI tested.

Usage:
  rc_manifest.py write  --rc-dir DIR --expected-commit SHA
      DIR holds one sub-directory per platform (each with provenance-<platform>.json, SHA256SUMS-<platform>.txt and
      the tarballs). Verifies every platform's provenance against SHA and against the checkout's own tree (which
      must be unmodified), then writes DIR/rc-manifest.json. RC_WORKFLOW / RC_RUN_ID / RC_RUN_ATTEMPT / RC_EVENT /
      RC_REF from the environment record which RC CI run produced it; the event must be an RC event.
  rc_manifest.py verify --rc-dir DIR [--expected-commit SHA] [--expected-tree TREE]
      Re-verifies a downloaded RC artifact before any install (hashes, provenance, manifest agreement).

An RC manifest is evidence that RC CI built these bytes from that tree after its gates passed. It authorizes
nothing: release promotion additionally needs a PASS live acceptance of exactly this commit
(scripts/release/promotion_check.py) and a human decision.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_provenance import ProvenanceError, _git, verify  # noqa: E402

SCHEMA = "pados.rc-manifest.v1"
PLATFORMS = ("linux-x64", "win32-x64")
RC_EVENTS = frozenset({"workflow_dispatch", "push"})


def _platform_dirs(rc_dir: Path) -> dict[str, Path]:
    found = {platform: rc_dir / platform for platform in PLATFORMS if (rc_dir / platform).is_dir()}
    if set(found) != set(PLATFORMS):
        raise ProvenanceError("RC_PLATFORM_MISSING", ",".join(sorted(set(PLATFORMS) - set(found))))
    return found


def write(rc_dir: Path, expected_commit: str) -> dict[str, Any]:
    event, ref = os.environ.get("RC_EVENT", ""), os.environ.get("RC_REF", "")
    if event not in RC_EVENTS or (event == "push" and not ref.startswith("refs/heads/rc/")):
        raise ProvenanceError("NOT_AN_RC_EVENT", f"{event} {ref}")
    if _git("rev-parse", "HEAD") != expected_commit or _git("status", "--porcelain", "--untracked-files=no"):
        raise ProvenanceError("SOURCE_IDENTITY_UNPROVABLE", "checkout is not the unmodified RC commit")
    tree = _git("rev-parse", "HEAD^{tree}")
    platforms = {
        platform: verify(platform, directory, expected_commit, tree)
        for platform, directory in _platform_dirs(rc_dir).items()
    }
    manifest = {
        "schema": SCHEMA,
        "RC_COMMIT": expected_commit,
        "RC_TREE": tree,
        "produced_by": {key.lower(): os.environ.get(key, "") for key in
                        ("RC_WORKFLOW", "RC_RUN_ID", "RC_RUN_ATTEMPT", "RC_EVENT", "RC_REF")},
        "gates": ["pr-ci checks", "postgres integration", "fresh-install migration", "packaging install acceptance"],
        "platforms": {platform: {"BUILD_TIMESTAMP": statement["BUILD_TIMESTAMP"],
                                 "packages": statement["PACKAGES"]} for platform, statement in platforms.items()},
        "authorizes": "NOTHING (evidence only; promotion needs live acceptance and a human decision)",
    }
    (rc_dir / "rc-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                             encoding="utf-8", newline="\n")
    return manifest


def check(rc_dir: Path, expected_commit: str | None, expected_tree: str | None) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((rc_dir / "rc-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ProvenanceError("RC_MANIFEST_INVALID")
    if expected_commit and manifest["RC_COMMIT"] != expected_commit:
        raise ProvenanceError("RC_COMMIT_MISMATCH", manifest["RC_COMMIT"])
    if expected_tree and manifest["RC_TREE"] != expected_tree:
        raise ProvenanceError("RC_TREE_MISMATCH", manifest["RC_TREE"])
    for platform, directory in _platform_dirs(rc_dir).items():
        statement = verify(platform, directory, manifest["RC_COMMIT"], manifest["RC_TREE"])
        if statement["PACKAGES"] != manifest["platforms"][platform]["packages"]:
            raise ProvenanceError("RC_MANIFEST_PACKAGE_MISMATCH", platform)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("write", "verify"))
    parser.add_argument("--rc-dir", required=True, type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tree")
    args = parser.parse_args(argv)
    try:
        if args.mode == "write":
            if not args.expected_commit:
                raise ProvenanceError("EXPECTED_COMMIT_REQUIRED")
            manifest = write(args.rc_dir, args.expected_commit)
        else:
            manifest = check(args.rc_dir, args.expected_commit, args.expected_tree)
    except (ProvenanceError, OSError, ValueError, KeyError) as error:
        print(f"RC_MANIFEST_FAIL {getattr(error, 'code', type(error).__name__)} {error}")
        return 1
    print(f"RC_MANIFEST_OK {args.mode} commit={manifest['RC_COMMIT']} tree={manifest['RC_TREE']} "
          f"platforms={','.join(sorted(manifest['platforms']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
