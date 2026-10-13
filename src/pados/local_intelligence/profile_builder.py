# SPDX-License-Identifier: AGPL-3.0-only
"""M2 evidence -> verified ``ModelCapabilityProfile`` (measurement only; never registration or authorization).

``verify_evidence`` re-checks an extracted M2 evidence directory against the pinned identity: internal SHA256SUMS
and exact file set, code commit/tree, the pull record (``pull_outcome=SUCCESS`` or ``skipped=ALREADY_PRESENT``,
``identity_refusal=null``, local manifest hash), runtime-reported digests, pinned layer blobs, runtime version and
device consistency, and recomputes ``BENCHMARK_SUMMARY.json`` from ``BENCHMARK_RAW.jsonl`` field by field.
``build_capability_profile`` then derives observations ONLY from raw records of a verified directory, through a
mapping table pinned to the benchmark version (an unknown benchmark version or task category is refused, never
guessed). Anything that fails is ``EvidenceRefused``: no partial profile is produced.

Runtime-specific facts (runtime family, artifact tag, record shapes) are read here, in the evidence adapter, and
leave it only as opaque identity/provenance data of the generic profile contract.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pados.capability.model import Level
from pados.execution.context import canonical_hash
from pados.learning.profiles import wilson_lower_bound
from pados.local_intelligence.bench_summary import summarize
from pados.local_intelligence.bench_tasks import long_context_tasks, quality_tasks, task_set_digest
from pados.resources.capability_profile import (
    CapabilityConstraint,
    CapabilityEvidenceRef,
    CapabilityObservation,
    Dependency,
    DeviceSummary,
    ExecutionConfig,
    MeasuredCapability,
    MeasuredExecutionCondition,
    ModelArtifactIdentity,
    ModelCapabilityProfile,
    ModelRuntimeBinding,
    ObservationStatus,
    OutputConstraint,
    ReasoningMode,
    ResourceFootprint,
    derive_roles,
)
from pados.resources.hardware import AcceleratorRuntime

EVIDENCE_LEVEL = "MACHINE_CAPTURED_RAW_EVIDENCE_VERIFIED"
CORE_FILES = frozenset({"BENCHMARK_RAW.jsonl", "BENCHMARK_SUMMARY.json", "WINDOWS_M2_RESULT.json", "SHA256SUMS",
                        "logs/capture_before.log", "logs/capture_after.log"})
MAX_ARCHIVE_MEMBERS = 256
MAX_MEMBER_BYTES = 16 * 1024 * 1024
# benchmark version -> check category (":think" suffix when the reasoning phase was enabled) -> capability.
CATEGORY_MAP: Mapping[str, Mapping[str, MeasuredCapability]] = {
    "pados-local-bench-1.0.0": {
        "zh_tw_instruction": MeasuredCapability.TEXT_GENERATION_ZH_TW,
        "en_instruction": MeasuredCapability.TEXT_GENERATION_EN,
        "structured_json": MeasuredCapability.STRUCTURED_JSON,
        "schema_adherence": MeasuredCapability.SCHEMA_ADHERENCE,
        "extraction": MeasuredCapability.INFORMATION_EXTRACTION,
        "reasoning": MeasuredCapability.REASONING,
        "reasoning:think": MeasuredCapability.REASONING_EXTENDED,
        "summarization": MeasuredCapability.SUMMARIZATION,
        "multi_step": MeasuredCapability.MULTI_STEP_INSTRUCTION,
        "tool_call": MeasuredCapability.TOOL_CALL_FORMATTING,
        "refusal_inability": MeasuredCapability.INABILITY_ACKNOWLEDGEMENT,
        "determinism": MeasuredCapability.DETERMINISM,
    },
}
# 1.1.0 (M4.1) changed only harness instrumentation (total request deadline, process/config telemetry,
# concurrency semantics); its task categories are identical.
CATEGORY_MAP = {**CATEGORY_MAP, "pados-local-bench-1.1.0": CATEGORY_MAP["pados-local-bench-1.0.0"]}
CODE_TASKS: Mapping[str, MeasuredCapability] = {"code_generate": MeasuredCapability.CODE_GENERATION,
                                                "code_repair": MeasuredCapability.CODE_REPAIR}
CONTEXT_CATEGORIES = {"long_context_8k": 8192, "long_context_16k": 16384}
SERIAL_EPSILON_S = 0.1


class EvidenceRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PinnedIdentity:
    """What the evidence must prove. The tag is only how the runtime was asked; digests are the identity."""

    artifact_ref: str
    manifest_digest: str
    layer_digests: tuple[str, ...]
    commit: str
    tree: str
    runtime_version: str
    layer_sizes: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | FAIL
    detail: str


@dataclass(frozen=True)
class M2Evidence:
    root: Path
    files: Mapping[str, bytes]
    raw: tuple[dict[str, Any], ...]
    summary: dict[str, Any]
    result: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    runtime_before: dict[str, Any]
    runtime_after: dict[str, Any]

    def sha256(self, name: str) -> str:
        return hashlib.sha256(self.files[name]).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_archive(archive: Path, expected_sha256: str, destination: Path) -> Path:
    """Verify the external digest, then extract into an EMPTY directory with member/path/size checks."""
    if sha256_file(archive) != expected_sha256:
        raise EvidenceRefused("ARCHIVE_SHA256_MISMATCH")
    if destination.exists() and any(destination.iterdir()):
        raise EvidenceRefused("EXTRACT_DESTINATION_NOT_EMPTY")
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise EvidenceRefused("ARCHIVE_TOO_MANY_MEMBERS")
        tops = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename or ":" in path.parts[0]:
                raise EvidenceRefused("ARCHIVE_MEMBER_PATH_UNSAFE")
            if member.file_size > MAX_MEMBER_BYTES or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise EvidenceRefused("ARCHIVE_MEMBER_REFUSED")
            tops.add(path.parts[0])
        if len(tops) != 1:
            raise EvidenceRefused("ARCHIVE_LAYOUT_INVALID")
        destination.mkdir(parents=True, exist_ok=True)
        bundle.extractall(destination)
    return destination / tops.pop()


def _json(data: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceRefused(code) from exc
    if not isinstance(value, dict):
        raise EvidenceRefused(code)
    return value


def load_evidence(root: Path) -> M2Evidence:
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    missing = CORE_FILES - set(files)
    if missing:
        raise EvidenceRefused("EVIDENCE_FILE_MISSING:" + sorted(missing)[0])
    try:
        raw = tuple(json.loads(line) for line in files["BENCHMARK_RAW.jsonl"].decode("utf-8").splitlines()
                    if line.strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceRefused("BENCHMARK_RAW_UNPARSEABLE") from exc
    if not all(isinstance(item, dict) for item in raw):
        raise EvidenceRefused("BENCHMARK_RAW_UNPARSEABLE")
    get = files.get
    return M2Evidence(
        root=root, files=files, raw=raw,
        summary=_json(files["BENCHMARK_SUMMARY.json"], "SUMMARY_UNPARSEABLE"),
        result=_json(files["WINDOWS_M2_RESULT.json"], "RESULT_UNPARSEABLE"),
        before=_json(get("capture_before/device_profile.json") or b"", "DEVICE_PROFILE_UNPARSEABLE"),
        after=_json(get("capture_after/device_profile.json") or b"", "DEVICE_PROFILE_UNPARSEABLE"),
        runtime_before=_json(get("capture_before/runtime_discovery.json") or b"", "RUNTIME_DISCOVERY_UNPARSEABLE"),
        runtime_after=_json(get("capture_after/runtime_discovery.json") or b"", "RUNTIME_DISCOVERY_UNPARSEABLE"))


# --- verification ---------------------------------------------------------------------------------------------

def _leaves(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}")
    elif isinstance(value, list) and value and all(isinstance(item, dict | list) for item in value):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def recompute_summary(evidence: M2Evidence) -> dict[str, list[str]]:
    """Every archived summary leaf classified MATCH / MISMATCH / NOT_RECOMPUTABLE against a fresh recomputation."""
    archived = dict(_leaves(evidence.summary))
    fresh = dict(_leaves(summarize(list(evidence.raw))))
    result: dict[str, list[str]] = {"MATCH": [], "MISMATCH": [], "NOT_RECOMPUTABLE": []}
    for key in sorted(set(archived) | set(fresh)):
        if key not in fresh:
            result["NOT_RECOMPUTABLE"].append(key)
        elif key not in archived or archived[key] != fresh[key]:
            result["MISMATCH"].append(key)
        else:
            result["MATCH"].append(key)
    return result


def _stable_device(profile: Mapping[str, Any]) -> dict[str, Any]:
    gpu = profile.get("gpu") or {}
    return {"os": profile.get("os"), "os_version": profile.get("os_version"),
            "architecture": profile.get("architecture"), "cpu": profile.get("cpu"),
            "ram_total_mb": (profile.get("memory") or {}).get("total_mb"),
            "hostname_sha256": (profile.get("machine") or {}).get("hostname_sha256"),
            "gpus": [{k: d.get(k) for k in ("name", "vendor", "vram_total_mb", "compute_capability", "driver_version",
                                            "pci_bus_id")} for d in gpu.get("devices") or []],
            "cuda_driver_version": (gpu.get("nvidia") or {}).get("cuda_driver_version")}


def device_profile_id(profile: Mapping[str, Any]) -> str:
    """Identity of the device from stable facts only (load, free memory and timestamps excluded)."""
    return canonical_hash({"schema": "pados.local-intelligence.device-identity.v1", **_stable_device(profile)})


def _runtime(discovery: Mapping[str, Any]) -> Mapping[str, Any]:
    runtimes = discovery.get("runtimes") or []
    return runtimes[0] if len(runtimes) == 1 and isinstance(runtimes[0], dict) else {}


def verify_evidence(evidence: M2Evidence, pin: PinnedIdentity) -> tuple[Check, ...]:
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, "PASS" if ok else "FAIL", detail[:300]))

    listed: dict[str, str] = {}
    for line in evidence.files["SHA256SUMS"].decode("utf-8").splitlines():
        if line.strip():
            digest, _, name = line.strip().partition("  ")
            listed[name.lstrip("*")] = digest
    actual = {name for name in evidence.files if name != "SHA256SUMS"}
    check("internal_sha256sums", all(evidence.sha256(n) == d for n, d in listed.items() if n in evidence.files),
          f"{len(listed)} entries")
    capture_files = set()
    for phase in ("before", "after"):
        log = _json(evidence.files[f"logs/capture_{phase}.log"], "CAPTURE_LOG_UNPARSEABLE")
        capture_files |= {f"capture_{phase}/{name}" for name in log.get("files") or []}
    expected = (CORE_FILES - {"SHA256SUMS"}) | capture_files
    check("exact_file_set", set(listed) == actual == expected,
          f"listed={len(listed)} actual={len(actual)} expected={len(expected)}")
    identity = evidence.result.get("identity") or {}
    check("code_commit", identity.get("commit") == pin.commit == identity.get("expected_commit"))
    check("code_tree", identity.get("tree") == pin.tree == identity.get("expected_tree"))
    check("code_clean_and_parity", identity.get("tracked_changes") is False and identity.get("parity") == "PASS")
    check("run_status", evidence.result.get("status") == "COMPLETED")
    check("approved_manifest", evidence.result.get("approved_manifest_digest") == pin.manifest_digest)
    check("approved_layers", tuple(evidence.result.get("approved_layers") or ()) == pin.layer_digests)
    pulls = [r for r in evidence.raw if r.get("record_type") == "pull"]
    pull = pulls[0] if len(pulls) == 1 else {}
    check("pull_outcome", pull.get("pull_outcome") == "SUCCESS" or pull.get("skipped") == "ALREADY_PRESENT",
          str(pull.get("pull_outcome") or pull.get("skipped")))
    check("identity_refusal_null", "identity_refusal" in pull and pull.get("identity_refusal") is None)
    check("local_manifest_sha256", f"sha256:{pull.get('local_manifest_sha256')}" == pin.manifest_digest)
    check("observed_manifest_digest", pull.get("observed_manifest_digest") == pin.manifest_digest)
    check("pull_record_pinned_layers", tuple(pull.get("pinned_layers") or ()) == pin.layer_digests)
    for phase in ("blobs_before", "blobs_after"):
        blobs = evidence.result.get(phase) or {}
        ok = all((blobs.get(layer) or {}).get("complete_bytes") is not None
                 and (blobs.get(layer) or {}).get("partial_files") == 0 for layer in pin.layer_digests)
        ok = ok and all((blobs.get(layer) or {}).get("complete_bytes") == size for layer, size in pin.layer_sizes.items())
        check(f"pinned_layer_blobs_{phase.split('_')[1]}", ok)
    ps_digests = {(m or {}).get("digest") for r in evidence.raw if r.get("record_type") == "ps"
                  for m in r.get("models") or []}
    check("runtime_loaded_digest", ps_digests == {pin.manifest_digest.removeprefix("sha256:")}, str(len(ps_digests)))
    for phase, discovery in (("before", evidence.runtime_before), ("after", evidence.runtime_after)):
        runtime = _runtime(discovery)
        listed_models = {m.get("name"): m.get("digest") for m in runtime.get("models") or []}
        check(f"runtime_version_{phase}", runtime.get("version") == pin.runtime_version, str(runtime.get("version")))
        check(f"runtime_lists_pinned_digest_{phase}",
              listed_models.get(pin.artifact_ref) == pin.manifest_digest.removeprefix("sha256:"))
    check("device_identity_stable", device_profile_id(evidence.before) == device_profile_id(evidence.after))
    meta = next((r for r in evidence.raw if r.get("record_type") == "meta"), {})
    check("benchmark_version_mapped", meta.get("benchmark_version") in CATEGORY_MAP,
          str(meta.get("benchmark_version")))
    check("benchmark_model_is_pinned_ref", meta.get("model") == pin.artifact_ref)
    check("benchmark_completed", any(r.get("record_type") == "meta_end" for r in evidence.raw)
          and not any(r.get("record_type") == "phase_error" for r in evidence.raw))
    recomputed = recompute_summary(evidence)
    check("summary_recomputed", not recomputed["MISMATCH"] and not recomputed["NOT_RECOMPUTABLE"],
          f"match={len(recomputed['MATCH'])} mismatch={len(recomputed['MISMATCH'])} "
          f"not_recomputable={len(recomputed['NOT_RECOMPUTABLE'])}")
    return tuple(checks)


# --- profile derivation ---------------------------------------------------------------------------------------

def _confidence(samples: int) -> Level:
    return Level.UNKNOWN if samples == 0 else Level.LOW if samples < 10 else Level.MEDIUM if samples < 30 \
        else Level.HIGH


def _distribution(values: list[float]) -> tuple[tuple[str, float], ...]:
    if not values:
        return ()
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return (("median", round(statistics.median(ordered), 3)), ("p90", round(p90, 3)),
            ("min", round(ordered[0], 3)), ("max", round(ordered[-1], 3)))


def _counted(capability: MeasuredCapability, checks: list[dict[str, Any]], at: str, *,
             context_tokens: int | None = None, extra_refs: tuple[str, ...] = (),
             ttft: list[float] | None = None) -> CapabilityObservation:
    passed = sum(1 for item in checks if item.get("passed") is True)
    total = len(checks)
    return CapabilityObservation(
        capability=capability, status=ObservationStatus.MEASURED, unit="checked_tasks", sample_size=total,
        success_count=passed, failure_count=total - passed,
        success_lower_bound=round(wilson_lower_bound(passed, total), 4), measured_value=round(passed / total, 4),
        distribution=tuple((f"ttft_{name}", value) for name, value in _distribution(ttft or [])),
        context_tokens=context_tokens, confidence=_confidence(total),
        dependencies=(Dependency.DEVICE, Dependency.RUNTIME, Dependency.CONFIGURATION)
        + ((Dependency.CONTEXT,) if context_tokens else ()),
        evidence_refs=tuple(sorted({str(item["task_id"]) for item in checks})) + extra_refs, observed_at=at)


def _timed(capability: MeasuredCapability, values: list[float], unit: str, refs: Iterable[str], at: str,
           *, context_tokens: int | None = None, note: str | None = None) -> CapabilityObservation:
    return CapabilityObservation(
        capability=capability, status=ObservationStatus.MEASURED, unit=unit, sample_size=len(values),
        success_count=len(values), measured_value=round(statistics.median(values), 3),
        distribution=_distribution(values), context_tokens=context_tokens, confidence=_confidence(len(values)),
        evidence_refs=tuple(sorted(set(refs))), observed_at=at, note=note)


def _serialized(results: list[dict[str, Any]]) -> bool:
    """True when each request's first token arrived only after the previous request finished (a queue)."""
    ordered = sorted((r for r in results if r.get("ok")), key=lambda r: float(r.get("ttft_s") or 0.0))
    return len(ordered) > 1 and all(float(b.get("ttft_s") or 0.0) >= float(a.get("wall_s") or 0.0) - SERIAL_EPSILON_S
                                    for a, b in zip(ordered, ordered[1:], strict=False))


# benchmark version -> the sampling every call of that version used (the client sets it; raw calls record the rest).
BENCHMARK_TEMPERATURE: Mapping[str, float] = {"pados-local-bench-1.0.0": 0.0, "pados-local-bench-1.1.0": 0.0}
# Phases of the pinned benchmark whose calls never carry an output schema (fixed prompts outside the task set).
UNCONSTRAINED_PHASES = frozenset({"cold_start", "warm_start", "determinism"})


def _call_config(call: Mapping[str, Any], *, temperature: float, seed: int | None,
                 schema_tasks: Mapping[str, bool]) -> ExecutionConfig | None:
    """The generic execution configuration one raw call ran under, or None when any part is not on record."""
    options = call.get("options") or {}
    num_ctx, num_predict, think = options.get("num_ctx"), options.get("num_predict"), options.get("think")
    if not isinstance(num_ctx, int) or not isinstance(num_predict, int) or not isinstance(think, bool):
        return None
    task_id = str(call.get("task_id"))
    if task_id in schema_tasks:
        constraint = OutputConstraint.JSON_SCHEMA if schema_tasks[task_id] else OutputConstraint.UNCONSTRAINED
    elif call.get("phase") in UNCONSTRAINED_PHASES:
        constraint = OutputConstraint.UNCONSTRAINED
    else:
        return None
    return ExecutionConfig(context_window=num_ctx, reasoning_mode=ReasoningMode.ENABLED if think else
                           ReasoningMode.DISABLED, temperature=temperature, seed=seed, max_output_tokens=num_predict,
                           output_constraint=constraint)


def execution_conditions(raw: tuple[dict[str, Any], ...], meta: Mapping[str, Any],
                         groups: Mapping[tuple[MeasuredCapability, int | None], list[tuple[str, str]]]
                         ) -> tuple[MeasuredExecutionCondition, ...]:
    """Per measured capability: the ONE execution configuration its raw calls ran under.

    Sources: each raw call's recorded options (context window, output budget, reasoning switch), the run's
    recorded seed, the sampling temperature pinned to the benchmark version (the client always sends it; the raw
    call records do not repeat it), and output-schema use from the pinned task definitions, only when the
    evidence's task-set digest equals theirs. Calls are matched by (phase, task id). A capability whose calls
    disagree on a capability-critical setting, or whose configuration is not fully on record, gets NO condition
    (it then supports no requirement: unknown stays unknown). The output-token floor is the largest completion any
    of its calls produced; a successful call without a token count leaves the floor unknown (the full measured
    budget is then required)."""
    temperature = BENCHMARK_TEMPERATURE.get(str(meta.get("benchmark_version")))
    seed = meta.get("seed") if isinstance(meta.get("seed"), int) else None
    tasks = quality_tasks() + long_context_tasks()
    if temperature is None or task_set_digest(tasks) != meta.get("task_set_digest"):
        return ()
    schema_tasks = {task.task_id: task.format is not None for task in tasks}
    calls = [r for r in raw if r.get("record_type") == "call"]
    conditions: list[MeasuredExecutionCondition] = []
    for (capability, _context), task_ids in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1] or 0)):
        wanted = set(task_ids)
        used = [c for c in calls if (str(c.get("phase")), str(c.get("task_id"))) in wanted]
        configs = [_call_config(c, temperature=temperature, seed=seed, schema_tasks=schema_tasks) for c in used]
        if not used or None in configs or len({c.critical() for c in configs if c is not None}) != 1:
            continue
        present = [c for c in configs if c is not None]
        counted = [value for c in used if isinstance(value := c.get("eval_count"), int) and not isinstance(value, bool)]
        uncounted = any(c.get("ok") and not isinstance(c.get("eval_count"), int) for c in used)
        observed = max(counted) if counted and not uncounted else None
        conditions.append(MeasuredExecutionCondition(
            capabilities=(capability,), config=present[0].model_copy(update={
                "max_output_tokens": min(c.max_output_tokens for c in present)}),
            max_observed_output_tokens=observed, sample_size=len(used),
            evidence_refs=tuple(sorted({str(c.get("task_id")) for c in used}))[:64]))
    return tuple(conditions)


def build_capability_profile(evidence: M2Evidence, pin: PinnedIdentity, *, archive_sha256: str) -> ModelCapabilityProfile:
    failed = [c for c in verify_evidence(evidence, pin) if c.status != "PASS"]
    if failed:
        raise EvidenceRefused("EVIDENCE_NOT_VERIFIED:" + failed[0].name)
    raw = evidence.raw
    meta = next(r for r in raw if r.get("record_type") == "meta")
    mapping = CATEGORY_MAP[str(meta["benchmark_version"])]
    started, finished = str(meta["started_at"]), str(next(r for r in raw if r.get("record_type") == "meta_end")
                                                      ["finished_at"])
    calls = [r for r in raw if r.get("record_type") == "call"]
    ok_calls = [r for r in calls if r.get("ok")]
    checks = [r for r in raw if r.get("record_type") == "check"]
    grouped: dict[tuple[MeasuredCapability, int | None], list[dict[str, Any]]] = {}
    observations: list[CapabilityObservation] = []
    for item in checks:
        category = str(item.get("category"))
        task_id = str(item.get("task_id"))
        if category in CONTEXT_CATEGORIES:
            grouped.setdefault((MeasuredCapability.LONG_CONTEXT_RETRIEVAL, CONTEXT_CATEGORIES[category]), []).append(item)
        elif category == "code" and task_id in CODE_TASKS:
            grouped.setdefault((CODE_TASKS[task_id], None), []).append(item)
        elif category == "stability" and task_id == "timeout_recovery":
            details = item.get("details") or {}
            if details.get("timed_out") == "CLIENT_TIMEOUT" or (
                    details.get("timed_out") == "TOTAL_DEADLINE_EXCEEDED" and details.get("measurement") == "MEASURED"):
                grouped.setdefault((MeasuredCapability.TIMEOUT_RECOVERY, None), []).append(item)
            else:  # the client timeout never fired: recovery after a timeout was not exercised
                observations.append(CapabilityObservation(
                    capability=MeasuredCapability.TIMEOUT_RECOVERY, status=ObservationStatus.INVALID_MEASUREMENT,
                    unit="none", evidence_refs=(task_id, "client_timeout"), observed_at=started,
                    note="timeout not induced: streamed chunks arrived within the per-read client timeout"))
        elif category == "stability" and task_id == "reload_cycles":
            oks = [bool(v) for v in (item.get("details") or {}).get("ok") or []]
            observations.append(CapabilityObservation(
                capability=MeasuredCapability.RELOAD_STABILITY, status=ObservationStatus.MEASURED, unit="cycles",
                sample_size=len(oks), success_count=sum(oks), failure_count=len(oks) - sum(oks),
                success_lower_bound=round(wilson_lower_bound(sum(oks), len(oks)), 4),
                measured_value=round(sum(oks) / len(oks), 4) if oks else None, confidence=_confidence(len(oks)),
                evidence_refs=(task_id,), observed_at=started, note="runtime process restart not automated"))
        else:
            key = category + (":think" if item.get("think") else "")
            if key not in mapping:
                raise EvidenceRefused("BENCHMARK_CATEGORY_UNMAPPED:" + key)
            grouped.setdefault((mapping[key], None), []).append(item)
    for (capability, context), items in sorted(grouped.items(), key=lambda kv: (kv[0][0].value, kv[0][1] or 0)):
        if capability is MeasuredCapability.DETERMINISM:
            digests = (items[0].get("details") or {}).get("digests") or []
            same = [d == digests[0] and d is not None for d in digests]
            observations.append(CapabilityObservation(
                capability=capability, status=ObservationStatus.MEASURED, unit="identical_runs",
                sample_size=len(same), success_count=sum(same), failure_count=len(same) - sum(same),
                success_lower_bound=round(wilson_lower_bound(sum(same), len(same)), 4),
                measured_value=round(sum(same) / len(same), 4), confidence=_confidence(len(same)),
                evidence_refs=("determinism",), observed_at=started,
                note="temperature 0 with a fixed seed, identical output digests across runs"))
        else:
            ids = {item.get("task_id") for item in items}
            ttft = [float(r["ttft_s"]) for r in ok_calls if context and r.get("task_id") in ids
                    and isinstance(r.get("ttft_s"), int | float)]
            observations.append(_counted(capability, items, started, context_tokens=context, ttft=ttft))

    def values(phase: str, key: str) -> list[float]:
        return [float(r[key]) for r in ok_calls if r.get("phase") == phase and isinstance(r.get(key), int | float)]

    cold_refs = [str(r["task_id"]) for r in ok_calls if r.get("phase") == "cold_start"]
    warm_refs = [str(r["task_id"]) for r in ok_calls if r.get("phase") == "warm_start"]
    observations.append(_timed(MeasuredCapability.COLD_START_LATENCY, values("cold_start", "wall_s"), "s", cold_refs,
                               started, context_tokens=8192, note="unload then first request, includes model load"))
    observations.append(_timed(MeasuredCapability.WARM_LATENCY, values("warm_start", "ttft_s"), "s", warm_refs, started,
                               context_tokens=8192, note="time to first token, model resident"))
    observations.append(_timed(MeasuredCapability.GENERATION_THROUGHPUT, values("warm_start", "gen_tokens_per_s"),
                               "tokens_per_s", warm_refs, started, context_tokens=8192))
    def phase_calls(phase: str) -> list[tuple[str, str]]:
        return [(phase, str(r.get("task_id"))) for r in calls if r.get("phase") == phase]

    condition_groups: dict[tuple[MeasuredCapability, int | None], list[tuple[str, str]]] = {
        key: [(str(item.get("phase")), str(item.get("task_id"))) for item in items]
        for key, items in grouped.items() if key[0] is not MeasuredCapability.DETERMINISM}
    condition_groups[(MeasuredCapability.DETERMINISM, None)] = phase_calls("determinism")
    condition_groups[(MeasuredCapability.COLD_START_LATENCY, None)] = phase_calls("cold_start")
    condition_groups[(MeasuredCapability.WARM_LATENCY, None)] = phase_calls("warm_start")
    condition_groups[(MeasuredCapability.GENERATION_THROUGHPUT, None)] = phase_calls("warm_start")
    prompt = [float(r["prompt_tokens_per_s"]) for r in ok_calls if isinstance(r.get("prompt_tokens_per_s"), int | float)]
    observations.append(_timed(MeasuredCapability.PROMPT_THROUGHPUT, prompt, "tokens_per_s",
                               [str(r["task_id"]) for r in ok_calls], started, note="all successful calls"))
    concurrency = [r for r in raw if r.get("record_type") == "concurrency"]
    clean = [int(r["level"]) for r in concurrency if r.get("errors") == 0]
    if concurrency:
        observations.append(CapabilityObservation(
            capability=MeasuredCapability.CONCURRENT_REQUESTS, status=ObservationStatus.MEASURED, unit="requests",
            sample_size=len(concurrency), success_count=len(clean), failure_count=len(concurrency) - len(clean),
            measured_value=float(max(clean)) if clean else None,
            distribution=tuple((f"aggregate_tps_l{int(r['level'])}", float(r["aggregate_tokens_per_s"]))
                               for r in concurrency if isinstance(r.get("aggregate_tokens_per_s"), int | float)),
            confidence=_confidence(len(concurrency)), evidence_refs=tuple(f"concurrency_l{int(r['level'])}"
                                                                          for r in concurrency),
            observed_at=started, note="largest concurrent request count completed without errors"))

    constraints: list[CapabilityConstraint] = []
    residency = [(int(m.get("context_length") or 0), m) for r in raw if r.get("record_type") == "ps"
                 for m in r.get("models") or [] if isinstance(m.get("size"), int) and isinstance(m.get("size_vram"), int)]
    partial = sorted((ctx, m["size_vram"] / m["size"]) for ctx, m in residency if m["size_vram"] < m["size"])
    if partial:
        constraints.append(CapabilityConstraint(
            code="GPU_PARTIAL_OFFLOAD", value=round(partial[0][1], 4), unit="gpu_fraction",
            applies_from_context_tokens=partial[0][0], capabilities=(MeasuredCapability.LONG_CONTEXT_RETRIEVAL,),
            evidence_refs=tuple(sorted({str(r["phase"]) for r in raw if r.get("record_type") == "ps"
                                        and any(m.get("context_length") == partial[0][0] for m in r.get("models") or [])})),
            note="model not fully GPU-resident at this context on the measured device"))
        degraded = [float(r["gen_tokens_per_s"]) for r in ok_calls if r.get("phase") == "context"
                    and (r.get("options") or {}).get("num_ctx", 0) >= partial[0][0]
                    and isinstance(r.get("gen_tokens_per_s"), int | float)]
        if degraded:
            constraints.append(CapabilityConstraint(
                code="THROUGHPUT_DEGRADED_AT_CONTEXT", value=round(statistics.median(degraded), 2),
                unit="tokens_per_s", applies_from_context_tokens=partial[0][0],
                capabilities=(MeasuredCapability.GENERATION_THROUGHPUT,), evidence_refs=("context",)))
    if any(_serialized(list(r.get("results") or [])) for r in concurrency):
        constraints.append(CapabilityConstraint(
            code="CONCURRENCY_SERIALIZED", value=1.0, unit="effective_parallelism",
            capabilities=(MeasuredCapability.CONCURRENT_REQUESTS,), evidence_refs=("concurrency",),
            note="each request started generating only after the previous one finished"))
    cold = values("cold_start", "wall_s")
    if cold and max(cold) > 2 * statistics.median(cold):
        constraints.append(CapabilityConstraint(
            code="FIRST_LOAD_OUTLIER", value=round(max(cold), 3), unit="s",
            capabilities=(MeasuredCapability.COLD_START_LATENCY,), evidence_refs=(cold_refs[0],),
            note="first load after the run started was much slower than later reloads"))
    rss = {(r.get("resources") or {}).get("runtime_rss_peak_mib") for r in calls}
    partial_ram = bool(partial)
    if partial_ram and len({v for v in rss if v is not None}) <= 3 and max((v or 0) for v in rss) < 1024:
        constraints.append(CapabilityConstraint(
            code="RUNTIME_RSS_NOT_CAPTURED", value=float(max((v or 0) for v in rss)), unit="MiB",
            note="runtime process RSS stayed flat while the model was partly CPU-resident, RAM use not attributable"))
    constraints.append(CapabilityConstraint(code="SINGLE_BENCHMARK_RUN", value=1.0, unit="runs",
                                            note="one run on one device, no cross-run variance measured"))
    constraints.append(CapabilityConstraint(code="RUNTIME_SERVER_CONFIGURATION_NOT_CAPTURED",
                                            note="server-side runtime settings were not recorded by the benchmark"))

    full = sorted(ctx for ctx, m in residency if m["size_vram"] == m["size"])
    base_ctx = full[-1] if full else 8192
    base_calls = [r for r in calls if (r.get("options") or {}).get("num_ctx") == base_ctx]
    gpu_peak = max((int((r.get("resources") or {}).get("gpu_mem_used_peak_mib") or 0) for r in base_calls), default=0)
    resident = max((int(m["size"]) for ctx, m in residency if ctx == base_ctx), default=0) // 2**20
    pull = next(r for r in raw if r.get("record_type") == "pull")
    tags: Mapping[str, Any] = next((m for m in _runtime(evidence.runtime_before).get("models") or []
                                    if m.get("name") == pin.artifact_ref), {})
    runtime = _runtime(evidence.runtime_before)
    device_id = device_profile_id(evidence.before)
    gpu = ((evidence.before.get("gpu") or {}).get("devices") or [{}])[0]
    cpu = evidence.before.get("cpu") or {}
    contexts = sorted({int((r.get("options") or {}).get("num_ctx") or 0) for r in calls} - {0})
    artifact = ModelArtifactIdentity(
        runtime_family=str(runtime.get("runtime_type")), artifact_ref=pin.artifact_ref,
        manifest_digest=pin.manifest_digest, layer_digests=pin.layer_digests, model_family=str(tags.get("family")),
        parameter_size=tags.get("parameter_size"), quantization=tags.get("quantization"),
        weights_format=tags.get("format"))
    binding = ModelRuntimeBinding(
        runtime_family=artifact.runtime_family, runtime_version=pin.runtime_version, protocol="native",
        endpoint_scope="LOOPBACK", accelerator=AcceleratorRuntime.CUDA if (evidence.before.get("derived") or {}).get(
            "cuda_available") else AcceleratorRuntime.CPU, device_profile_id=device_id,
        configuration=(("MEASURED_CONTEXT_LENGTHS", ",".join(str(c) for c in contexts)),
                       ("SAMPLING", "temperature=0 seed=" + str(meta.get("seed"))),
                       ("SERVER_SETTINGS", "NOT_CAPTURED")))
    device = DeviceSummary(
        device_profile_id=device_id, os_family=str(evidence.before.get("os")),
        machine_class=str((evidence.before.get("derived") or {}).get("machine_class")),
        gpu_model=gpu.get("name"), gpu_vram_mb=int(gpu.get("vram_total_mb") or 0), cpu_model=cpu.get("model"),
        cpu_logical_cores=int(cpu.get("logical_cores") or 1),
        ram_mb=int((evidence.before.get("memory") or {}).get("total_mb") or 1))
    evidence_ref = CapabilityEvidenceRef(
        archive_sha256=archive_sha256, raw_sha256=evidence.sha256("BENCHMARK_RAW.jsonl"),
        summary_sha256=evidence.sha256("BENCHMARK_SUMMARY.json"), result_sha256=evidence.sha256("WINDOWS_M2_RESULT.json"),
        benchmark_version=str(meta["benchmark_version"]), task_set_digest=str(meta["task_set_digest"]))
    options = sorted({json.dumps(r.get("options") or {}, sort_keys=True) for r in calls})
    config_digest = canonical_hash({"benchmark_version": meta["benchmark_version"],
                                    "task_set_digest": meta["task_set_digest"], "seed": meta.get("seed"),
                                    "options": options})
    evidence_digest = canonical_hash(evidence_ref.model_dump(mode="json"))
    ordered = tuple(sorted(observations, key=lambda o: (o.capability.value, o.context_tokens or 0)))
    if any(not math.isfinite(o.measured_value) for o in ordered if o.measured_value is not None):
        raise EvidenceRefused("OBSERVATION_NOT_FINITE")
    return ModelCapabilityProfile.create(
        artifact=artifact, binding=binding, device=device, benchmark_config_digest=config_digest,
        benchmark_evidence_digest=evidence_digest, evidence=evidence_ref, measured_from=started,
        measured_until=finished, observations=ordered, constraints=tuple(constraints),
        footprint=ResourceFootprint(base_context_tokens=base_ctx, gpu_memory_mib=gpu_peak, resident_size_mib=resident),
        execution_conditions=execution_conditions(raw, meta, {
            key: ids for key, ids in condition_groups.items()
            if any(o.capability is key[0] and o.status is ObservationStatus.MEASURED for o in ordered)}),
        roles=derive_roles(ordered), evidence_level=EVIDENCE_LEVEL,
        provenance=(("EXECUTION", str(evidence.result.get("execution"))),
                    ("CODE_COMMIT", str((evidence.result.get("identity") or {}).get("commit"))),
                    ("PULL_RECORD", "ALREADY_PRESENT" if pull.get("skipped") == "ALREADY_PRESENT" else "SUCCESS"),
                    ("LOCAL_MANIFEST_PATH_CLASS", "MODEL_STORE/manifests/REGISTRY/NAMESPACE/NAME/TAG"),
                    ("DEVICE_PROFILE_DIGEST_BEFORE", str((evidence.before.get("derived") or {}).get("profile_digest"))),
                    ("DEVICE_PROFILE_DIGEST_AFTER", str((evidence.after.get("derived") or {}).get("profile_digest"))),
                    ("PLATFORM", str((evidence.result.get("environment") or {}).get("platform")))))
