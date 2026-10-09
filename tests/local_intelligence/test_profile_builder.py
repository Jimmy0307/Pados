"""Stage B M3: M2 evidence -> verified ModelCapabilityProfile (synthetic archives; the real one only if provided)."""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

from pados.local_intelligence.bench_summary import summarize
from pados.local_intelligence.profile_builder import (
    EvidenceRefused,
    PinnedIdentity,
    build_capability_profile,
    extract_archive,
    load_evidence,
    verify_evidence,
)
from pados.resources.capability_profile import DescriptiveRole, MeasuredCapability, ObservationStatus

MANIFEST = "sha256:" + "a" * 64
LAYERS = ("sha256:" + "b" * 64, "sha256:" + "c" * 64)
PIN = PinnedIdentity(artifact_ref="example-model:small", manifest_digest=MANIFEST, layer_digests=LAYERS,
                     commit="1" * 40, tree="2" * 40, runtime_version="1.2.3", layer_sizes={LAYERS[0]: 1000})


def _call(phase: str, task_id: str, *, ctx: int = 8192, ttft: float = 0.5, wall: float = 1.0, tps: float = 50.0,
          ok: bool = True, gpu: int = 6000) -> dict[str, Any]:
    return {"record_type": "call", "phase": phase, "task_id": task_id, "options": {"num_ctx": ctx}, "ok": ok,
            "ttft_s": ttft, "wall_s": wall, "gen_tokens_per_s": tps, "prompt_tokens_per_s": 1000.0, "load_s": 0.01,
            "prompt_eval_count": 50, "eval_count": 25,
            "resources": {"gpu_mem_used_peak_mib": gpu, "runtime_rss_peak_mib": 50, "sys_available_min_mib": 9000,
                          "gpu_util_mean_pct": 50.0, "cpu_mean_pct": 5.0, "gpu_util_peak_pct": 90}}


def _check(phase: str, task_id: str, category: str, passed: bool, *, think: bool = False,
           details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"record_type": "check", "phase": phase, "task_id": task_id, "category": category, "think": think,
            "passed": passed, "details": details or {}}


def _ps(phase: str, ctx: int, size: int, vram: int) -> dict[str, Any]:
    return {"record_type": "ps", "phase": phase, "models": [{"name": PIN.artifact_ref, "size": size, "size_vram": vram,
                                                             "digest": MANIFEST.removeprefix("sha256:"),
                                                             "context_length": ctx}]}


QUALITY = (("zh_a", "zh_tw_instruction", True, False), ("zh_b", "zh_tw_instruction", True, False),
           ("en_a", "en_instruction", True, False), ("json_a", "structured_json", True, False),
           ("schema_a", "schema_adherence", True, False), ("ext_a", "extraction", True, False),
           ("reason_a", "reasoning", True, False), ("reason_t", "reasoning", True, True),
           ("code_generate", "code", True, False), ("code_repair", "code", True, False),
           ("sum_a", "summarization", True, False), ("sum_b", "summarization", False, False),
           ("multi", "multi_step", False, False), ("tool_a", "tool_call", True, False),
           ("refuse_a", "refusal_inability", True, False))


def raw_records(*, category_override: str | None = None, timed_out: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [
        {"record_type": "pull", "model": PIN.artifact_ref, "skipped": "ALREADY_PRESENT",
         "expected_manifest_digest": MANIFEST, "observed_manifest_digest": MANIFEST,
         "local_manifest_sha256": MANIFEST.removeprefix("sha256:"), "pinned_layers": list(LAYERS),
         "identity_refusal": None, "digest_match": True},
        {"record_type": "meta", "benchmark_version": "pados-local-bench-1.0.0", "model": PIN.artifact_ref,
         "task_set_digest": "9" * 64, "seed": 42, "started_at": "2026-10-07T18:40:13+00:00"}]
    records += [_call("cold_start", f"cold_{i}", wall=w) for i, w in ((1, 40.0), (2, 6.0), (3, 6.1))]
    records.append(_ps("after_cold", 8192, 6_000_000_000, 6_000_000_000))
    records += [_call("warm_start", f"warm_{i}", ttft=0.4 + i / 100) for i in range(1, 6)]
    for task_id, category, passed, think in QUALITY:
        records.append(_call("quality", task_id))
        records.append(_check("quality", task_id, category_override if task_id == "multi" and category_override
                              else category, passed, think=think))
    for size, ctx, vram in (("8k", 8192, 6_000_000_000), ("16k", 16384, 4_700_000_000)):
        for pos in ("30", "70"):
            task_id = f"needle_{size}_{pos}"
            records.append(_call("context", task_id, ctx=ctx, ttft=3.0 if ctx == 8192 else 12.0,
                                 tps=50.0 if ctx == 8192 else 11.0))
            records.append(_check("context", task_id, f"long_context_{size}", True))
            records.append(_ps(f"after_{task_id}", ctx, 7_800_000_000 if ctx == 16384 else 6_000_000_000, vram))
    records += [_call("determinism", f"determinism_{i}") for i in (1, 2, 3)]
    records.append(_check("determinism", "determinism", "determinism", True, details={"digests": ["d" * 64] * 3}))
    for level in (2, 4):
        results = [{"ok": True, "ttft_s": 0.5 + i, "wall_s": 1.0 + i, "eval_count": 25} for i in range(level)]
        records.append({"record_type": "concurrency", "phase": "concurrency", "level": level, "results": results,
                        "wall_s": float(level), "aggregate_tokens_per_s": 25.0, "errors": 0,
                        "resources": {"gpu_mem_used_peak_mib": 6000}})
    records.append(_call("timeout", "client_timeout", ok=timed_out is None))
    records.append(_call("timeout", "after_timeout_health"))
    records.append(_check("timeout", "timeout_recovery", "stability", timed_out is not None,
                          details={"timed_out": timed_out, "recovered": True}))
    records += [_call("reload", "reload_1"), _call("reload", "reload_2")]
    records.append(_check("reload", "reload_cycles", "stability", True, details={"cycles": 2, "ok": [True, True]}))
    records.append({"record_type": "meta_end", "finished_at": "2026-10-07T18:44:03+00:00"})
    return [{"at": 1.0 + i, **item} for i, item in enumerate(records)]


DEVICE = {"os": "LINUX", "os_version": "6.8", "architecture": "X86_64",
          "cpu": {"model": "CPU", "logical_cores": 16, "physical_cores": 8}, "memory": {"total_mb": 32768},
          "machine": {"hostname_sha256": "h" * 64},
          "gpu": {"devices": [{"name": "GPU", "vendor": "V", "vram_total_mb": 8192, "compute_capability": "9.0",
                               "driver_version": "1", "pci_bus_id": "0"}], "nvidia": {"cuda_driver_version": "13"}},
          "derived": {"cuda_available": True, "machine_class": "DESKTOP/DISCRETE_GPU", "profile_digest": "p" * 64}}
RUNTIME = {"runtimes": [{"runtime_type": "runtime-x", "version": "1.2.3",
                         "models": [{"name": PIN.artifact_ref, "digest": MANIFEST.removeprefix("sha256:"),
                                     "family": "family-x", "parameter_size": "3B", "quantization": "Q4_K_M",
                                     "format": "gguf"}]}]}


def write_evidence(root: Path, *, raw: list[dict[str, Any]] | None = None, mutate: Any = None) -> Path:
    records = raw if raw is not None else raw_records()
    blobs = {layer: {"complete_bytes": PIN.layer_sizes.get(layer, 10), "partial_files": 0, "partial_bytes": 0}
             for layer in LAYERS}
    files: dict[str, bytes] = {
        "BENCHMARK_RAW.jsonl": "".join(json.dumps(r) + "\n" for r in records).encode(),
        "BENCHMARK_SUMMARY.json": json.dumps(summarize(records), indent=2).encode(),
        "WINDOWS_M2_RESULT.json": json.dumps({
            "execution": "EXECUTED_WINDOWS_NATIVE", "status": "COMPLETED",
            "identity": {"commit": PIN.commit, "tree": PIN.tree, "expected_commit": PIN.commit,
                         "expected_tree": PIN.tree, "tracked_changes": False, "parity": "PASS"},
            "approved_manifest_digest": MANIFEST, "approved_layers": list(LAYERS), "blobs_before": blobs,
            "blobs_after": blobs, "environment": {"platform": "test"}}).encode(),
    }
    for phase in ("before", "after"):
        files[f"capture_{phase}/device_profile.json"] = json.dumps(DEVICE).encode()
        files[f"capture_{phase}/runtime_discovery.json"] = json.dumps(RUNTIME).encode()
        files[f"logs/capture_{phase}.log"] = json.dumps({"files": ["device_profile.json",
                                                                   "runtime_discovery.json"]}).encode()
    if mutate:
        mutate(files)
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
    files["SHA256SUMS"] = sums.encode()
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def failing(root: Path) -> set[str]:
    return {c.name for c in verify_evidence(load_evidence(root), PIN) if c.status != "PASS"}


def test_verified_synthetic_evidence_builds_a_bound_profile(tmp_path: Path) -> None:
    root = write_evidence(tmp_path / "ev")
    assert failing(root) == set()
    profile = build_capability_profile(load_evidence(root), PIN, archive_sha256="f" * 64)
    assert profile.artifact.manifest_digest == MANIFEST and profile.artifact.layer_digests == LAYERS
    assert profile.binding.runtime_version == "1.2.3" and profile.evidence.archive_sha256 == "f" * 64
    again = build_capability_profile(load_evidence(root), PIN, archive_sha256="f" * 64)
    assert again.profile_id == profile.profile_id  # deterministic derivation
    summary = profile.observation(MeasuredCapability.SUMMARIZATION)
    assert (summary.sample_size, summary.success_count, summary.failure_count) == (2, 1, 1)
    assert DescriptiveRole.LOCAL_SUMMARIZATION_CANDIDATE not in profile.roles
    assert DescriptiveRole.LOCAL_CODE_ASSIST_CANDIDATE in profile.roles
    assert profile.observation(MeasuredCapability.TIMEOUT_RECOVERY).status is ObservationStatus.INVALID_MEASUREMENT
    assert profile.observation(MeasuredCapability.REASONING_EXTENDED).sample_size == 1
    codes = {c.code for c in profile.constraints}
    assert {"GPU_PARTIAL_OFFLOAD", "CONCURRENCY_SERIALIZED", "FIRST_LOAD_OUTLIER", "SINGLE_BENCHMARK_RUN",
            "RUNTIME_SERVER_CONFIGURATION_NOT_CAPTURED"} <= codes
    offload = profile.constraint("GPU_PARTIAL_OFFLOAD")
    assert offload is not None and offload.applies_from_context_tokens == 16384
    assert profile.footprint.base_context_tokens == 8192 and profile.footprint.gpu_memory_mib == 6000
    assert profile.observation(MeasuredCapability.CODE_REPAIR).status is ObservationStatus.MEASURED
    assert profile.observation(MeasuredCapability.CONCURRENT_REQUESTS).measured_value == 4.0


def test_an_induced_timeout_is_a_valid_measurement(tmp_path: Path) -> None:
    root = write_evidence(tmp_path / "ev", raw=raw_records(timed_out="CLIENT_TIMEOUT"))
    profile = build_capability_profile(load_evidence(root), PIN, archive_sha256="f" * 64)
    assert profile.observation(MeasuredCapability.TIMEOUT_RECOVERY).all_passed


@pytest.mark.parametrize("mutate, expected", [
    (lambda f: f.update({"BENCHMARK_SUMMARY.json": json.dumps(
        {**json.loads(f["BENCHMARK_SUMMARY.json"]), "failed_calls": 7}).encode()}), "summary_recomputed"),
    (lambda f: f.update({"extra.txt": b"x"}), "exact_file_set"),
    (lambda f: f.update({"WINDOWS_M2_RESULT.json": f["WINDOWS_M2_RESULT.json"].replace(b'"1111', b'"0111', 1)}),
     "code_commit"),
    (lambda f: f.update({"capture_after/runtime_discovery.json": f["capture_after/runtime_discovery.json"].replace(
        b"1.2.3", b"1.2.4")}), "runtime_version_after"),
    (lambda f: f.update({"capture_after/device_profile.json": f["capture_after/device_profile.json"].replace(
        b'"vram_total_mb": 8192', b'"vram_total_mb": 4096')}), "device_identity_stable"),
], ids=["summary", "file-set", "commit", "runtime-version", "device"])
def test_tampered_evidence_is_refused(tmp_path: Path, mutate: Any, expected: str) -> None:
    root = write_evidence(tmp_path / "ev", mutate=mutate)
    assert expected in failing(root)
    with pytest.raises(EvidenceRefused, match="EVIDENCE_NOT_VERIFIED"):
        build_capability_profile(load_evidence(root), PIN, archive_sha256="f" * 64)


def test_identity_failures_are_refused(tmp_path: Path) -> None:
    records = raw_records()
    records[0] = {**records[0], "identity_refusal": "LAYER_DIGEST_MISMATCH", "local_manifest_sha256": "0" * 64}
    root = write_evidence(tmp_path / "ev", raw=records)
    assert {"identity_refusal_null", "local_manifest_sha256"} <= failing(root)
    other = PinnedIdentity(**{**PIN.__dict__, "manifest_digest": "sha256:" + "e" * 64})
    good = write_evidence(tmp_path / "good")
    assert "observed_manifest_digest" in {c.name for c in verify_evidence(load_evidence(good), other)
                                          if c.status != "PASS"}


def test_sha256sums_mismatch_is_detected(tmp_path: Path) -> None:
    root = write_evidence(tmp_path / "ev")
    (root / "BENCHMARK_RAW.jsonl").write_bytes((root / "BENCHMARK_RAW.jsonl").read_bytes() + b"\n")
    assert "internal_sha256sums" in failing(root)


def test_unmapped_benchmark_category_is_refused_not_guessed(tmp_path: Path) -> None:
    records = raw_records(category_override="poetry")
    root = write_evidence(tmp_path / "ev", raw=records)
    with pytest.raises(EvidenceRefused, match="BENCHMARK_CATEGORY_UNMAPPED:poetry"):
        build_capability_profile(load_evidence(root), PIN, archive_sha256="f" * 64)


def test_archive_extraction_checks_digest_and_member_paths(tmp_path: Path) -> None:
    root = write_evidence(tmp_path / "src" / "m2-test")
    archive = tmp_path / "m2-test.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, f"m2-test/{path.relative_to(root).as_posix()}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(EvidenceRefused, match="ARCHIVE_SHA256_MISMATCH"):
        extract_archive(archive, "0" * 64, tmp_path / "out0")
    extracted = extract_archive(archive, digest, tmp_path / "out")
    assert failing(extracted) == set()
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as bundle:
        bundle.writestr("../escape.txt", b"x")
    with pytest.raises(EvidenceRefused, match="ARCHIVE_MEMBER_PATH_UNSAFE"):
        extract_archive(evil, hashlib.sha256(evil.read_bytes()).hexdigest(), tmp_path / "out2")


REAL = os.environ.get("PADOS_M2_1_EVIDENCE_ZIP")


@pytest.mark.skipif(not REAL, reason="set PADOS_M2_1_EVIDENCE_ZIP to the accepted M2.1 archive to run")
def test_real_m2_1_archive_verifies_and_builds(tmp_path: Path) -> None:
    archive = Path(str(REAL))
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sha == "9a7c8cda4d7c7fad4189de40f548df830a7eee41d274c0df91507ecde38509d4"
    pin = PinnedIdentity(
        artifact_ref="qwen3:8b",
        manifest_digest="sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41",
        layer_digests=("sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f",
                       "sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4",
                       "sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12",
                       "sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472e6a12034e4246fbbdb"),
        commit="e0499c722794b98577542e9cbfb464d23bd6973c", tree="a03083d5c9ca2ddeb7aa267384edd90a8c07d95b",
        runtime_version="0.34.3",
        layer_sizes={"sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f": 5_225_374_496})
    evidence = load_evidence(extract_archive(archive, sha, tmp_path / "real"))
    assert all(c.status == "PASS" for c in verify_evidence(evidence, pin))
    profile = build_capability_profile(evidence, pin, archive_sha256=sha)
    assert profile.observation(MeasuredCapability.TIMEOUT_RECOVERY).status is ObservationStatus.INVALID_MEASUREMENT
    assert profile.constraint("GPU_PARTIAL_OFFLOAD") is not None
