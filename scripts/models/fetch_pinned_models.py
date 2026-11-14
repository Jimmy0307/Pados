# SPDX-License-Identifier: AGPL-3.0-only
"""Operator action: fetch the exact files of an audited model pin, then verify them byte for byte.

PADOS never downloads a model on its own. This script is the explicit, operator-run fetch: it downloads ONLY the
files a built-in pin lists, at the pin's exact 40-hex revision, from the pin's source repository, into
``<model-root>/<pin_id>``; then it runs the same ``verify_model_dir`` the engines run before loading. A mismatch
leaves the directory refused (the engine stays unavailable) and the script exits non-zero.

The operator must name every pin to fetch (``--approve``) and accept each pin's upstream license
(``--accept-license``); nothing is fetched by default. Requires ``huggingface_hub`` (installed with the
``document-intelligence`` extra). Example:

    python scripts/models/fetch_pinned_models.py --approve bge-small-en-v1.5-onnx-q \\
        --approve ms-marco-minilm-l6-v2-onnx --accept-license MIT --accept-license Apache-2.0
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from pados.packs.document_intelligence.local_models import default_model_root
from pados.packs.document_intelligence.model_pin import BUILTIN_PINS, ModelPin, verify_model_dir


def fetch(pin: ModelPin, root: Path) -> str | None:
    from huggingface_hub import hf_hub_download

    directory = root / pin.pin_id
    for item in pin.files:
        hf_hub_download(pin.source_repo, item.path, revision=pin.revision, local_dir=str(directory))
    refusal = verify_model_dir(pin, directory)
    if refusal is not None:  # quarantine, never leave unverified bytes where an engine could look
        shutil.move(str(directory), str(root / f"{pin.pin_id}.refused"))
    return refusal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify audited model pins (operator action).")
    parser.add_argument("--approve", action="append", default=[], choices=sorted(BUILTIN_PINS),
                        help="a pin id to fetch (repeat for each)")
    parser.add_argument("--accept-license", action="append", default=[],
                        help="an upstream SPDX license the operator accepts (repeat for each)")
    parser.add_argument("--model-root", type=Path, default=None, help="default: PADOS_MODEL_ROOT or the user data dir")
    args = parser.parse_args(argv)
    if not args.approve:
        parser.error("name at least one pin with --approve; nothing is fetched by default")
    root = args.model_root or default_model_root()
    report: dict[str, object] = {}
    status = 0
    for pin_id in args.approve:
        pin = BUILTIN_PINS[pin_id]
        if pin.license_spdx not in args.accept_license:
            report[pin_id] = {"status": "REFUSED", "reason": "LICENSE_NOT_ACCEPTED", "license": pin.license_spdx}
            status = 1
            continue
        if verify_model_dir(pin, root / pin_id) is None:
            report[pin_id] = {"status": "ALREADY_VERIFIED", "pin_digest": pin.digest}
            continue
        refusal = fetch(pin, root)
        report[pin_id] = ({"status": "VERIFIED", "pin_digest": pin.digest, "revision": pin.revision}
                          if refusal is None else {"status": "REFUSED", "reason": refusal})
        status = status or (0 if refusal is None else 1)
    print(json.dumps(report, indent=2, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
