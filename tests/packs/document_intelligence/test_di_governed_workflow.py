# SPDX-License-Identifier: AGPL-3.0-only
"""Document -> retrieval -> rerank -> evidence -> grounded report -> ResultCandidate, through the EXISTING fabric.

Task 1 (``document_evidence.document.*``) plans and executes BM25, dense, fusion, rerank and the evidence hand-off
as registered pack capabilities with receipts and verification. Task 2 (``problem_solving.document``) runs the
existing Structured Problem Solving pack over the verified evidence pack, which the TASK pins by digest and
whose source the task authorizes. The verified report becomes a ``ResultCandidate`` only through the existing
work-protocol functions and stays ``PENDING_GOVERNED_ACCEPTANCE``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from di_support import PROJECT, Workbench, corpus, live_models_present, workbench

from pados.capability.executor import ExecutionStatus
from pados.capability.planner import PlanStatus
from pados.capability.receipts import VerificationStatus
from pados.enterprise.classification import DataClassification
from pados.packs.document_intelligence.deterministic_stages import EVIDENCE_MAP_KIND
from pados.packs.document_intelligence.ranking import SCORED_KIND, load_scored
from pados.packs.document_intelligence.workflow import admit_verified_evidence, to_result_candidate
from pados.packs.document_intelligence.wiring import task_type
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT
from pados.packs.problem_solving import pack as ps
from pados.packs.problem_solving.contracts import (
    ClaimSet,
    GroundingStatus,
    ProblemSolvingReport,
    ReportStatus,
    SectionStatus,
)
from pados.work_protocol.models import AuthorizationMetadata, Goal, TaskEnvelope

CASE1 = (corpus.case("case.ncr-2041"),)
QUERY = "NCR-2041 team problem containment root cause corrective action validation prevention closure"
SOURCE = "src.ncr-2041"


def _evidence_run(bench: Workbench, cases: tuple[corpus.Case, ...] = CASE1, *, retrieval: str = "hybrid_rerank",
                  mode: str = "SHADOW", query: str = QUERY) -> tuple[Any, Any]:
    document = bench.put_document(cases)
    task = bench.evidence_task(document, task_type=task_type("document", retrieval, mode),  # type: ignore[arg-type]
                               query=query, case_id=cases[0].case_id, source_id=SOURCE)
    plan, result = bench.run(task)
    bench.last = (task, plan, result)
    return plan, result


def _solve(bench: Workbench, result: Any, *, authorized: str = SOURCE, digest: str | None = None,
           project_id: str = PROJECT) -> tuple[Any, Any, Any]:
    task, plan, ran = bench.last
    assert ran is result
    pack_ref, pack = admit_verified_evidence(task, plan, result, store=bench.store, provenance=bench.provenance)
    task = bench.solve_task(pack_ref, pinned_digest=digest or pack.digest, authorized_sources=authorized,
                            project_id=project_id)
    plan, solved = bench.run(task)
    return pack, plan, solved


def test_the_evidence_task_runs_every_stage_through_the_fabric_with_verification(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    plan, result = _evidence_run(bench)
    assert plan.status is PlanStatus.FEASIBLE
    assert [stage.stage_id for stage in plan.stages] == ["retrieve", "dense", "fuse", "rerank", "evidence"]
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    receipts = {stage.stage_id: stage for stage in result.plan_receipt.stages}
    assert all(stage.verification is VerificationStatus.PASSED for stage in receipts.values())
    assert receipts["dense"].adapter_id == "document-intelligence.dense-fastembed"
    assert receipts["rerank"].adapter_id == "document-intelligence.cross-encoder-fastembed"
    assert result.plan_receipt.verify_structure() and result.semantic_authority == "NONE"

    rerank = load_scored(bench.outputs(result, "rerank")[SCORED_KIND])
    fuse = load_scored(bench.outputs(result, "fuse")[SCORED_KIND])
    assert rerank.rerank_mode == "SHADOW" and rerank.input_rankings == (
        bench.output_ref(result, "fuse", SCORED_KIND).sha256,)
    # SHADOW: the published order is the fused order; the reranker's order is recorded beside it.
    assert [item.passage_id for item in rerank.results] == [item.passage_id for item in fuse.results][:8]
    assert sorted(rerank.shadow_order) == sorted(item.passage_id for item in rerank.results)
    assert all(item.model_score is not None for item in rerank.results)


def test_the_evidence_map_binds_every_item_to_its_passage_and_digest(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    _, result = _evidence_run(bench)
    outputs = bench.outputs(result, "evidence")
    pack = ps.load_evidence_pack(outputs[ps.KIND_EVIDENCE])
    mapping = json.loads(outputs[EVIDENCE_MAP_KIND])
    keys = corpus.passage_keys(CASE1)
    texts = {f"p{page}.l{n}": text for n, (page, _key, text) in enumerate(corpus.binder_lines(CASE1), start=1)}
    assert mapping["evidence_pack_digest"] == pack.digest
    for item, entry in zip(pack.items, mapping["items"], strict=True):
        assert entry["evidence_id"] == item.evidence_id and item.case_id == "case.ncr-2041"
        assert item.source_id == SOURCE and entry["passage_id"] in keys
        if item.subject is not None:  # structured evidence is stated by the very passage it cites
            assert texts[entry["passage_id"]].casefold().startswith(item.subject.replace("_", " "))
    assert "root_cause" in {item.subject for item in pack.items if item.subject}


def test_a_verified_report_becomes_a_pending_result_candidate(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    _, result = _evidence_run(bench)
    pack, plan, solved = _solve(bench, result)
    assert plan.status is PlanStatus.FEASIBLE and solved.status is ExecutionStatus.COMPLETED, solved.failure_code
    (stage,) = solved.plan_receipt.stages
    assert stage.verification is VerificationStatus.PASSED
    outputs = bench.outputs(solved, "solve")
    claims = ClaimSet.model_validate_json(outputs[ps.KIND_CLAIMS])
    report = ProblemSolvingReport.model_validate_json(outputs[ps.KIND_REPORT])
    assert all(verdict.status is GroundingStatus.GROUNDED for verdict in report.verdicts)
    supported = {s.section_id for s in report.sections if s.status is SectionStatus.SUPPORTED}
    assert "D4" in supported  # sections whose evidence ranked below the cut-off abstain; they are never filled

    goal = Goal(protocol_version="1.0", schema_version="pados.goal.v1", goal_id="goal-ncr-2041",
                title="Grounded 8D for NCR-2041", objective="Grounded 8D for NCR-2041",
                required_capabilities=(ps.CAPABILITY_ID,))
    envelope = TaskEnvelope(protocol_version="1.0", schema_version="pados.task-envelope.v1", task_id="task-di-1",
                            original_intent="Produce an evidence-grounded 8D report for NCR-2041.",
                            normalized_goals=(goal,), goal_relations=(), constraints=(), project_ref=PROJECT,
                            classification="CONFIDENTIAL", priority="NORMAL", deadline=None,
                            scope_version_ref="scope:1", created_by="human:synthetic",
                            created_at="2026-11-14T00:00:00+00:00")
    authorization = AuthorizationMetadata(project_ref=PROJECT, classification="CONFIDENTIAL",
                                          scope_version_ref="scope:1", grant_ref="grant:synthetic",
                                          policy_decision_ref="decision:synthetic", capability=ps.CAPABILITY_ID)
    candidate = to_result_candidate(pack=pack, claims=claims, report=report, envelope=envelope,
                                    goal_id="goal-ncr-2041", authorization=authorization, request_id="request-di-1",
                                    result_id="result-di-1", worker_id="worker-di", receipt_hash=stage.receipt_hash)
    assert candidate.semantic_acceptance == "PENDING_GOVERNED_ACCEPTANCE"
    assert candidate.verification_status.value == "NOT_VERIFIED"
    cited = {ref for section in candidate.sections for ref in section.evidence_refs}
    assert cited and cited <= {item.evidence_id for item in pack.items}


def test_a_stale_pinned_digest_is_refused(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    _, result = _evidence_run(bench)
    _, _, solved = _solve(bench, result, digest="0" * 64)
    assert solved.status is ExecutionStatus.FAILED and solved.failure_code == "solve:PS_EVIDENCE_DIGEST_STALE"


def test_an_unauthorized_source_is_refused(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    _, result = _evidence_run(bench)
    _, _, solved = _solve(bench, result, authorized="src.other-document")
    assert solved.status is ExecutionStatus.FAILED
    assert solved.failure_code == "solve:PS_EVIDENCE_SOURCE_UNAUTHORIZED"


def test_another_projects_document_never_reaches_this_projects_task(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    foreign = bench.put_document(CASE1, project_id="proj-b")
    task = bench.evidence_task(foreign, task_type=task_type("document", "hybrid_rerank"), query=QUERY,
                               case_id="case.ncr-2041", source_id=SOURCE)  # task in proj-a names proj-a labels
    plan, result = bench.run(task)
    assert result is None and plan.status is not PlanStatus.FEASIBLE


def test_active_rerank_failure_fails_closed_without_evidence(tmp_path: Path) -> None:
    bench = workbench(tmp_path, cross_fails=True)
    _, result = _evidence_run(bench, mode="ACTIVE")
    assert result.status is ExecutionStatus.FAILED and result.failure_code == "rerank:DI_RERANK_FAILED"
    assert "evidence" not in {stage.stage_id for stage in result.plan_receipt.stages if stage.outputs}


def test_shadow_rerank_failure_keeps_the_prior_order_and_says_so(tmp_path: Path) -> None:
    bench = workbench(tmp_path, cross_fails=True)
    _, result = _evidence_run(bench, mode="SHADOW")
    assert result.status is ExecutionStatus.COMPLETED
    rerank = load_scored(bench.outputs(result, "rerank")[SCORED_KIND])
    fuse = load_scored(bench.outputs(result, "fuse")[SCORED_KIND])
    assert [item.passage_id for item in rerank.results] == [item.passage_id for item in fuse.results][:8]
    assert all(item.model_score is None for item in rerank.results)


def test_without_an_engine_the_hybrid_task_is_infeasible_and_never_falls_back(tmp_path: Path) -> None:
    bench = workbench(tmp_path, installed=False)
    plan, result = _evidence_run(bench)
    assert plan.status is PlanStatus.INFEASIBLE and result is None
    plan, result = _evidence_run(bench, retrieval="lexical")  # the lexical capability is a separate choice
    assert plan.status is PlanStatus.FEASIBLE and result.status is ExecutionStatus.COMPLETED


def test_missing_evidence_abstains_instead_of_filling_the_section(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    cases = (corpus.case("case.ncr-2091"),)
    _, result = _evidence_run(bench, cases, query="NCR-2091 root cause corrective action containment problem")
    _, _, solved = _solve(bench, result)
    report = ProblemSolvingReport.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_REPORT])
    status = {section.section_id: section.status for section in report.sections}
    assert status["D4"] is SectionStatus.ABSTAINED and status["D5"] is SectionStatus.ABSTAINED
    assert report.status in (ReportStatus.INCOMPLETE, ReportStatus.ABSTAINED)


def test_conflicting_evidence_is_never_resolved_by_the_pipeline(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    cases = (corpus.case("case.ncr-2095"),)
    _, result = _evidence_run(bench, cases, query="NCR-2095 root cause valve sticking")
    _, _, solved = _solve(bench, result)
    report = ProblemSolvingReport.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_REPORT])
    assert {s.section_id: s.status for s in report.sections}["D4"] is SectionStatus.CONFLICTED
    assert report.status is ReportStatus.REVIEW_REQUIRED


class _Fabricating:
    """An untrusted provider that invents a root cause and cites unrelated evidence."""

    provider_id = "test.fabricating-provider"

    def draft_claims(self, pack: Any, profile: Any) -> object:
        cited = pack.items[0].evidence_id
        return {"claims": [{"claim_id": "cl.invented", "case_ids": [pack.cases[0]], "section": "D4",
                            "subject": "root_cause", "assertion": "operator_error", "text": "The operator erred.",
                            "citations": [cited]},
                           {"claim_id": "cl.uncited", "case_ids": [pack.cases[0]], "section": "D4",
                            "subject": "root_cause", "assertion": "sabotage", "text": "Sabotage.",
                            "citations": []}]}


def test_unsupported_model_claims_are_never_promoted(tmp_path: Path) -> None:
    bench = workbench(tmp_path, provider=_Fabricating())  # type: ignore[arg-type]
    _, result = _evidence_run(bench)
    _, _, solved = _solve(bench, result)
    assert solved.status is ExecutionStatus.COMPLETED  # execution completes; the claims are rejected
    report = ProblemSolvingReport.model_validate_json(bench.outputs(solved, "solve")[ps.KIND_REPORT])
    assert {verdict.status for verdict in report.verdicts} <= {GroundingStatus.UNSUPPORTED,
                                                               GroundingStatus.CONTRADICTED}
    assert report.status is ReportStatus.REVIEW_REQUIRED
    assert all(section.status is not SectionStatus.SUPPORTED for section in report.sections)


def test_a_malformed_document_fails_closed(tmp_path: Path) -> None:
    bench = workbench(tmp_path)
    garbage = bench.store.put(b'{"schema":"not-a-document"}', media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)
    bench.provenance.register(garbage.sha256, project_id=PROJECT, classification=DataClassification.CONFIDENTIAL)
    task = bench.evidence_task(garbage, task_type=task_type("document", "hybrid_rerank"), query=QUERY,
                               case_id="case.ncr-2041", source_id=SOURCE)
    _, result = bench.run(task)
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_code == "retrieve:RETRIEVAL_DOCUMENT_INVALID"


@pytest.mark.skipif(not live_models_present(), reason="pinned models not fetched")
def test_live_models_run_the_same_governed_workflow(tmp_path: Path) -> None:
    bench = workbench(tmp_path, live=True)
    plan, result = _evidence_run(bench, query="why did the pump housings crack")
    assert plan.status is PlanStatus.FEASIBLE and result.status is ExecutionStatus.COMPLETED, result.failure_code
    receipts = {stage.stage_id: stage for stage in result.plan_receipt.stages}
    assert all(stage.verification is VerificationStatus.PASSED for stage in receipts.values())
    dense = load_scored(bench.outputs(result, "dense")[SCORED_KIND])
    assert dense.model_pin_id == "bge-small-en-v1.5-onnx-q" and dense.engine and "fastembed" in dense.engine
    rerank = load_scored(bench.outputs(result, "rerank")[SCORED_KIND])
    keys = corpus.passage_keys(CASE1)
    assert keys[rerank.shadow_order[0]] in {"c1.root", "c1.discussion", "c1.summary"}
