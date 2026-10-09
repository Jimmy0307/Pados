# SPDX-License-Identifier: AGPL-3.0-only
"""Formal third-party / donor provenance audit (read-only).

Compares git blob ids of every tracked file against every blob of the
authorized donor mirrors (``GIT_OPTIONAL_LOCKS=0``: no donor index writes),
lists manifest-declared ports from the consolidation phase, and scans for
company/HR data markers. Usage: provenance_audit.py DONORS_DIR OUT.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HR_DATA = re.compile(r"(candidate_(name|email|phone)|resume_text|\b104\s*job\s*bank\b|身分證|履歷)", re.IGNORECASE)


def git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env).stdout


def main() -> int:
    donors_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    donor_blobs: dict[str, list[str]] = defaultdict(list)
    heads = {}
    # Donor names come from the internal (export-excluded) migration manifest, not from this script.
    migration = json.loads((ROOT / "docs/migration/PADOS_ASSET_MIGRATION_MANIFEST.json").read_text(encoding="utf-8"))
    for repo in sorted(migration["donor_revisions"]):
        path = donors_dir / repo
        if not path.exists():
            continue
        heads[repo] = git("rev-parse", "HEAD", cwd=path).strip()
        for line in git("ls-tree", "-r", "HEAD", cwd=path).splitlines():
            meta, name = line.split("\t", 1)
            _mode, kind, blob = meta.split()
            if kind == "blob":
                donor_blobs[blob].append(f"{repo}:{name}")
    tracked = [line.split("\t", 1) for line in git("ls-files", "-s", cwd=ROOT).splitlines()]
    identical = [{"path": name, "donor": donor_blobs[meta.split()[1]][:3]} for meta, name in tracked
                 if meta.split()[1] in donor_blobs and os.path.getsize(ROOT / name) > 0]
    manifest = json.loads((ROOT / "docs/migration/PADOS_ASSET_MIGRATION_MANIFEST.json").read_text(encoding="utf-8"))
    ports = [{"destination": c["destination"], "source_repo": c["source_repo"], "source_path": c["source_path"],
              "migration_type": c["migration_type"], "capability": c["capability"]}
             for c in manifest["components"] if c["migration_type"] in {"DIRECT_PORT", "DIRECT_PORT_WITH_ADAPTER"}]
    hr_hits = []
    for _meta, name in tracked:
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if HR_DATA.search(text):
            hr_hits.append(name)
    report = {
        "donor_heads": heads, "donor_blob_count": len(donor_blobs), "tracked_files": len(tracked),
        "identical_to_donor_blob": identical,
        "manifest_direct_ports": ports,
        "manifest_direct_port_destinations": sorted({p["destination"] for p in ports}),
        "hr_data_marker_files": hr_hits,
        "phases_concept_only": ["ENTERPRISE_CAPABILITY_PACK_V1", "LOCAL_COMPUTE_NODE_V1", "PRODUCTIZATION_V1"],
    }
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"tracked={len(tracked)} donor_blobs={len(donor_blobs)} identical={len(identical)} "
          f"direct_ports={len(ports)} destinations={len(report['manifest_direct_port_destinations'])} hr_marker_files={len(hr_hits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
