# SPDX-License-Identifier: AGPL-3.0-only
"""Distribution-oriented dependency inventory (not legal advice).

Classifies every locked dependency by how each public artifact consumes it:
runtime (installed by the user, not bundled), build/dev-only, or bundled into
the frontend bundle. PADOS vendors no third-party source and modifies none.
Usage: dependency_distribution.py OUT.json
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW = re.compile(r"GPL|MPL|Mozilla|SSPL|BUSL|Commons Clause|UNKNOWN|CC-BY", re.IGNORECASE)
# Code that ends up inside web/dist (checked against the built bundle).
FRONTEND_BUNDLED = {"react", "react-dom", "scheduler", "vite"}
WINDOWS_ONLY_KNOWN = {"colorama": "BSD-3-Clause (upstream; Windows-only marker)",
                      "tzdata": "Apache-2.0 (upstream; Windows-only marker)"}


def _name(requirement: str) -> str:
    return re.split(r"[\s;<>=!~\[(]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _license(dist_name: str) -> str:
    try:
        meta = metadata.metadata(dist_name)
    except metadata.PackageNotFoundError:
        return WINDOWS_ONLY_KNOWN.get(dist_name, "NOT_INSTALLED")
    expr = meta.get("License-Expression")
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    raw = (meta.get("License") or "").splitlines()[0][:60] if meta.get("License") else ""
    return expr or "; ".join(classifiers) or raw or "UNKNOWN"


def runtime_closure(roots: list[str]) -> set[str]:
    """Transitive runtime closure; extras are followed only where a root requests them."""
    seen: set[str] = set()
    stack = [(_name(r), frozenset(re.findall(r"\[([^\]]+)\]", r)[0].split(",")) if "[" in r else frozenset())
             for r in roots]
    while stack:
        name, extras = stack.pop()
        if name in seen and not extras:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for requirement in requires:
            match = re.search(r"extra\s*==\s*[\"']([^\"']+)[\"']", requirement)
            if match and match.group(1) not in extras:
                continue
            stack.append((_name(requirement), frozenset()))
    return seen


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = runtime_closure(project["dependencies"])
    rows = []
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8-sig").splitlines():
        name, _, version = line.strip().partition("==")
        if not name:
            continue
        key = name.lower().replace("_", "-")
        scope = "RUNTIME_DEPENDENCY" if key in runtime else "DEV_OR_BUILD_ONLY"
        lic = _license(key)
        rows.append({"ecosystem": "python", "name": name, "version": version, "license": lic, "scope": scope,
                     "bundled_in": [], "vendored": False, "modified": False, "review": bool(REVIEW.search(lic))})
    lock = json.loads((ROOT / "web/package-lock.json").read_text(encoding="utf-8"))
    for path, entry in sorted(lock.get("packages", {}).items()):
        if not path.startswith("node_modules/"):
            continue
        name = path.removeprefix("node_modules/")
        lic = str(entry.get("license") or "UNKNOWN")
        bundled = name in FRONTEND_BUNDLED
        rows.append({"ecosystem": "npm", "name": name, "version": str(entry.get("version")), "license": lic,
                     "scope": "BUNDLED_IN_FRONTEND" if bundled else ("DEV_OR_BUILD_ONLY" if entry.get("dev") else "RUNTIME_DEPENDENCY"),
                     "bundled_in": ["frontend-bundle"] if bundled else [], "vendored": False, "modified": False,
                     "review": bool(REVIEW.search(lic))})
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        summary.setdefault(row["scope"], {}).setdefault(row["license"], 0)
        summary[row["scope"]][row["license"]] += 1
    report = {"note": "Automated from package metadata and the lock files; not legal advice.",
              "artifacts": {"source_repository": "no third-party source vendored",
                            "python_wheel": "pure-Python pados package only; dependencies are installed separately by pip",
                            "frontend_bundle": sorted(FRONTEND_BUNDLED), "docker_image": "none distributed",
                            "demo_package": "none distributed (the demo runs from the source checkout)"},
              "packages": rows, "summary": summary, "review_required": [r for r in rows if r["review"]]}
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"packages={len(rows)} runtime={sum(r['scope']=='RUNTIME_DEPENDENCY' for r in rows)} "
          f"bundled={sum(r['scope']=='BUNDLED_IN_FRONTEND' for r in rows)} review={len(report['review_required'])}")
    for r in report["review_required"]:
        print("REVIEW", r["ecosystem"], r["name"], r["version"], r["license"], r["scope"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
