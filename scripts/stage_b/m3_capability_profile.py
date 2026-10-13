# SPDX-License-Identifier: AGPL-3.0-only
"""Stage B M2.1 acceptance + M3: verify the Windows evidence archive, build and REGISTER the capability profile.

Run on the VMware side with the checkout's own interpreter (nothing is downloaded, authorized or routed):

    .venv/bin/python scripts/stage_b/m3_capability_profile.py --archive <m2-...zip> --sha256 <hex> --out <dir>

Writes ``M2_1_EVIDENCE_VERIFICATION.json``, ``M2_1_BENCHMARK_RECOMPUTATION.json`` and
``M3_MODEL_CAPABILITY_PROFILE.json``. The archive is extracted into a temporary directory that is removed
afterwards; the raw evidence is never modified or copied into the report. Registration means: the measured
profile is attached to its description in the existing ``ModelCatalog`` (an in-memory value here, persisted only
as this report). The lifecycle ends at REGISTERED: AUTHORIZED and ROUTABLE remain explicit policy decisions.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pados.capability.model import Distribution, LicenseInfo
from pados.local_intelligence.audit import ModelLifecycleState
from pados.local_intelligence.profile_builder import (
    PinnedIdentity,
    build_capability_profile,
    extract_archive,
    load_evidence,
    recompute_summary,
    sha256_file,
    verify_evidence,
)
from pados.resources.capability_profile import graph_projection
from pados.resources.models import ModelCatalog, profile_from_capability

# The M1 selection (immutable pin) and the code that produced the evidence.
PIN = PinnedIdentity(
    artifact_ref="qwen3:8b",
    manifest_digest="sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41",
    layer_digests=("sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f",
                   "sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4",
                   "sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12",
                   "sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472e6a12034e4246fbbdb"),
    commit="e0499c722794b98577542e9cbfb464d23bd6973c", tree="a03083d5c9ca2ddeb7aa267384edd90a8c07d95b",
    runtime_version="0.34.3",
    layer_sizes={"sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f": 5_225_374_496,
                 "sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4": 1723,
                 "sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12": 11338,
                 "sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472e6a12034e4246fbbdb": 120})
# From the M1 audit: Apache-2.0, user-installed runtime artifact (not bundled).
LICENSE = LicenseInfo(spdx="Apache-2.0", engine_version="0.34.3", distribution=Distribution.RUNTIME_OPTIONAL)


def _write(path: Path, document: object) -> str:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    code = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,  # noqa: S603,S607
                                     check=True).stdout.strip()}
    now = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix="pados-m2-1-") as scratch:
        root = extract_archive(args.archive, args.sha256, Path(scratch) / "x")
        evidence = load_evidence(root)
        checks = verify_evidence(evidence, PIN)
        recomputed = recompute_summary(evidence)
        independent = Path(scratch) / "independent.json"
        tool = repo / "scripts" / "stage_b" / "m2_1_independent_recompute.py"
        run = subprocess.run([sys.executable, "-I", str(tool), str(root), str(independent)], capture_output=True,  # noqa: S603
                             text=True, check=False)
        independent_report = json.loads(independent.read_text(encoding="utf-8"))
        passed = all(c.status == "PASS" for c in checks) and run.returncode == 0
        files = {name: evidence.sha256(name) for name in sorted(evidence.files)}
        _write(args.out / "M2_1_EVIDENCE_VERIFICATION.json", {
            "schema": "pados.stage-b.m2-1-evidence-verification.v1", "verified_at": now, "verifier_code": code,
            "archive": args.archive.name, "archive_sha256_expected": args.sha256,
            "archive_sha256_actual": sha256_file(args.archive), "pin": {
                "artifact_ref": PIN.artifact_ref, "manifest_digest": PIN.manifest_digest,
                "layer_digests": list(PIN.layer_digests), "layer_sizes": dict(PIN.layer_sizes),
                "commit": PIN.commit, "tree": PIN.tree, "runtime_version": PIN.runtime_version},
            "checks": [c.__dict__ for c in checks], "file_sha256": files,
            "result": "PASS" if passed else "FAIL",
            "labels": ["MACHINE_CAPTURED", "RAW_EVIDENCE_VERIFIED", "M2_1_ACCEPTED"] if passed else []})
        _write(args.out / "M2_1_BENCHMARK_RECOMPUTATION.json", {
            "schema": "pados.stage-b.m2-1-benchmark-recomputation.v1",
            "repository_summarizer": {"counts": {k: len(v) for k, v in recomputed.items()}, "leaves": recomputed},
            "independent_reimplementation": {k: independent_report[k] for k in
                                             ("counts", "whole_object_equal", "rate_cross_check_issues", "leaves")},
            "raw_sha256": files["BENCHMARK_RAW.jsonl"], "summary_sha256": files["BENCHMARK_SUMMARY.json"]})
        if not passed:
            print("M2_1_EVIDENCE_VERIFICATION=FAIL")
            return 1
        profile = build_capability_profile(evidence, PIN, archive_sha256=args.sha256)
    description = profile_from_capability(profile, license=LICENSE)
    catalog = ModelCatalog([description]).register_capability_profile(profile)
    registered = catalog.capability_profile(description.model_id) == profile
    _write(args.out / "M3_MODEL_CAPABILITY_PROFILE.json", {
        "schema": "pados.stage-b.m3-model-capability-profile.v1", "built_at": now, "builder_code": code,
        "profile_id": profile.profile_id, "artifact_id": profile.artifact.artifact_id,
        "binding_id": profile.binding.binding_id,
        "lifecycle": [ModelLifecycleState.DOWNLOADED.value, ModelLifecycleState.BENCHMARKED.value]
        + ([ModelLifecycleState.REGISTERED.value] if registered else []),
        "registration": {"registry": "pados.resources.models.ModelCatalog", "model_id": description.model_id,
                         "description_digest": description.profile_digest, "state": "REGISTERED" if registered
                         else "NOT_REGISTERED", "authorization": "NOT_AUTHORIZED", "routable": False},
        "catalog_description": description.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"), "graph_projection": graph_projection(profile)})
    print(f"M2_1_EVIDENCE_VERIFICATION=PASS\nM3_CAPABILITY_PROFILE_ID={profile.profile_id}\n"
          f"M3_REGISTRATION_STATE={'REGISTERED' if registered else 'NOT_REGISTERED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
