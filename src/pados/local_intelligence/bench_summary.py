# SPDX-License-Identifier: AGPL-3.0-only
"""Summarise raw benchmark records (``BENCHMARK_RAW.jsonl``) into measured figures; recomputable anywhere.

Only counts, rates and order statistics of recorded observations are produced. Nothing is estimated, weighted
or extrapolated: a figure with no observations is ``None``.
"""
from __future__ import annotations

import json
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _values(records: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in records if isinstance(r.get(key), int | float) and not isinstance(r.get(key), bool)]


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "median": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(values), "median": round(statistics.median(values), 3), "p90": round(p90, 3),
            "min": round(ordered[0], 3), "max": round(ordered[-1], 3)}


def _peak(records: Iterable[dict[str, Any]], key: str) -> Any:
    values = [r["resources"][key] for r in records if isinstance(r.get("resources"), dict)
              and r["resources"].get(key) is not None]
    return max(values) if values else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    meta = next((r for r in records if r.get("record_type") == "meta"), {})
    calls = [r for r in records if r.get("record_type") == "call"]
    checks = [r for r in records if r.get("record_type") == "check"]

    def phase(name: str) -> list[dict[str, Any]]:
        return [r for r in calls if r.get("phase") == name and r.get("ok")]

    categories: dict[str, dict[str, Any]] = {}
    for check in checks:
        key = check["category"] + (":think" if check.get("think") else "")
        bucket = categories.setdefault(key, {"passed": 0, "total": 0, "tasks": []})
        bucket["total"] += 1
        bucket["passed"] += 1 if check.get("passed") else 0
        bucket["tasks"].append({"task_id": check["task_id"], "passed": bool(check.get("passed"))})
    for bucket in categories.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 3) if bucket["total"] else None

    context: dict[str, Any] = {}
    for record in [r for r in calls if r.get("phase") == "context"]:
        size = record["task_id"].split("_")[1]
        item = context.setdefault(size, {"calls": 0, "ok": 0, "prompt_eval_count": [], "ttft_s": [], "gpu_mem_peak_mib": None})
        item["calls"] += 1
        item["ok"] += 1 if record.get("ok") else 0
        if isinstance(record.get("prompt_eval_count"), int):
            item["prompt_eval_count"].append(record["prompt_eval_count"])
        if isinstance(record.get("ttft_s"), int | float):
            item["ttft_s"].append(round(record["ttft_s"], 3))
        peak = (record.get("resources") or {}).get("gpu_mem_used_peak_mib")
        if peak is not None:
            item["gpu_mem_peak_mib"] = max(peak, item["gpu_mem_peak_mib"] or 0)
    loaded = [r for r in records if r.get("record_type") == "ps"]
    residency = []
    for snapshot in loaded:
        for model in snapshot.get("models") or []:
            if isinstance(model, dict) and isinstance(model.get("size"), int) and isinstance(model.get("size_vram"), int):
                residency.append({"phase": snapshot["phase"], "size_bytes": model["size"], "size_vram_bytes": model["size_vram"],
                                  "gpu_fraction": round(model["size_vram"] / model["size"], 4) if model["size"] else None,
                                  "context_length": model.get("context_length")})
    concurrency = [{k: r.get(k) for k in ("level", "wall_s", "aggregate_tokens_per_s", "errors")}
                   | {"per_request_wall_s": _stats(_values(r.get("results") or [], "wall_s")),
                      "gpu_mem_peak_mib": (r.get("resources") or {}).get("gpu_mem_used_peak_mib")}
                   for r in records if r.get("record_type") == "concurrency"]
    pull = next((r for r in records if r.get("record_type") == "pull"), None)
    failures = [{"task_id": r.get("task_id"), "phase": r.get("phase"), "error": r.get("error")}
                for r in calls if not r.get("ok")] + [r for r in records if r.get("record_type") == "phase_error"]
    return {
        "schema": "pados.stage-b.benchmark-summary.v1",
        "benchmark_version": meta.get("benchmark_version"), "task_set_digest": meta.get("task_set_digest"),
        "model": meta.get("model"), "seed": meta.get("seed"), "started_at": meta.get("started_at"),
        "records": len(records), "calls": len(calls), "failed_calls": sum(1 for r in calls if not r.get("ok")),
        "pull": pull,
        "latency": {
            "cold_start_wall_s": _stats(_values(phase("cold_start"), "wall_s")),
            "cold_start_load_s": _stats(_values(phase("cold_start"), "load_s")),
            "cold_start_ttft_s": _stats(_values(phase("cold_start"), "ttft_s")),
            "warm_ttft_s": _stats(_values(phase("warm_start"), "ttft_s")),
            "warm_wall_s": _stats(_values(phase("warm_start"), "wall_s")),
            "warm_gen_tokens_per_s": _stats(_values(phase("warm_start"), "gen_tokens_per_s")),
            "gen_tokens_per_s_all": _stats(_values([r for r in calls if r.get("ok")], "gen_tokens_per_s")),
            "prompt_tokens_per_s_all": _stats(_values([r for r in calls if r.get("ok")], "prompt_tokens_per_s")),
        },
        "categories": dict(sorted(categories.items())),
        "context": context,
        "residency": residency,
        "concurrency": concurrency,
        "resources": {"gpu_mem_used_peak_mib": _peak(calls, "gpu_mem_used_peak_mib"),
                      "gpu_util_peak_pct": _peak(calls, "gpu_util_peak_pct"),
                      "runtime_rss_peak_mib": _peak(calls, "runtime_rss_peak_mib"),
                      "sys_available_min_mib": min((r["resources"]["sys_available_min_mib"] for r in calls
                                                    if (r.get("resources") or {}).get("sys_available_min_mib") is not None),
                                                   default=None),
                      "gpu_util_mean_pct_by_call": _stats(_values([r.get("resources") or {} for r in calls], "gpu_util_mean_pct")),
                      "cpu_mean_pct_by_call": _stats(_values([r.get("resources") or {} for r in calls], "cpu_mean_pct"))},
        "failures": failures,
    }
