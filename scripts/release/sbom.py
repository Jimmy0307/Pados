# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the PADOS SBOM (CycloneDX 1.5 JSON and SPDX 2.3 JSON), offline.

Usage: sbom.py OUT_DIR [--timestamp YYYY-MM-DDTHH:MM:SSZ] [--no-spdx] [--build-environment FILE]
Inputs: pyproject.toml, requirements.lock, web/package-lock.json and installed
package metadata (licenses). The timestamp defaults to SOURCE_DATE_EPOCH or the
HEAD commit time, so the output is reproducible for a given tree and venv.
``--build-environment`` binds the build job's actual ``pip freeze`` inventory digest.
Writes OUT_DIR/sbom.cdx.json (and OUT_DIR/sbom.spdx.json). An SBOM is inventory
evidence, not trust and not authority.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.sbom import generate_for_project, render  # noqa: E402
from pados.release.supply_chain import SBOM_CYCLONEDX_FILE, SBOM_SPDX_FILE, build_timestamp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--no-spdx", action="store_true")
    parser.add_argument("--build-environment", type=Path, default=None)
    args = parser.parse_args(argv)
    timestamp = args.timestamp or build_timestamp(ROOT, os.environ)
    environment = args.build_environment.read_bytes() if args.build_environment else None
    cyclonedx, spdx = generate_for_project(ROOT, timestamp=timestamp, build_environment=environment)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    outputs = {SBOM_CYCLONEDX_FILE: render(cyclonedx)}
    if not args.no_spdx:
        outputs[SBOM_SPDX_FILE] = render(spdx)
    for name, data in outputs.items():
        (args.out_dir / name).write_bytes(data)
        print(f"{name} sha256={hashlib.sha256(data).hexdigest()}")
    print(f"cyclonedx_components={len(cyclonedx['components'])} serial={cyclonedx['serialNumber']} "
          f"spdx_packages={len(spdx['packages'])} timestamp={timestamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
