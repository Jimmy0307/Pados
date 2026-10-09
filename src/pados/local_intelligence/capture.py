# SPDX-License-Identifier: AGPL-3.0-only
"""Capture machine evidence: device profile, runtime discovery and the projected hardware profile.

    python -m pados.local_intelligence.capture --out <dir> [--ollama-url http://127.0.0.1:11434]

Writes ``device_profile.json``, ``runtime_discovery.json``, ``hardware_profile.json`` and ``raw/<probe>.out``
(the exact bytes every recorded SHA-256 refers to). It reads the host and loopback runtimes only; it never
downloads, installs, registers or authorizes anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pados.local_intelligence.device import DeviceProfiler, to_hardware_profile
from pados.local_intelligence.probes import ProbeSession
from pados.local_intelligence.runtimes import RuntimeEndpoint, default_runtime_endpoints, describe_runtime


def capture(out: Path, *, ollama_url: str, extra: Sequence[RuntimeEndpoint] = (),
            resource_id: str = "local-device", disk_path: str = ".") -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = out / "raw"
    raw_dir.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    runtimes = []
    raw: dict[str, bytes] = {}
    for target in (*default_runtime_endpoints(ollama_url), *extra):
        session = ProbeSession()
        runtimes.append(describe_runtime(target, now=now, session=session))
        raw.update({f"{target.runtime_id}.{key}": value for key, value in session.raw.items()})
    profiler = DeviceProfiler.local(disk_path=disk_path)
    profile = profiler.profile(tuple(runtimes))
    raw.update(profiler.session.raw)
    documents: dict[str, object] = {
        "device_profile.json": profile.evidence(),
        "runtime_discovery.json": {"schema": "pados.local-intelligence.runtime-discovery.v1",
                                   "measured_at": now.isoformat(),
                                   "runtimes": [item.model_dump(mode="json") for item in runtimes]},
    }
    try:
        documents["hardware_profile.json"] = to_hardware_profile(profile, resource_id=resource_id).model_dump(mode="json")
    except ValueError as exc:
        documents["hardware_profile.json"] = {"error": str(exc)}
    for name, document in documents.items():
        (out / name).write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                encoding="utf-8")
    for probe_id, payload in sorted(raw.items()):
        (raw_dir / f"{probe_id}.out").write_bytes(payload)
    return {"files": sorted([*documents, *(f"raw/{key}.out" for key in raw)]),
            "raw_sha256": {key: hashlib.sha256(value).hexdigest() for key, value in sorted(raw.items())},
            "gpu_status": profile.gpu.status.value, "machine_class": profile.machine_class,
            "runtimes": {item.runtime_id: item.health.value for item in runtimes}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pados.local_intelligence.capture")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--resource-id", default="local-device")
    parser.add_argument("--disk-path", default=".")
    args = parser.parse_args(argv)
    summary = capture(args.out, ollama_url=args.ollama_url, resource_id=args.resource_id, disk_path=args.disk_path)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
