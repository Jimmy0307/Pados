# SPDX-License-Identifier: AGPL-3.0-only
"""Package the built web frontend as a byte-reproducible tar.gz.

Usage: bundle_web.py WEB_DIST_DIR OUT_DIR
Writes OUT_DIR/pados-web-<version>.tar.gz with sorted entries, owner 0:0, fixed
modes and mtime = SOURCE_DATE_EPOCH (or the HEAD commit time). Symlinks fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.supply_chain import SupplyChainError, deterministic_tar_gz  # noqa: E402
from pados.versions import PRODUCT_VERSION  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("web_dist", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is None:
        epoch = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True, text=True,
                               check=True).stdout.strip()
    try:
        data = deterministic_tar_gz(args.web_dist, prefix=f"pados-web-{PRODUCT_VERSION}", mtime=int(epoch))
    except SupplyChainError as error:
        print(f"WEB_BUNDLE_REFUSED {error.code}", file=sys.stderr)
        return 1
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"pados-web-{PRODUCT_VERSION}.tar.gz"
    out.write_bytes(data)
    print(f"{out.name} sha256={hashlib.sha256(data).hexdigest()} bytes={len(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
