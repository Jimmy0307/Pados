# SPDX-License-Identifier: AGPL-3.0-only
"""Write the release manifest and unsigned local provenance for a release directory.

Usage: release_manifest.py RELEASE_DIR [--timestamp YYYY-MM-DDTHH:MM:SSZ] [--record-build-environment]
RELEASE_DIR holds the built artifacts (pados-<v>-*.whl, pados-<v>.tar.gz,
pados-web-<v>.tar.gz), sbom.cdx.json, sbom.spdx.json and build-environment.txt
(the build job's ``pip freeze --all --exclude-editable``); any other file fails
closed. ``--record-build-environment`` writes build-environment.txt from the
running interpreter when it is absent (local runs). Writes provenance.intoto.json
(in-toto v1 + SLSA provenance v1, UNSIGNED_LOCAL_PROVENANCE) and
release-manifest.json, then prints the manifest SHA-256 the Home Base operator
pins out of band. Nothing is deployed; the manifest records deployment_authorized=false.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.supply_chain import (  # noqa: E402
    BUILD_ENVIRONMENT_FILE,
    SupplyChainError,
    build_timestamp,
    environment_inventory,
    invocation_from_env,
    read_source_identity,
    write_release_metadata,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--record-build-environment", action="store_true")
    args = parser.parse_args(argv)
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    environment = args.release_dir / BUILD_ENVIRONMENT_FILE
    if args.record_build_environment and not environment.exists():
        environment.write_text(environment_inventory(), encoding="utf-8")
    try:
        result = write_release_metadata(
            ROOT, args.release_dir,
            source=read_source_identity(ROOT, os.environ),
            invocation=invocation_from_env(os.environ, started_on=started,
                                           finished_on=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            created_at=args.timestamp or build_timestamp(ROOT, os.environ),
        )
    except SupplyChainError as error:
        print(f"RELEASE_MANIFEST_REFUSED {error.code}", file=sys.stderr)
        return 1
    manifest = result.manifest
    print(f"manifest={result.manifest_path.name} manifest_sha256={result.manifest_sha256}")
    print(f"provenance={result.provenance_path.name} sha256={result.provenance_sha256} "
          f"signing={manifest['provenance']['statement']['signing']}")
    for artifact in manifest["artifacts"]:
        print(f"artifact {artifact['kind']} {artifact['name']} sha256={artifact['sha256']}")
    for sbom in manifest["sbom"]:
        print(f"sbom {sbom['format']} {sbom['name']} sha256={sbom['sha256']} components={sbom['component_count']}")
    env = manifest["build_environment"]
    print(f"build_environment {env['name']} sha256={env['sha256']} packages={env['package_count']}")
    print(f"commit={manifest['source']['git_commit']} tree={manifest['source']['git_tree']} "
          f"dirty={manifest['source']['dirty']} posture={manifest['posture']['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
