# SPDX-License-Identifier: AGPL-3.0-only
"""Compute the public export set and scan it (read-only; nothing is published).

Usage: python scripts/release/public_export_check.py OUT.json
Exit 0 when every finding in the export set is classified as allowed or as a
listed release blocker; exit 1 when an unclassified finding exists.

``export_set``, ``scan_tree`` and ``summarize`` are also used by
``build_public_export.py`` to re-scan the materialized export tree.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "scripts/release/public_export_manifest.json").read_text(encoding="utf-8"))
RULES = {
    "local_user_path": re.compile(r"/home/[a-z][a-z0-9_-]*/|[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z]"),
    "machine_path": re.compile(r"[A-Za-z]:[\\/]+PADOS"),
    "private_ipv4": re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.(com|org|invalid)\b)(?!local\.invalid\b)(?!anthropic\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}
DEFAULT_PRIVATE_MARKERS = ROOT / "runtime/cache/release/private_markers.json"


def private_markers_path() -> Path:
    """The untracked private marker file (``PADOS_PRIVATE_MARKERS_FILE`` or the default cache location)."""
    return Path(os.environ.get("PADOS_PRIVATE_MARKERS_FILE", DEFAULT_PRIVATE_MARKERS))


def load_private_markers(path: Path) -> dict[str, re.Pattern[str]]:
    """Organisation/personal marker patterns are private data themselves, so they are never stored in this
    public script; they are loaded from an untracked local file mapping a rule name to a regex."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("PRIVATE_MARKERS_INVALID")
    return {str(rule): re.compile(str(pattern), re.IGNORECASE) for rule, pattern in raw.items()}


PRIVATE_MARKERS = private_markers_path()
if PRIVATE_MARKERS.is_file():
    RULES.update(load_private_markers(PRIVATE_MARKERS))


def _match(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern) or (pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"))


def export_set(files: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for path in files:
        rule = next((r for r in MANIFEST["exclude"] if _match(path, r["pattern"])), None)
        keep = rule is not None and any(_match(path, k) for k in rule.get("keep", []))
        (dropped if rule is not None and not keep else kept).append(path)
    return kept, dropped


def classify(path: str, rule: str) -> str:
    for entry in MANIFEST["allowed_findings"]:
        if _match(path, entry["path"]) and rule in entry["rules"]:
            return "ALLOWED"
    for entry in MANIFEST["release_blocker_findings"]:
        if _match(path, entry["path"]) and rule in entry["rules"]:
            return str(entry["blocker"])
    return "UNCLASSIFIED"


def scan_tree(root: Path, paths: list[str], rules: dict[str, re.Pattern[str]] | None = None) -> list[dict[str, object]]:
    """Findings for ``paths`` under ``root``: path, rule, count and classification only, never matched values."""
    sys.path.insert(0, str(ROOT / "src"))
    from pados.release.package import _scan_secret

    active = RULES if rules is None else rules
    findings: list[dict[str, object]] = []
    for path in paths:
        full = root / path
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rule, pattern in active.items():
            count = len(pattern.findall(text))
            if count:
                findings.append({"path": path, "rule": rule, "count": count, "classification": classify(path, rule)})
        if _scan_secret(full) and not path.startswith("scripts/release/"):
            findings.append({"path": path, "rule": "secret_like", "count": 1,
                             "classification": classify(path, "secret_like")})
    return findings


def summarize(findings: list[dict[str, object]]) -> dict[str, object]:
    summary: dict[str, int] = {}
    for item in findings:
        key = str(item["classification"])
        summary[key] = summary.get(key, 0) + 1
    return {"summary": summary,
            "unclassified": [f for f in findings if f["classification"] == "UNCLASSIFIED"],
            "release_blockers": sorted({str(f["classification"]) for f in findings
                                        if f["classification"] not in {"ALLOWED", "UNCLASSIFIED"}})}


def main() -> int:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    files = [f for f in tracked.decode("utf-8").split("\0") if f]
    kept, dropped = export_set(files)
    findings = scan_tree(ROOT, kept)
    excluded_by_top_level: dict[str, int] = {}
    for path in dropped:
        top = path.split("/", 1)[0]
        excluded_by_top_level[top] = excluded_by_top_level.get(top, 0) + 1
    report: dict[str, object] = {"private_marker_rules_loaded": PRIVATE_MARKERS.is_file(), "tracked_files": len(files),
                                 "export_files": len(kept), "excluded_files": len(dropped),
                                 "excluded_by_top_level": excluded_by_top_level, "findings": findings,
                                 **summarize(findings)}
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"export_files={len(kept)} excluded={len(dropped)} findings={len(findings)} summary={report['summary']}")
    print("PUBLIC_EXPORT_CLASSIFIED_OK" if not report["unclassified"] else "PUBLIC_EXPORT_UNCLASSIFIED_FINDINGS")
    return 0 if not report["unclassified"] else 1


if __name__ == "__main__":
    sys.exit(main())
