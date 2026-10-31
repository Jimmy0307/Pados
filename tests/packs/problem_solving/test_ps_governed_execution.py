# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Problem Solving through the existing fabric, end to end, and its hand-off toward acceptance.

* The task (PADOS) pins the evidence digest and names the authorized sources as stage parameters; the adapter
  refuses a stale pack, an unauthorized source, missing or malformed parameters -- closed codes, no content.
* A disabled pack is never planned.
* Execution that completes but fails verification produces no outputs and no completion.
* The verified report reaches a ``ResultCandidate`` only through the EXISTING work-protocol functions
  (``normalize_capability_result`` -> ``build_coverage`` -> ``compose_results``), with PADOS supplying every
  authority field; the candidate stays ``PENDING_GOVERNED_ACCEPTANCE``. The pack never imports that path.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest
from _ps_fixtures import (
    complete_case,
    conflicting_evidence_case,
    insufficient_evidence_case,
    raw_claims,
    unauthorized_source_case,
)
from test_ps_pack import ScriptedProvider, _call, parameters

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capability"))
from fabric_support import OPERATOR, fabric, template  # noqa: E402

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest  # noqa: E402
from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.capability.pack import disable_pack, register_pack  # noqa: E402
from pados.capability.planner import PlanStatus  # noqa: E402
from pados.capability.receipts import VerificationStatus  # noqa: E402
from pados.capability.task import TaskInput  # noqa: E402
from pados.capability.verification import ValidatorRegistry, VerificationEngine  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.packs.problem_solving import pack as ps  # noqa: E402
from pados.packs.problem_solving.contracts import (  # noqa: E402
    EIGHT_D,
    ClaimSet,
    EvidencePack,
    ProblemSolvingReport,
    ReportStatus,
)
from pados.packs.problem_solving.pipeline import build_report, result_projection  # noqa: E402
from pados.work_protocol.compatibility import normalize_capability_result  # noqa: E402
from pados.work_protocol.composition import compose_results  # noqa: E402
from pados.work_protocol.coverage import build_coverage  # noqa: E402
from pados.work_protocol.models import (  # noqa: E402
    AuthorizationMetadata,
    CapabilityRequest,
    ExecutionReceiptRef,
    Goal,
    Provenance,
    ResourceKind,
    TaskEnvelope,
)


# -- task-owned admission at the adapter ----------------------------------------------------------------------
def _refused(pack: EvidencePack, claims: ClaimSet, params: dict[str, Any]) -> CapabilityAdapterError:
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(ScriptedProvider(raw_claims(claims)), pack, params=params)
    assert excinfo.value.retryable is False and excinfo.value.__cause__ is None
    return excinfo.value


def test_a_stale_pinned_digest_is_refused_at_the_adapter() -> None:
    pack, claims = complete_case()
    stale = conflicting_evidence_case()[0].digest
    assert _refused(pack, claims, parameters(pack, evidence_pack_digest=stale)).code == "PS_EVIDENCE_DIGEST_STALE"


def test_an_unauthorized_source_is_refused_at_the_adapter() -> None:
    pack, claims = unauthorized_source_case()
    assert _refused(pack, claims, parameters(pack)).code == "PS_EVIDENCE_SOURCE_UNAUTHORIZED"


@pytest.mark.parametrize("change", [
    {"evidence_pack_digest": None}, {"evidence_pack_digest": "not-a-digest"}, {"authorized_sources": ""},
    {"authorized_sources": "src-ok,,src-ok"}, {"authorized_sources": "src ok"}, {"profile": "dmaic"},
    {"extra": "value"},
])
def test_missing_or_malformed_task_parameters_fail_closed(change: dict[str, Any]) -> None:
    pack, claims = complete_case()
    params = {k: v for k, v in parameters(pack, **change).items() if v is not None}
    assert _refused(pack, claims, params).code == "PS_PARAMETERS_INVALID"


def test_insufficient_evidence_runs_and_abstains() -> None:
    pack, claims = insufficient_evidence_case()
    output, _ = _call(ScriptedProvider(raw_claims(claims)), pack)
    assert output.evidence["report_status"] == "ABSTAINED"
    assert output.evidence["sections_abstained"] == 8


# -- end to end through the existing executor -------------------------------------------------------------------
class _Tampering(ps.ProblemSolvingAdapter):
    """Runs correctly, then forges the report to COMPLETE (with self-consistent evidence keys)."""

    def __init__(self, pack: EvidencePack, claims: ClaimSet) -> None:
        super().__init__(ScriptedProvider(raw_claims(claims)))
        self._forged = build_report(pack, claims, EIGHT_D).model_copy(update={"status": ReportStatus.COMPLETE})

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        output = super().execute(request, context=context)
        forged: list[Any] = []
        for ref in output.outputs:
            if ref.kind != ps.KIND_REPORT:
                forged.append(ref)
                continue
            data = ps.canonical_json(self._forged.model_dump(mode="json"))
            forged.append(context.artifacts.put(data, media_type=ps.JSON_MEDIA_TYPE, kind=ps.KIND_REPORT))
        evidence = dict(output.evidence, artifact_digest=ps.outputs_digest(tuple(forged)), report_status="COMPLETE")
        return StageOutput(outputs=tuple(forged), metrics=output.metrics, evidence=evidence)


def _solve(tmp_path: Path, pack: EvidencePack, claims: ClaimSet, *, adapter: Any = None,
           disable: bool = False) -> tuple[Any, Any, Any]:
    fab = fabric(tmp_path)
    implementation = adapter or ps.ProblemSolvingAdapter(ScriptedProvider(raw_claims(claims)))
    register_pack(fab.registry, ps.MANIFEST, {ps.ADAPTER_ID: implementation}, operator=OPERATOR)
    if disable:
        disable_pack(fab.registry, ps.MANIFEST, operator=OPERATOR)
    fab.schemas.register(ps.OUTPUT_CONTRACT, "problem-solving.schema", ps.report_output_valid)
    references = ValidatorRegistry()
    references.register(ps.OUTPUT_CONTRACT, ps.CHECKER_ID, ps.report_reference_check)
    fab.templates.register(template("problem.solve", [
        {"stage_id": "solve", "capability_id": ps.CAPABILITY_ID, "parameters": parameters(pack),
         "inputs": [{"source": "TASK_INPUT", "name": "evidence"}]}]))
    fab.grants.grant("DOCUMENT_OCR", "TEXT_CHUNK", ps.CAPABILITY_ID)
    source = fab.store.put(ps.canonical_json(pack.model_dump(mode="json")), media_type=ps.JSON_MEDIA_TYPE,
                           kind=ps.KIND_EVIDENCE)
    fab.provenance.register(source.sha256, project_id="proj-a", classification=DataClassification.CONFIDENTIAL)
    task = fab.task(task_type="problem.solve", required_outputs=(ps.OUTPUT_CONTRACT,),
                    inputs=(TaskInput(name="evidence", ref=source, project_id="proj-a",
                                      classification=DataClassification.CONFIDENTIAL),))
    plan = fab.planner().plan(task)
    if plan.status is not PlanStatus.FEASIBLE:
        return fab, plan, None
    executor = fab.executor(verifier=VerificationEngine(schemas=fab.schemas, references=references))
    return fab, plan, executor.execute(plan, task)


def test_a_disabled_problem_solving_pack_is_never_planned(tmp_path: Path) -> None:
    pack, claims = complete_case()
    _, plan, result = _solve(tmp_path, pack, claims, disable=True)
    assert plan.status is PlanStatus.INFEASIBLE and result is None


def test_execution_succeeds_but_verification_fails_yields_no_completion(tmp_path: Path) -> None:
    pack, claims = conflicting_evidence_case()
    adapter = _Tampering(pack, claims)
    _, _, result = _solve(tmp_path, pack, claims, adapter=adapter)
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_code == "solve:VERIFICATION_FAILED"
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.FAILED and stage.outputs == ()
    assert result.semantic_authority == "NONE"


# -- hand-off to the existing ResultCandidate path --------------------------------------------------------------
def _envelope() -> TaskEnvelope:
    goal = Goal(protocol_version="1.0", schema_version="pados.goal.v1", goal_id="goal-solve",
                title="Solve the synthetic problem", objective="Solve the synthetic problem",
                required_capabilities=(ps.CAPABILITY_ID,))
    return TaskEnvelope(protocol_version="1.0", schema_version="pados.task-envelope.v1", task_id="task-ps-1",
                        original_intent="Produce an evidence-grounded 8D report for a synthetic case.",
                        normalized_goals=(goal,), goal_relations=(), constraints=(), project_ref="proj-a",
                        classification="CONFIDENTIAL", priority="NORMAL", deadline=None, scope_version_ref="scope:1",
                        created_by="human:synthetic", created_at="2026-10-31T00:00:00+00:00")


def _artifact(fab: Any, stage: Any, kind: str) -> bytes:
    return bytes(fab.store.get(next(ref for ref in stage.outputs if ref.kind == kind)))


def test_a_verified_report_becomes_a_pending_result_candidate_through_existing_functions(tmp_path: Path) -> None:
    pack, claims = complete_case()
    fab, _, result = _solve(tmp_path, pack, claims)
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.PASSED
    report = ProblemSolvingReport.model_validate_json(_artifact(fab, stage, ps.KIND_REPORT))
    projection = result_projection(pack, claims, report, goal_id="goal-solve")

    # Everything below is PADOS-supplied authority metadata; the pack contributed only ``projection``.
    authorization = AuthorizationMetadata(project_ref="proj-a", classification="CONFIDENTIAL",
                                          scope_version_ref="scope:1", grant_ref="grant:synthetic",
                                          policy_decision_ref="decision:synthetic", capability=ps.CAPABILITY_ID)
    request = CapabilityRequest(protocol_version="1.0", schema_version=CapabilityRequest.SCHEMA,
                                request_id="request-ps-1", task_id="task-ps-1", goal_id="goal-solve",
                                capability=ps.CAPABILITY_ID, authorization=authorization)
    receipt_digest = stage.receipt_hash
    capability_result = normalize_capability_result(
        projection, request=request, result_id="result-ps-1", resource_id=ps.ADAPTER_ID,
        resource_kind=ResourceKind.TOOL, provider=None,  # ``provider`` is reserved for model resources
        provenance=Provenance(worker_id="worker-ps", resource_id=ps.ADAPTER_ID, resource_kind=ResourceKind.TOOL,
                              adapter_id=ps.ADAPTER_ID, adapter_version=ps.ADAPTER.version,
                              normalized_from=ps.PACK_ID,
                              provider_response_digest=hashlib.sha256(repr(projection).encode()).hexdigest(),
                              input_digests=(pack.digest,)),
        authorization=authorization,
        execution_receipt_ref=ExecutionReceiptRef(protocol_version="1.0", schema_version=ExecutionReceiptRef.SCHEMA,
                                                  receipt_id=f"execution-sha256:{receipt_digest}",
                                                  digest=receipt_digest))
    envelope = _envelope()
    candidate = compose_results(envelope, (capability_result,), build_coverage(envelope, (capability_result,)))

    assert candidate.semantic_acceptance == "PENDING_GOVERNED_ACCEPTANCE"
    assert candidate.verification_status.value == "NOT_VERIFIED"  # governed verification has not run yet
    assert len(candidate.sections) == 8 and all(s.goal_id == "goal-solve" for s in candidate.sections)
    assert {ref for s in candidate.sections for ref in s.evidence_refs} == {f"ev-d{n}" for n in range(1, 9)}
