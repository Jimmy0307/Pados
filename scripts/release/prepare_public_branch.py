# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare (never push) a public-repository branch whose tree is exactly the public export of an internal commit.

Usage:
  python scripts/release/prepare_public_branch.py <internal-commit> <public-clone> --branch NAME --message MSG
         [--repo INTERNAL] [--base origin/main] [--export-dir DIR] [--no-private-markers]

Steps (all local):

1. ``build_public_export.build`` materializes and re-scans the export of ``<internal-commit>`` (refused without the
   private marker file unless ``--no-private-markers``; any unclassified finding fails).
2. In ``<public-clone>`` a NEW local branch ``refs/heads/NAME`` is created on top of ``--base`` (default
   ``origin/main``; it must resolve to a remote-tracking ref). The commit's tree is the export set and nothing else:
   blobs are written with ``git hash-object --no-filters`` and ``core.autocrlf=false`` into a temporary index, so the
   bytes are exactly the internal blobs whatever the clone's attributes say. The clone's working tree, HEAD, index
   and every existing ref are left untouched; an existing branch NAME is refused.
3. Every blob of the new tree is read back and its sha256 compared with the export manifest; paths and modes must
   match exactly. The remote-tracking refs are compared before and after.
4. The commit and tree ids are printed. Publishing (``git push``) is a separate, human action and needs its own
   credential; a push of ``.github/workflows/*`` additionally needs a token with the ``workflow`` scope.

Commit metadata is public history too (red-team F-8): author AND committer are a fixed public identity
(``PADOS Release <release@local.invalid>`` unless ``--identity "Name <email>"`` names another one with an ``.invalid``
or GitHub noreply address), dates are UTC, and the commit message and identity are scanned with the same export-check
rules (private markers included) and the secret-scan rules before anything is written; any finding refuses.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_IDENTITY = "PADOS Release <release@local.invalid>"
_IDENTITY = re.compile(r"^([A-Za-z][A-Za-z0-9 ._-]{0,63}) <([a-z0-9][a-z0-9._+-]{0,63}"
                       r"@(?:(?:[a-z0-9-]{1,63}\.)+invalid|users\.noreply\.github\.com))>$")
MAX_MESSAGE_CHARS = 4096


def public_identity(identity: str) -> tuple[str, str]:
    """``Name <email>`` with a non-routable (``.invalid``) or GitHub noreply address; anything else is refused."""
    match = _IDENTITY.fullmatch(identity)
    if match is None:
        raise SystemExit("REFUSED: the public commit identity must be 'Name <email>' with an .invalid "
                         "or a GitHub noreply address")
    return match.group(1), match.group(2)


def metadata_findings(message: str, name: str, email: str, check: ModuleType) -> list[str]:
    """Rule names (never values) that the commit message or identity trips: export-check rules incl. the private
    markers, plus the secret-scan rules. The e-mail rule is not applied to the already validated address."""
    sys.path.insert(0, str(ROOT / "src"))
    from pados.release.secret_scan import scan_text

    rules: dict[str, re.Pattern[str]] = check.RULES
    text = f"{name}\n{message}"
    found = {rule for rule, pattern in rules.items() if pattern.search(text)}
    found |= {rule for rule, pattern in rules.items() if rule != "email" and pattern.search(email)}
    found |= {str(rule) for _line, rule in scan_text("COMMIT_MESSAGE", text)}
    return sorted(found)


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_public_export", ROOT / "scripts/release/build_public_export.py")
    if spec is None or spec.loader is None:
        raise SystemExit("BUILD_PUBLIC_EXPORT_NOT_LOADABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
    done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],  # noqa: S603,S607
                          cwd=repo, input=data, env=env, capture_output=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"GIT_FAILED: git {' '.join(args[:2])}: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def _remote_refs(clone: Path) -> bytes:
    return _git(clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes")


def _check_clone(clone: Path, internal: Path, branch: str, base: str) -> str:
    top = Path(_git(clone, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top == Path(_git(internal, "rev-parse", "--show-toplevel").decode().strip()).resolve():
        raise SystemExit("REFUSED: the public clone is the internal repository")
    _git(clone, "check-ref-format", "--branch", branch)
    if subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],  # noqa: S603,S607
                      cwd=clone, check=False).returncode == 0:
        raise SystemExit(f"REFUSED: branch {branch} already exists in the public clone")
    symbolic = _git(clone, "rev-parse", "--symbolic-full-name", base).decode().strip()
    if not symbolic.startswith("refs/remotes/"):
        raise SystemExit(f"REFUSED: base {base} is not a remote-tracking ref")
    return _git(clone, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()


def _write_tree(clone: Path, export_dir: Path, record: dict[str, object], work: Path) -> str:
    files: dict[str, str] = record["files"]  # type: ignore[assignment]
    modes: dict[str, str] = record["modes"]  # type: ignore[assignment]
    paths = sorted(files)
    names = "".join(f"{(export_dir / p).resolve()}\n" for p in paths).encode("utf-8")
    shas = _git(clone, "hash-object", "-w", "--no-filters", "--stdin-paths", data=names).decode().split()
    if len(shas) != len(paths):
        raise SystemExit("HASH_OBJECT_COUNT_MISMATCH")
    env = {**os.environ, "GIT_INDEX_FILE": str(work / "public.index")}
    info = b"".join(f"{modes[p]} {sha}\t{p}".encode() + b"\0" for p, sha in zip(paths, shas, strict=True))
    _git(clone, "update-index", "-z", "--index-info", data=info, env=env)
    return _git(clone, "write-tree", env=env).decode().strip()


def _verify_tree(clone: Path, tree: str, record: dict[str, object], builder: ModuleType) -> None:
    files: dict[str, str] = record["files"]  # type: ignore[assignment]
    modes: dict[str, str] = record["modes"]  # type: ignore[assignment]
    entries: dict[str, tuple[str, str]] = {}
    for item in _git(clone, "ls-tree", "-r", "-z", "--full-tree", tree).decode("utf-8").split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            mode, _kind, sha = meta.split(" ")
            entries[path] = (mode, sha)
    if sorted(entries) != sorted(files):
        raise SystemExit("TREE_PATHS_DIFFER_FROM_EXPORT_MANIFEST")
    paths = sorted(entries)
    for path, data in zip(paths, builder.read_blobs(clone, [entries[p][1] for p in paths]), strict=True):
        if hashlib.sha256(data).hexdigest() != files[path] or entries[path][0] != modes[path]:
            raise SystemExit(f"BLOB_MISMATCH:{path}")


def prepare(commit: str, clone: Path, *, branch: str, message: str, repo: Path = ROOT, base: str = "origin/main",
            export_dir: Path | None = None, allow_no_private_markers: bool = False,
            identity: str = PUBLIC_IDENTITY) -> dict[str, object]:
    builder = _builder()
    name, email = public_identity(identity)
    if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        raise SystemExit("REFUSED: the commit message is empty or too long")
    check = builder._load_check_module({})  # the rules only (private markers included when the file is present)
    if not check.PRIVATE_MARKERS.is_file() and not allow_no_private_markers:
        raise SystemExit("REFUSED: private marker file missing; the commit message cannot be scanned")
    findings = metadata_findings(message, name, email, check)
    if findings:
        raise SystemExit("REFUSED: the commit message or identity failed the export scan: rules=" + ",".join(findings))
    base_commit = _check_clone(clone, repo, branch, base)
    before = _remote_refs(clone)
    work = Path(tempfile.mkdtemp(prefix="pados-public-branch-"))
    try:
        out = export_dir if export_dir is not None else work / "export"
        code = builder.build(commit, out, repo=repo, allow_no_private_markers=allow_no_private_markers)
        if code != 0:
            raise SystemExit(f"REFUSED: public export build failed (exit {code})")
        record = json.loads(out.with_name(out.name + ".manifest.json").read_text(encoding="utf-8"))
        if record["scans"]["status"] != "PASS":
            raise SystemExit("REFUSED: export scans did not pass")
        tree = _write_tree(clone, out, record, work)
        _verify_tree(clone, tree, record, builder)
        stamp = f"{int(time.time())} +0000"  # UTC: no local timezone in public history
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp}
        new_commit = _git(clone, "commit-tree", tree, "-p", base_commit, "-F", "-",
                          data=message.encode("utf-8"), env=env).decode().strip()
        # The all-zero old value makes update-ref create the branch only if it does not exist.
        _git(clone, "update-ref", f"refs/heads/{branch}", new_commit, "0" * len(new_commit))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if _remote_refs(clone) != before:
        raise SystemExit("REMOTE_TRACKING_REFS_CHANGED")
    return {"branch": branch, "commit": new_commit, "tree": tree, "base": base_commit,
            "internal_commit": record["commit"], "export_files": record["export_files"],
            "private_marker_rules_loaded": record["private_marker_rules_loaded"], "identity": f"{name} <{email}>",
            "pushed": False}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("commit")
    parser.add_argument("public_clone", type=Path)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--export-dir", type=Path, default=None, help="keep the materialized export here (must not exist)")
    parser.add_argument("--no-private-markers", action="store_true")
    parser.add_argument("--identity", default=PUBLIC_IDENTITY, help="public author/committer 'Name <email>'")
    args = parser.parse_args(argv)
    result = prepare(args.commit, args.public_clone, branch=args.branch, message=args.message, repo=args.repo,
                     base=args.base, export_dir=args.export_dir, allow_no_private_markers=args.no_private_markers,
                     identity=args.identity)
    print(json.dumps(result, sort_keys=True))
    print(f"PUBLIC_BRANCH_PREPARED commit={result['commit']} tree={result['tree']} (not pushed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
