# SPDX-License-Identifier: AGPL-3.0-only
"""Materialize the public export set of one commit into a directory, then re-scan it (nothing is pushed).

Usage: python scripts/release/build_public_export.py <commit> <out-dir> [--repo PATH] [--no-private-markers]

The export set is exactly what ``public_export_check.py`` computes from ``public_export_manifest.json`` AT that
commit: tracked files minus the manifest's excluded patterns. Files are written byte-for-byte from the commit's
blobs (never from the working tree). ``<out-dir>`` must not exist.

Fail closed:

* The untracked private marker file (``PADOS_PRIVATE_MARKERS_FILE`` or ``runtime/cache/release/private_markers.json``)
  is required. Without it the build is refused, unless ``--no-private-markers`` is passed explicitly, which prints
  a loud warning and is recorded in the manifest (organisation/personal markers were NOT checked).
* After materializing, the produced tree is scanned with the export-check rules (incl. private markers) classified
  by the manifest at the commit, and with the repository secret scanner classified by the commit's
  ``secret_scan_allowlist.json``. Any unclassified finding or listed release blocker fails the build (exit 1).

Writes ``<out-dir>.manifest.json`` next to the tree: commit, tree, file count, per-file sha256 and git mode, and the
scan results (paths, rules and counts only; matched values are never recorded).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "pados.release.public-export-tree.v2"
SUPPORTED_MODES = frozenset({"100644", "100755"})
WARNING = ("WARNING: --no-private-markers: building WITHOUT the private marker file. Organisation, personal and "
           "private-model markers are NOT checked; this export must not be published until it is rebuilt with "
           "the marker file present.")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo,  # noqa: S603,S607
                          capture_output=True, check=True).stdout


def _load_check_module(manifest: dict[str, Any]) -> ModuleType:
    """``public_export_check`` (rules + private markers) with the manifest AT the commit."""
    spec = importlib.util.spec_from_file_location("public_export_check", ROOT / "scripts/release/public_export_check.py")
    if spec is None or spec.loader is None:
        raise SystemExit("PUBLIC_EXPORT_CHECK_NOT_LOADABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MANIFEST = manifest
    return module


def _tree_entries(repo: Path, commit: str) -> dict[str, str]:
    """path -> git mode of every tracked file at ``commit``."""
    entries: dict[str, str] = {}
    for record in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).decode("utf-8").split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            entries[path] = meta.split(" ", 1)[0]
    return entries


def _safe_parts(path: str) -> tuple[str, ...]:
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts or any(c in path for c in "\n\r\\"):
        raise SystemExit(f"UNSAFE_PATH:{path}")
    return posix.parts


def read_blobs(repo: Path, objects: list[str]) -> list[bytes]:
    """Raw bytes of each object name (``<sha>`` or ``<commit>:<path>``) via one ``git cat-file --batch``."""
    if any("\n" in name for name in objects):
        raise SystemExit("UNSAFE_OBJECT_NAME")
    stdout = _git_input(repo, "".join(f"{name}\n" for name in objects).encode("utf-8"), "cat-file", "--batch")
    blobs: list[bytes] = []
    offset = 0
    for name in objects:
        end = stdout.index(b"\n", offset)
        header = stdout[offset:end].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            raise SystemExit(f"NOT_A_BLOB:{name}")
        size = int(header[2])
        blobs.append(stdout[end + 1:end + 1 + size])
        offset = end + 1 + size + 1
    return blobs


def _git_input(repo: Path, data: bytes, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo, input=data,  # noqa: S603,S607
                          capture_output=True, check=True).stdout


def _materialize(repo: Path, commit: str, kept: list[str], modes: dict[str, str], out: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in kept:
        if modes[path] not in SUPPORTED_MODES:
            raise SystemExit(f"UNSUPPORTED_TREE_ENTRY:{modes[path]}:{path}")
    for path, data in zip(kept, read_blobs(repo, [f"{commit}:{path}" for path in kept]), strict=True):
        target = out.joinpath(*_safe_parts(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[path] = hashlib.sha256(data).hexdigest()
    return digests


def _scan(repo: Path, commit: str, out: Path, kept: list[str], check: ModuleType) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from pados.release.secret_scan import load_allowlist, scan_files

    export_findings = check.scan_tree(out, kept)
    export_summary = check.summarize(export_findings)
    allowlist = load_allowlist(json.loads(_git(repo, "show", f"{commit}:scripts/release/secret_scan_allowlist.json")))
    secret_findings = [f.as_record() for f in scan_files(out, kept, allowlist)]
    secret_unclassified = [f for f in secret_findings if f["classification"] == "UNCLASSIFIED"]
    failed = bool(export_summary["unclassified"] or export_summary["release_blockers"] or secret_unclassified)
    return {"status": "FAIL" if failed else "PASS",
            "export_check": {"findings": len(export_findings), **export_summary},
            "secret_scan": {"findings": len(secret_findings), "unclassified": secret_unclassified},
            "values_recorded": False}


def build(commit_ref: str, out: Path, *, repo: Path = ROOT, allow_no_private_markers: bool = False) -> int:
    """Materialize and scan; returns the process exit code (0 PASS, 1 scan failure, 2 refused)."""
    commit = _git(repo, "rev-parse", "--verify", f"{commit_ref}^{{commit}}").decode().strip()
    if out.exists():
        print("REFUSED: output directory exists", file=sys.stderr)
        return 2
    manifest = json.loads(_git(repo, "show", f"{commit}:scripts/release/public_export_manifest.json"))
    check = _load_check_module(manifest)
    markers_loaded = bool(check.PRIVATE_MARKERS.is_file())
    if not markers_loaded and not allow_no_private_markers:
        print("REFUSED: private marker file missing (set PADOS_PRIVATE_MARKERS_FILE, or pass --no-private-markers "
              "explicitly to build an unpublishable export)", file=sys.stderr)
        return 2
    if not markers_loaded:
        print("!" * 100 + "\n" + WARNING + "\n" + "!" * 100, file=sys.stderr)
    modes = _tree_entries(repo, commit)
    kept, dropped = check.export_set(sorted(modes))
    digests = _materialize(repo, commit, kept, modes, out)
    scans = _scan(repo, commit, out, kept, check)
    record = {"schema": SCHEMA, "commit": commit, "tree": _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip(),
              "export_files": len(kept), "excluded_files": len(dropped), "files": digests,
              "modes": {path: modes[path] for path in kept},
              "private_marker_rules_loaded": markers_loaded,
              "private_marker_rule_names": sorted(set(check.RULES) - {"local_user_path", "machine_path",
                                                                       "private_ipv4", "email"}),
              "no_private_markers_override": not markers_loaded, "scans": scans}
    out.with_name(out.name + ".manifest.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                                          encoding="utf-8")
    for finding in [*scans["export_check"]["unclassified"], *scans["secret_scan"]["unclassified"]]:
        print(f"UNCLASSIFIED {finding['path']} {finding['rule']}", file=sys.stderr)
    print(f"export_files={len(kept)} excluded={len(dropped)} commit={commit} scan={scans['status']} "
          f"private_markers={'loaded' if markers_loaded else 'NOT_LOADED'}")
    print("PUBLIC_EXPORT_BUILD_OK" if scans["status"] == "PASS" else "PUBLIC_EXPORT_BUILD_SCAN_FAILED")
    return 0 if scans["status"] == "PASS" else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commit")
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--repo", type=Path, default=ROOT, help="internal repository (default: this checkout)")
    parser.add_argument("--no-private-markers", action="store_true",
                        help="permit a build without the private marker file (loud warning; recorded)")
    args = parser.parse_args(argv)
    return build(args.commit, args.out_dir, repo=args.repo, allow_no_private_markers=args.no_private_markers)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
