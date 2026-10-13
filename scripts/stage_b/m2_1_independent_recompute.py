# SPDX-License-Identifier: AGPL-3.0-only
"""Independent recomputation of BENCHMARK_SUMMARY from BENCHMARK_RAW.jsonl (no PADOS imports), leaf by leaf.

Re-implements every summary figure from the raw records alone, cross-checks each per-call rate against the
runtime's own counters, and classifies every archived leaf as MATCH / MISMATCH / NOT_RECOMPUTABLE. It shares no
code with ``pados.local_intelligence.bench_summary`` on purpose (a second, independent derivation).

    python -I scripts/stage_b/m2_1_independent_recompute.py <evidence_dir> <out.json>
"""
from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


def _num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    count = len(ordered)
    index = min(count - 1, int(round(0.9 * (count - 1))))
    middle = ordered[count // 2] if count % 2 else (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    return {"n": count, "median": round(middle, 3), "p90": round(ordered[index], 3), "min": round(ordered[0], 3),
            "max": round(ordered[-1], 3)}


def _vals(records: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in records if _num(r.get(key))]


def recompute(raw: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any]]:
    calls = [r for r in raw if r["record_type"] == "call"]
    ok = [r for r in calls if r.get("ok")]
    checks = [r for r in raw if r["record_type"] == "check"]
    meta = next(r for r in raw if r["record_type"] == "meta")

    def phase(name: str) -> list[dict[str, Any]]:
        return [r for r in ok if r["phase"] == name]

    rate_issues: list[Any] = []
    for r in ok:
        if _num(r.get("eval_count")) and r.get("eval_duration"):
            if round(r["eval_count"] / (r["eval_duration"] / 1e9), 2) != r.get("gen_tokens_per_s"):
                rate_issues.append([r["task_id"], "gen_tokens_per_s"])
        if _num(r.get("prompt_eval_count")) and r.get("prompt_eval_duration"):
            if round(r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9), 2) != r.get("prompt_tokens_per_s"):
                rate_issues.append([r["task_id"], "prompt_tokens_per_s"])
        if isinstance(r.get("load_duration"), int) and round(r["load_duration"] / 1e9, 3) != r.get("load_s"):
            rate_issues.append([r["task_id"], "load_s"])
    categories: dict[str, dict[str, Any]] = {}
    for check in checks:
        key = check["category"] + (":think" if check.get("think") else "")
        bucket = categories.setdefault(key, {"passed": 0, "total": 0, "tasks": []})
        bucket["total"] += 1
        bucket["passed"] += bool(check["passed"])
        bucket["tasks"].append({"task_id": check["task_id"], "passed": bool(check["passed"])})
    for bucket in categories.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 3)
    context: dict[str, dict[str, Any]] = {}
    for r in [r for r in calls if r["phase"] == "context"]:
        item = context.setdefault(r["task_id"].split("_")[1], {"calls": 0, "ok": 0, "prompt_eval_count": [],
                                                               "ttft_s": [], "gpu_mem_peak_mib": None})
        item["calls"] += 1
        item["ok"] += bool(r.get("ok"))
        if isinstance(r.get("prompt_eval_count"), int):
            item["prompt_eval_count"].append(r["prompt_eval_count"])
        if _num(r.get("ttft_s")):
            item["ttft_s"].append(round(r["ttft_s"], 3))
        peak = (r.get("resources") or {}).get("gpu_mem_used_peak_mib")
        if peak is not None:
            item["gpu_mem_peak_mib"] = max(peak, item["gpu_mem_peak_mib"] or 0)
    residency = [{"phase": s["phase"], "size_bytes": m["size"], "size_vram_bytes": m["size_vram"],
                  "gpu_fraction": round(m["size_vram"] / m["size"], 4), "context_length": m.get("context_length")}
                 for s in raw if s["record_type"] == "ps" for m in s["models"]]
    concurrency = []
    for r in [r for r in raw if r["record_type"] == "concurrency"]:
        tokens = sum(x.get("eval_count") or 0 for x in r["results"] if x["ok"])
        aggregate = round(tokens / r["wall_s"], 2)
        if aggregate != r["aggregate_tokens_per_s"]:
            rate_issues.append([f"concurrency_{r['level']}", "aggregate_tokens_per_s"])
        concurrency.append({"level": r["level"], "wall_s": r["wall_s"], "aggregate_tokens_per_s": aggregate,
                            "errors": sum(1 for x in r["results"] if not x["ok"]),
                            "per_request_wall_s": _stats(_vals(r["results"], "wall_s")),
                            "gpu_mem_peak_mib": r["resources"].get("gpu_mem_used_peak_mib")})

    def peak(key: str) -> Any:
        values = [r["resources"][key] for r in calls if r["resources"].get(key) is not None]
        return max(values) if values else None

    summary = {
        "schema": "pados.stage-b.benchmark-summary.v1", "benchmark_version": meta["benchmark_version"],
        "task_set_digest": meta["task_set_digest"], "model": meta["model"], "seed": meta["seed"],
        "started_at": meta["started_at"], "records": len(raw), "calls": len(calls), "failed_calls": len(calls) - len(ok),
        "pull": next((r for r in raw if r["record_type"] == "pull"), None),
        "latency": {
            "cold_start_wall_s": _stats(_vals(phase("cold_start"), "wall_s")),
            "cold_start_load_s": _stats(_vals(phase("cold_start"), "load_s")),
            "cold_start_ttft_s": _stats(_vals(phase("cold_start"), "ttft_s")),
            "warm_ttft_s": _stats(_vals(phase("warm_start"), "ttft_s")),
            "warm_wall_s": _stats(_vals(phase("warm_start"), "wall_s")),
            "warm_gen_tokens_per_s": _stats(_vals(phase("warm_start"), "gen_tokens_per_s")),
            "gen_tokens_per_s_all": _stats(_vals(ok, "gen_tokens_per_s")),
            "prompt_tokens_per_s_all": _stats(_vals(ok, "prompt_tokens_per_s"))},
        "categories": dict(sorted(categories.items())), "context": context, "residency": residency,
        "concurrency": concurrency,
        "resources": {"gpu_mem_used_peak_mib": peak("gpu_mem_used_peak_mib"),
                      "gpu_util_peak_pct": peak("gpu_util_peak_pct"), "runtime_rss_peak_mib": peak("runtime_rss_peak_mib"),
                      "sys_available_min_mib": min(r["resources"]["sys_available_min_mib"] for r in calls
                                                   if r["resources"].get("sys_available_min_mib") is not None),
                      "gpu_util_mean_pct_by_call": _stats(_vals([r["resources"] for r in calls], "gpu_util_mean_pct")),
                      "cpu_mean_pct_by_call": _stats(_vals([r["resources"] for r in calls], "cpu_mean_pct"))},
        "failures": [{"task_id": r["task_id"], "phase": r["phase"], "error": r.get("error")} for r in calls
                     if not r.get("ok")] + [r for r in raw if r["record_type"] == "phase_error"],
    }
    return summary, rate_issues


def leaves(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, f"{path}.{key}")
    elif isinstance(value, list) and value and all(isinstance(item, dict | list) for item in value):
        for index, item in enumerate(value):
            yield from leaves(item, f"{path}[{index}]")
    else:
        yield path, value


def main(evidence: Path, out: Path) -> int:
    lines = (evidence / "BENCHMARK_RAW.jsonl").read_text(encoding="utf-8").splitlines()
    raw = [json.loads(line) for line in lines if line.strip()]
    archived = json.loads((evidence / "BENCHMARK_SUMMARY.json").read_text(encoding="utf-8"))
    mine, rate_issues = recompute(raw)
    left, right = dict(leaves(archived)), dict(leaves(mine))
    result: dict[str, list[Any]] = {"MATCH": [], "MISMATCH": [], "NOT_RECOMPUTABLE": []}
    for key in sorted(set(left) | set(right)):
        if key not in right:
            result["NOT_RECOMPUTABLE"].append(key)
        elif key not in left or left[key] != right[key]:
            result["MISMATCH"].append([key, left.get(key), right[key]])
        else:
            result["MATCH"].append(key)
    report = {"counts": {k: len(v) for k, v in result.items()}, "whole_object_equal": archived == mine,
              "rate_cross_check_issues": rate_issues, "leaves": result,
              "median_rule": statistics.median.__name__ + "-equivalent (independent implementation)"}
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("counts", "whole_object_equal", "rate_cross_check_issues")}))
    return 0 if not result["MISMATCH"] and not result["NOT_RECOMPUTABLE"] and not rate_issues else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
