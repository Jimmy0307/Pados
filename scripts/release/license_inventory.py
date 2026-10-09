# SPDX-License-Identifier: AGPL-3.0-only
"""Dependency license inventory from installed package metadata (not legal advice).

Usage: license_inventory.py OUT.json. Python: requirements.lock pins resolved via
importlib.metadata. JavaScript: web/package-lock.json packages read from
web/node_modules/<name>/package.json.
"""
from __future__ import annotations

import json
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW = re.compile(r"GPL|AGPL|LGPL|SSPL|BUSL|Commons Clause|UNKNOWN|MPL", re.IGNORECASE)


def python_licenses() -> list[dict[str, str]]:
    rows = []
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8-sig").splitlines():
        name, _, version = line.strip().partition("==")
        if not name or name.startswith("#"):
            continue
        try:
            meta = metadata.metadata(name)
            expr = meta.get("License-Expression") or ""
            classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
            raw = (meta.get("License") or "").splitlines()[0][:80] if meta.get("License") else ""
            license_name = expr or "; ".join(classifiers) or raw or "UNKNOWN"
        except metadata.PackageNotFoundError:
            license_name = "NOT_INSTALLED"
        rows.append({"ecosystem": "python", "name": name, "version": version, "license": license_name,
                     "review": bool(REVIEW.search(license_name))})
    return rows


def npm_licenses() -> list[dict[str, str]]:
    lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    rows = []
    for key, entry in sorted(lock.get("packages", {}).items()):
        if not key.startswith("node_modules/"):
            continue
        name = key.removeprefix("node_modules/")
        package = ROOT / "web" / key / "package.json"
        license_name = entry.get("license")
        if not license_name and package.exists():
            license_name = json.loads(package.read_text(encoding="utf-8")).get("license")
        license_name = str(license_name or "UNKNOWN")
        rows.append({"ecosystem": "npm", "name": name, "version": str(entry.get("version")), "license": license_name,
                     "dev": bool(entry.get("dev")), "review": bool(REVIEW.search(license_name))})
    return rows


def main() -> int:
    rows = python_licenses() + npm_licenses()
    summary: dict[str, int] = {}
    for row in rows:
        summary[row["license"]] = summary.get(row["license"], 0) + 1
    report = {"note": "Automated metadata inventory; not legal advice. Items with review=true need human/legal review.",
              "packages": rows, "summary": dict(sorted(summary.items(), key=lambda kv: -kv[1])),
              "review_required": [r for r in rows if r["review"]]}
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"packages={len(rows)} review_required={len(report['review_required'])}")
    for row in report["review_required"]:
        print(f"REVIEW {row['ecosystem']} {row['name']} {row['version']} {row['license']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
