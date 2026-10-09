# SPDX-License-Identifier: AGPL-3.0-only
"""Verify a Stage B M4.1 dogfood archive (operator-returned) without trusting its own conclusions.

    python scripts/stage_b/m4_1_verify_dogfood.py <archive.zip> --sha256 <expected archive sha256>

Checks: the archive hash; safe member names; the internal SHA256SUMS over the exact file set; the result's
commit/tree; the profile id and manifest digest against the M2.1/M3 pins; the live binding (fresh device and
runtime, the pinned digest present, the measured runtime version and device identity); the authorization
sequence (unauthorized -> blocked; explicit -> routed and completed; revoked after plan -> refused with no adapter
run; revoked before selection -> blocked); the node receipt verification fields; the response re-validated
from its raw text by the deterministic checker (not the recorded verdict); and the integrity probe semantics.
Prints one JSON verdict; exit 0 only when every check passes.

Measured execution contract (M4.2): the archived catalog's profile is re-loaded and the execution condition the
requirement binds is RE-DERIVED here (``required_execution``); the admitted route, the node-signed record and the
node receipt must all carry exactly that condition's capability-critical settings (reasoning mode, context window,
sampling, output constraint) with a budget no smaller than the largest measured completion. Failure evidence must
be consistent: a completed run carries no failure record; a failed one carries the complete code and no receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

PIN_MANIFEST = "sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
PIN_PROFILE = "mcp-bd7794d49caed931f7063d8e021f97bd2e1b2cc6c10c09f46f1e04b5906ccadb"  # M3.1 (execution conditions)
PIN_RUNTIME_VERSION = "0.34.3"
PIN_DEVICE = "26494bb2891abe072a7d125a9dacaa89bbbba6d73b7c521d513101af684dc29a"


def verify(archive: Path, expected_sha256: str, *, rehearsal: bool = False) -> dict[str, Any]:
    from m4_1_live_chain import validate_extraction

    checks: dict[str, bool] = {}
    data = archive.read_bytes()
    checks["archive_sha256"] = hashlib.sha256(data).hexdigest() == expected_sha256
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        checks["member_names_safe"] = all(not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts
                                          for n in names)
        root = names[0].split("/", 1)[0]
        files = {n.split("/", 1)[1]: bundle.read(n) for n in names if not n.endswith("/")}
    sums = dict(reversed(line.split("  ", 1)) for line in files["SHA256SUMS"].decode().splitlines() if line)
    checks["sha256sums_exact_file_set"] = set(sums) == set(files) - {"SHA256SUMS"}
    checks["sha256sums_match"] = all(hashlib.sha256(files[name]).hexdigest() == digest for name, digest in sums.items())
    result = json.loads(files["M4_1_DOGFOOD_RESULT.json"])
    steps = result.get("steps", {})
    checks["mode"] = result.get("mode") == ("REHEARSAL" if rehearsal else "LIVE")
    checks["no_error"] = result.get("error") is None
    persistence = steps.get("persistence", {})
    checks["persistence_identical"] = persistence.get("reloaded_identical") is True
    binding = steps.get("binding", {})
    if not rehearsal:
        checks["profile_is_pinned"] = persistence.get("profile_id") == PIN_PROFILE and binding.get("profile_id") == PIN_PROFILE
        checks["manifest_is_pinned"] = binding.get("manifest_digest") == PIN_MANIFEST
        checks["runtime_version_measured"] = binding.get("runtime_version") == PIN_RUNTIME_VERSION
        checks["device_is_measured_device"] = binding.get("device_profile_id") == PIN_DEVICE
    checks["binding_fresh"] = binding.get("device_freshness") == "FRESH" and binding.get("runtime_freshness") == "FRESH"
    checks["binding_present"] = bool(binding.get("runtime_ref")) and binding.get("runtime_state") != "UNAVAILABLE"
    checks["unauthorized_blocked"] = steps.get("unauthorized", {}).get("feasible") is False
    plan = steps.get("plan", {})
    checks["authorized_routed"] = plan.get("feasible") is True and plan.get("router_status") == "ROUTED"
    dogfood = steps.get("dogfood", {})
    checks["completed"] = dogfood.get("status") == "COMPLETED" and dogfood.get("selected_worker_id") == plan.get("selected")
    receipt = dogfood.get("node_execution_receipt") or {}
    checks["node_receipt_verified"] = all(v == "VERIFIED" for v in (receipt.get("verification") or {"x": 0}).values())
    checks["receipt_not_authority"] = receipt.get("semantic_authority") is False and \
        dogfood.get("semantic_authority") == "NONE"
    response = str(dogfood.get("response", "")).encode("utf-8")
    checks["response_hash"] = hashlib.sha256(response).hexdigest() == dogfood.get("response_sha256")
    checks["response_revalidated"] = validate_extraction(response)["passed"] is True
    route = dogfood.get("measured_route") or {}
    checks["measured_route_bound"] = route.get("profile_id") == binding.get("profile_id") and \
        route.get("manifest_digest") == binding.get("manifest_digest")
    events = dogfood.get("authorization_evidence") or []
    checks["explicit_authorization_named_model"] = any(e.get("allowed_models") for e in events) and \
        all(m.endswith("@" + str(binding.get("manifest_digest"))) for e in events for m in e.get("allowed_models", []))
    after = steps.get("revoked_after_plan", {})
    checks["revoked_after_plan_refused"] = after.get("plan_feasible") is True and \
        after.get("execution") == "PLAN_NOT_REPRODUCIBLE" and after.get("adapter_runs") == 0
    checks["revoked_before_selection_blocked"] = steps.get("revoked_before_selection", {}).get("feasible") is False
    checks.update(_execution_checks(files, dogfood, route))
    integrity = steps.get("integrity") or {}
    timeout = integrity.get("timeout") or {}
    checks["timeout_semantics_recorded"] = timeout.get("measurement") in {"MEASURED", "INVALID_MEASUREMENT"} and \
        timeout.get("server_side_cancellation") == "NOT_OBSERVABLE"
    checks["concurrency_semantics_recorded"] = (integrity.get("concurrency") or {}).get("parallel_token_generation") in {
        "OBSERVED", "NOT_OBSERVED_SERIALIZED", "UNKNOWN"}
    text = b"\n".join(files.values()).decode("utf-8", errors="replace")
    checks["no_presigned_urls"] = not any(token in text for token in ("X-Amz-Signature", "X-Amz-Credential",
                                                                       "X-Amz-Security-Token", "AWSAccessKeyId"))
    failed = sorted(name for name, ok in checks.items() if not ok)
    return {"archive": archive.name, "root": root, "checks": checks, "failed": failed,
            "verdict": "PASS" if not failed else "FAIL",
            "summary": {"timeout": timeout, "concurrency": integrity.get("concurrency"),
                        "server_config": (integrity.get("server_config") or {}).get("SERVER_EFFECTIVE_CONFIGURATION"),
                        "process_memory_attribution": (integrity.get("process_tree_after_timeout") or {}).get(
                            "PROCESS_MEMORY_ATTRIBUTION"), "wall_s": dogfood.get("wall_s")}}


def _execution_checks(files: dict[str, bytes], dogfood: dict[str, Any], route: dict[str, Any]) -> dict[str, bool]:
    from m4_1_live_chain import REQUIREMENT

    from pados.resources.capability_profile import (
        ExecutionCompatibility,
        ExecutionConfig,
        ModelCapabilityProfile,
        check_execution,
        required_execution,
    )

    checks: dict[str, bool] = {}
    catalog = json.loads(files.get("state/model-catalog.json", b"{}"))
    profiles = [ModelCapabilityProfile.model_validate(p) for p in catalog.get("capability_profiles") or []]
    expected, _ = required_execution(profiles[0], REQUIREMENT) if len(profiles) == 1 else (None, None)
    checks["measured_execution_rederived"] = expected is not None
    bound = route.get("execution") or {}
    checks["route_binds_measured_execution"] = expected is not None and bound.get("config") ==         expected.config.model_dump(mode="json")
    receipt = dogfood.get("node_execution_receipt") or {}
    record = dogfood.get("node_record_execution")
    applied = (receipt.get("runtime") or {}).get("execution_requested")
    try:
        effective = ExecutionConfig.model_validate(record) if record is not None else None
    except ValueError:
        effective = None
    checks["node_ran_measured_execution"] = expected is not None and effective is not None and         check_execution(expected, effective)[0] is ExecutionCompatibility.COMPATIBLE         and record == receipt.get("execution") == applied
    failure = dogfood.get("node_failure")
    if dogfood.get("status") == "COMPLETED":
        checks["failure_evidence_consistent"] = failure is None and dogfood.get("node_refusal") is None
    else:
        checks["failure_evidence_consistent"] = isinstance(failure, dict) and bool(failure.get("failure_code"))             and failure.get("node_receipt") == "NOT_ISSUED_FOR_FAILED_EXECUTION" and not receipt
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--rehearsal", action="store_true")
    args = parser.parse_args(argv)
    verdict = verify(Path(args.archive), args.sha256, rehearsal=args.rehearsal)
    print(json.dumps(verdict, indent=2, ensure_ascii=False, default=str))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
