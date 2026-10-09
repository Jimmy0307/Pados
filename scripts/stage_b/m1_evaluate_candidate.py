# SPDX-License-Identifier: AGPL-3.0-only
"""Run the three pre-download gates for one audited candidate against a machine-captured device profile.

    python scripts/stage_b/m1_evaluate_candidate.py <candidate.json> <device_profile.json> <out.json>

The device profile is the Windows capture (``capture/device_profile.json``); its ``derived`` block is recomputed,
not trusted. Contexts evaluated: 8K with f16 KV, 16K with f16 KV, 16K with q8_0 KV. The download decision uses
8K/f16 (the first-model operating point); 16K results are recorded so M2 tests 16K only where it can fit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pados.local_intelligence.audit import (
    ModelCandidate,
    license_audit,
    may_become_available,
    provenance_audit,
    size_fit_audit,
)
from pados.local_intelligence.device import DeviceProfile


def main(candidate_path: str, device_path: str, out_path: str) -> int:
    candidate = ModelCandidate.model_validate_json(Path(candidate_path).read_text(encoding="utf-8"))
    raw = json.loads(Path(device_path).read_text(encoding="utf-8"))
    raw.pop("derived", None)
    device = DeviceProfile.model_validate(raw)
    license_result, provenance_result = license_audit(candidate), provenance_audit(candidate)
    sizes = {
        "8k_f16": size_fit_audit(candidate, device, context_tokens=8192),
        "16k_f16": size_fit_audit(candidate, device, context_tokens=16384),
        "16k_q8_0": size_fit_audit(candidate, device, context_tokens=16384, kv_bytes_per_value=1),
    }
    decision = (license_result, provenance_result, sizes["8k_f16"])
    result = {
        "schema": "pados.stage-b.m1-gate-evaluation.v1", "candidate_id": candidate.candidate_id,
        "device_profile_digest": device.profile_digest, "device_measured_at": device.measured_at.isoformat(),
        "MODEL_LICENSE_AUDIT": license_result.model_dump(mode="json"),
        "MODEL_PROVENANCE_AUDIT": provenance_result.model_dump(mode="json"),
        "MODEL_SIZE_FITS_DEVICE": {key: value.model_dump(mode="json") for key, value in sizes.items()},
        "decision_operating_point": "8k_f16",
        "may_become_available": may_become_available(decision),
        "next_state": "AVAILABLE" if may_become_available(decision) else "AUDITED",
        "not_implied": ["DOWNLOADED", "BENCHMARKED", "REGISTERED", "AUTHORIZED", "ROUTABLE"],
    }
    Path(out_path).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: (v["status"] if isinstance(v, dict) and "status" in v else v) for k, v in result.items()
                      if k.startswith(("MODEL_LICENSE", "MODEL_PROVENANCE", "may_", "next_"))}, indent=2))
    print({key: (value.status.value, value.figures.get("required_mib"), value.figures.get("vram_free_mib"))
           for key, value in sizes.items()})
    return 0 if may_become_available(decision) else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:4]))
