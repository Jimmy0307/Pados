# SPDX-License-Identifier: AGPL-3.0-only
"""Document Intelligence benchmark on the synthetic, public-safe NCR corpus (tests/fixtures/document_intelligence).

Every number comes from the GOVERNED path: each query is a task planned and executed by the existing fabric
planner and executor with the pinned local models, and the rankings are read back from the stage receipts'
output artifacts. One run per query yields all four rankings at once (BM25 -> dense -> RRF fusion -> ACTIVE
cross-encoder rerank of the fused top-20), so they are compared on identical inputs.

Measured: Recall@k (grade >= 1), Success@k (a grade-2 passage in the top k), MRR@8 (first grade-2 passage),
nDCG@5 (gain 2^grade - 1), per query style; per-stage receipt latency; model load time and process memory; and,
per NCR document, the two-task workflow (evidence -> grounded 8D report) with citation correctness, abstention
and conflict outcomes, plus refusal probes (cross-project, stale digest, unauthorized source, malformed document,
engine unavailable).

Limitations (also written into the output): six invented documents, 66 passages and 20 English queries; judgments
were written with the corpus by the same author; FIXTURE-provenance recognition (no OCR is measured here); CPU
only; one host. It shows whether the pieces work and how they compare on THIS corpus -- not production quality.

    python scripts/benchmarks/document_intelligence_benchmark.py --out <dir>
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "packs" / "document_intelligence"))

import di_support  # noqa: E402
import psutil  # noqa: E402
from di_support import PROJECT, corpus, workbench  # noqa: E402

from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.capability.graph import DecompositionTemplate  # noqa: E402
from pados.capability.task import TaskContract  # noqa: E402
from pados.packs.document_intelligence.ranking import SCORED_CONTRACT, SCORED_KIND, load_scored  # noqa: E402
from pados.packs.document_intelligence.wiring import evidence_stages, task_type  # noqa: E402
from pados.packs.document_intelligence.workflow import admit_verified_evidence  # noqa: E402
from pados.packs.problem_solving import pack as ps  # noqa: E402
from pados.packs.problem_solving.contracts import ClaimSet, GroundingStatus, ProblemSolvingReport  # noqa: E402
from pados.packs.retrieval.lexical import RANKING_KIND, load_ranking  # noqa: E402

BENCHMARK_VERSION = "pados.document-intelligence.benchmark.v1"
BENCH_TASK_TYPE = "document_retrieval.benchmark"
METHODS = {"bm25": "retrieve", "dense": "dense", "hybrid": "fuse", "hybrid_rerank": "rerank"}
KS = (1, 3, 5)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def _benchmark_template() -> DecompositionTemplate:
    """The evidence template's retrieval stages only (ACTIVE rerank, top 8): rankings, no evidence pack."""
    stages = [stage for stage in evidence_stages("document", "hybrid_rerank", "ACTIVE") if stage["stage_id"] != "evidence"]
    return DecompositionTemplate.model_validate({"template_id": "document-intelligence.benchmark", "version": "1",
                                                 "task_type": BENCH_TASK_TYPE, "graph": {"stages": stages}})


def _ranked(bench: Any, result: Any, stage: str) -> list[str]:
    outputs = bench.outputs(result, stage)
    if stage == "retrieve":
        return [item.passage_id for item in load_ranking(outputs[RANKING_KIND]).results]
    return [item.passage_id for item in load_scored(outputs[SCORED_KIND]).results]


def _metrics(ranked: Sequence[str], judgments: dict[str, int]) -> dict[str, float]:
    relevant = {key for key, grade in judgments.items() if grade >= 1}
    answers = {key for key, grade in judgments.items() if grade >= 2}
    values: dict[str, float] = {}
    for k in KS:
        top = set(ranked[:k])
        values[f"recall@{k}"] = len(top & relevant) / len(relevant)
        values[f"success@{k}"] = 1.0 if top & answers else 0.0
    first = next((index for index, key in enumerate(ranked[:8], start=1) if key in answers), None)
    values["mrr@8"] = 1.0 / first if first else 0.0
    dcg = sum((2 ** judgments.get(key, 0) - 1) / math.log2(index + 1) for index, key in enumerate(ranked[:5], start=1))
    ideal = sorted(judgments.values(), reverse=True)[:5]
    idcg = sum((2 ** grade - 1) / math.log2(index + 1) for index, grade in enumerate(ideal, start=1))
    values["ndcg@5"] = dcg / idcg if idcg else 0.0
    return values


def _mean(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    return {key: round(sum(row[key] for row in rows) / len(rows), 4) for key in rows[0]} if rows else {}


def _retrieval_task(document: Any, query: str, query_id: str) -> TaskContract:
    from pados.capability.task import TaskInput
    from pados.connectivity.requirements import NetworkRequirement
    from pados.enterprise.classification import DataClassification

    return TaskContract(task_id=f"bench-{query_id}", task_type=BENCH_TASK_TYPE, project_id=PROJECT,
                        work_contract_id=di_support.WORK, classification=DataClassification.CONFIDENTIAL,
                        network_policy=NetworkRequirement.LOCAL_ONLY, required_outputs=(SCORED_CONTRACT,),
                        parameters={"query": query},
                        inputs=(TaskInput(name="document", ref=document, project_id=PROJECT,
                                          classification=DataClassification.CONFIDENTIAL),))


def retrieval_benchmark(bench: Any, process: psutil.Process) -> dict[str, Any]:
    bench.wiring.templates.register(_benchmark_template())
    document = bench.put_document(corpus.CASES)
    keys = corpus.passage_keys(corpus.CASES)
    rows: dict[str, list[dict[str, float]]] = {method: [] for method in METHODS}
    by_style: dict[str, dict[str, list[dict[str, float]]]] = {}
    stage_ms: dict[str, list[int]] = {stage: [] for stage in METHODS.values()}
    wall: list[float] = []
    per_query = []
    rss_before = process.memory_info().rss
    for query in corpus.QUERIES:
        started = time.perf_counter()
        plan, result = bench.run(_retrieval_task(document, query.text, query.query_id))
        wall.append((time.perf_counter() - started) * 1000)
        if result is None or result.status is not ExecutionStatus.COMPLETED:
            raise SystemExit(f"benchmark query {query.query_id} did not complete: "
                             f"{plan.status.value if result is None else result.failure_code}")
        receipts = {stage.stage_id: stage for stage in result.plan_receipt.stages}
        record: dict[str, Any] = {"query_id": query.query_id, "style": query.style, "methods": {}}
        for method, stage in METHODS.items():
            ranked = [keys[pid] for pid in _ranked(bench, result, stage)]
            metric = _metrics(ranked, query.judgments)
            rows[method].append(metric)
            by_style.setdefault(query.style, {m: [] for m in METHODS})[method].append(metric)
            stage_ms[stage].append(receipts[stage].duration_ms)
            record["methods"][method] = {"top5": ranked[:5], **metric}
        record["verification"] = sorted({stage.verification.value for stage in receipts.values()})
        per_query.append(record)
    first_query_ms = wall[0]
    return {
        "documents": len(corpus.CASES), "passages": len(keys), "queries": len(corpus.QUERIES),
        "mean": {method: _mean(values) for method, values in rows.items()},
        "by_style": {style: {method: _mean(values) for method, values in methods.items()}
                     for style, methods in sorted(by_style.items())},
        "latency_ms": {
            "query_wall_first_including_model_load": round(first_query_ms, 1),
            "query_wall_median_after_first": round(sorted(wall[1:])[len(wall[1:]) // 2], 1) if len(wall) > 1 else None,
            "stage_median": {stage: sorted(values)[len(values) // 2] for stage, values in stage_ms.items()},
            "stage_max": {stage: max(values) for stage, values in stage_ms.items()},
        },
        "memory_mb": {"rss_before": round(rss_before / 2**20, 1),
                      "rss_after": round(process.memory_info().rss / 2**20, 1),
                      "peak_wset": round(getattr(process.memory_info(), "peak_wset", 0) / 2**20, 1) or None},
        "per_query": per_query,
    }


def _citation_correct(bench: Any, solved: Any, pack: Any, evidence_result: Any, keys: dict[str, str]) -> dict[str, int]:
    claims = ClaimSet.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_CLAIMS])
    report = ProblemSolvingReport.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_REPORT])
    mapping = json.loads(bench.outputs(evidence_result, "evidence")["document_intelligence.evidence-map"])
    passage_of = {entry["evidence_id"]: entry["passage_id"] for entry in mapping["items"]}
    texts = {key: text for _page, key, text in corpus.binder_lines(corpus.CASES)}
    status = {verdict.claim_id: verdict.status for verdict in report.verdicts}
    checked = correct = 0
    for claim in claims.claims:
        if status[claim.claim_id] is not GroundingStatus.GROUNDED:
            continue
        for evidence_id in claim.citations:
            checked += 1
            text = texts[keys[passage_of[evidence_id]]].casefold()
            label, _, value = text.partition(":")
            if label.strip().replace(" ", "_") == claim.subject and claim.assertion.replace("_", " ") in value:
                correct += 1
    return {"grounded_citations_checked": checked, "grounded_citations_correct": correct}


def grounding_benchmark(bench: Any) -> list[dict[str, Any]]:
    results = []
    for case in corpus.CASES:
        cases = (case,)
        document = bench.put_document(cases)
        query = f"{case.ncr} team problem containment root cause corrective action validation prevention closure"
        source_id = f"src.{case.ncr.lower()}"
        task = bench.evidence_task(document, task_type=task_type("document", "hybrid_rerank"), query=query,
                                   case_id=case.case_id, source_id=source_id, task_id=f"evidence-{case.ncr.lower()}")
        plan, result = bench.run(task)
        row: dict[str, Any] = {"case": case.ncr, "evidence_status": result.status.value if result else plan.status.value}
        if result is None or result.status is not ExecutionStatus.COMPLETED:
            results.append(row)
            continue
        pack_ref, pack = admit_verified_evidence(task, plan, result, store=bench.store, provenance=bench.provenance)
        _, solved = bench.run(bench.solve_task(pack_ref, pinned_digest=pack.digest, authorized_sources=source_id,
                                               task_id=f"solve-{case.ncr.lower()}"))
        report = ProblemSolvingReport.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_REPORT])
        row.update({
            "evidence_items": len(pack.items), "structured_items": sum(1 for i in pack.items if i.subject),
            "report_status": report.status.value,
            "sections": {s.section_id: s.status.value for s in report.sections},
            "claims_grounded": sum(1 for v in report.verdicts if v.status is GroundingStatus.GROUNDED),
            "claims_rejected": sum(1 for v in report.verdicts if v.status is not GroundingStatus.GROUNDED),
            **_citation_correct(bench, solved, pack, result, corpus.passage_keys(cases)),
        })
        results.append(row)
    return results


def probes(bench: Any) -> dict[str, str]:
    from pados.enterprise.classification import DataClassification
    from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT

    from pados.capability.provenance import ProvenanceRefused

    case = (corpus.CASES[0],)
    out: dict[str, str] = {}
    try:  # the same bytes are already a proj-a input: re-labelling them for proj-b must be refused
        bench.put_document(case, project_id="proj-b")
        out["relabel_registered_document_to_other_project"] = "ACCEPTED"
    except ProvenanceRefused as exc:
        out["relabel_registered_document_to_other_project"] = exc.code
    foreign = bench.put_document(corpus.CASES[:2], project_id="proj-b")  # distinct bytes owned by proj-b
    plan, _ = bench.run(bench.evidence_task(foreign, task_type=task_type("document", "hybrid_rerank"), query="root cause",
                                            case_id=case[0].case_id, task_id="probe-foreign"))
    out["cross_project_document"] = plan.status.value
    document = bench.put_document(case)
    task = bench.evidence_task(document, task_type=task_type("document", "hybrid_rerank"),
                               query="NCR-2041 root cause containment", case_id=case[0].case_id,
                               source_id="src.ncr-2041", task_id="probe-evidence")
    plan, result = bench.run(task)
    pack_ref, pack = admit_verified_evidence(task, plan, result, store=bench.store, provenance=bench.provenance)
    _, stale = bench.run(bench.solve_task(pack_ref, pinned_digest="0" * 64, authorized_sources="src.ncr-2041",
                                          task_id="probe-stale"))
    out["stale_digest"] = stale.failure_code
    _, foreign_source = bench.run(bench.solve_task(pack_ref, pinned_digest=pack.digest,
                                                   authorized_sources="src.other", task_id="probe-source"))
    out["unauthorized_source"] = foreign_source.failure_code
    garbage = bench.store.put(b'{"schema":"x"}', media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)
    bench.provenance.register(garbage.sha256, project_id=PROJECT, classification=DataClassification.CONFIDENTIAL)
    _, malformed = bench.run(bench.evidence_task(garbage, task_type=task_type("document", "hybrid_rerank"),
                                                 query="root cause", case_id=case[0].case_id, task_id="probe-garbage"))
    out["malformed_document"] = malformed.failure_code
    with tempfile.TemporaryDirectory() as scratch:
        missing = workbench(Path(scratch), installed=False)
        plan, _ = missing.run(missing.evidence_task(missing.put_document(case),
                                                    task_type=task_type("document", "hybrid_rerank"),
                                                    query="root cause", case_id=case[0].case_id))
        out["engine_not_installed_hybrid"] = plan.status.value
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not di_support.live_models_present():
        print("NOT_VALIDATED: pinned models absent (scripts/models/fetch_pinned_models.py)")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    with tempfile.TemporaryDirectory() as scratch:
        bench = workbench(Path(scratch), live=True)
        bench.clock = lambda: datetime.now(UTC)
        load_started = time.perf_counter()
        bench.embedder.embed(["warm"])
        bench.encoder.score("warm", ["warm"])
        load_ms = round((time.perf_counter() - load_started) * 1000, 1)
        retrieval = retrieval_benchmark(bench, process)
        grounding = grounding_benchmark(bench)
        refusals = probes(bench)
        identity = bench.embedder.identity
        record = {
            "benchmark": BENCHMARK_VERSION, "corpus": corpus.CORPUS_VERSION,
            "commit": _git("rev-parse", "HEAD"), "dirty": bool(_git("status", "--porcelain")),
            "recorded_at": datetime.now(UTC).isoformat(),
            "command": "python scripts/benchmarks/document_intelligence_benchmark.py --out <dir>",
            "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                         "python": platform.python_version(), "logical_cpus": psutil.cpu_count(),
                         "ram_gb": round(psutil.virtual_memory().total / 2**30, 1)},
            "models": {engine.pin.pin_id: {"model": engine.pin.model_name, "revision": engine.pin.revision,
                                           "license": engine.pin.license_spdx, "pin_digest": engine.pin.digest}
                       for engine in (bench.embedder, bench.encoder)},
            "engine": None if identity is None else identity.label(),
            "config": {"candidate_pool": 20, "rerank_top_k": 8, "rrf_k": 60, "bm25": {"k1": 1.2, "b": 0.75},
                       "rerank_mode": "ACTIVE (benchmark template)"},
            "model_warm_load_ms": load_ms,
            "retrieval": retrieval, "grounding": grounding, "probes": refusals,
            "limitations": ["synthetic corpus: 6 invented documents, 66 passages, 20 English queries",
                            "judgments written together with the corpus by the same author",
                            "FIXTURE-provenance recognition: OCR quality is not measured here",
                            "CPU only, one host, one run; latency includes governance (planning, receipts, replay)",
                            "not evidence of production quality"],
        }
    (args.out / "document-intelligence-benchmark.json").write_text(json.dumps(record, indent=2, sort_keys=True),
                                                                    encoding="utf-8")
    print(json.dumps({"commit": record["commit"], "mean": retrieval["mean"], "latency_ms": retrieval["latency_ms"],
                      "memory_mb": retrieval["memory_mb"], "model_warm_load_ms": load_ms,
                      "grounding": [{k: v for k, v in row.items() if k != "sections"} for row in grounding],
                      "probes": refusals}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
