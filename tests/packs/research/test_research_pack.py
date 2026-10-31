# SPDX-License-Identifier: AGPL-3.0-only
"""RESEARCH_CAMPAIGN as a Capability Pack V1 consumer: the external runtime is replaceable and never authority.

PADOS owns the task, policy, authorization, evidence, verification, receipts, ResultCandidate and acceptance; the
research runtime only returns a run. Covered: Pack V1 manifest per operator profile, runtime unavailable (unplannable
and retryable), timeout (non-retryable), agent outside the profile allowlist, stale request digest, classification
above the pack ceiling, end to end through the existing executor, completed execution with failed verification,
and the hand-off to an existing ResultCandidate that stays pending governed acceptance.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest
from _research_fixtures import TIMEOUT, UNAVAILABLE, ScriptedRuntime, output, profile, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capability"))
from fabric_support import OPERATOR, fabric, template  # noqa: E402

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest  # noqa: E402
from pados.capability.artifacts import ArtifactRef, ScopedArtifacts  # noqa: E402
from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.capability.pack import GovernedPackAdapter, PackManifestError, register_pack, validate_pack  # noqa: E402
from pados.capability.planner import PlanStatus  # noqa: E402
from pados.capability.receipts import VerificationStatus  # noqa: E402
from pados.capability.task import TaskInput  # noqa: E402
from pados.capability.verification import ValidatorRegistry, VerificationEngine  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.packs.research import pack as rp  # noqa: E402
from pados.packs.research.contracts import ResearchError, ResearchRunManifest, RunStatus  # noqa: E402
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


PINNED = rp.task_parameters(profile(), rp.canonical_request(request()))["request_digest"]


class _Store:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=media_type,
                          size_bytes=len(payload), kind=kind)
        self.blobs[ref.sha256] = payload
        return ref

    def get(self, ref: ArtifactRef) -> bytes:
        return self.blobs[ref.sha256]


def _direct(runtime: Any, *, campaign: Any = None, params: dict[str, Any] | None = None,
            classification: DataClassification = DataClassification.INTERNAL) -> tuple[StageOutput, _Store]:
    chosen = profile()
    store = _Store()
    payload = rp.canonical_request(campaign or request())
    source = store.put(payload, media_type=rp.JSON_MEDIA_TYPE, kind=rp.KIND_REQUEST)
    context = StageContext(artifacts=ScopedArtifacts(store, readable=(source,)), project_id="proj-a", stage_id="run")
    stage = StageRequest(stage_id="run", capability_id=rp.CAPABILITY_ID, inputs=(source,),
                         parameters=params or rp.task_parameters(chosen, payload), classification=classification)
    governed = GovernedPackAdapter(rp.research_pack_manifest(chosen), rp.ResearchCampaignAdapter(chosen, runtime))
    return governed.execute(stage, context=context), store


def _code(runtime: Any, **kwargs: Any) -> CapabilityAdapterError:
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _direct(runtime, **kwargs)
    assert excinfo.value.__cause__ is None
    return excinfo.value


# -- the pack and its operator profile ---------------------------------------------------------------------------
def test_each_operator_profile_is_one_content_addressed_pack_v1_version() -> None:
    manifest = rp.research_pack_manifest(profile())
    assert validate_pack(manifest.model_dump(mode="json")) == manifest
    assert manifest.capability_ids == (rp.CAPABILITY_ID,)
    assert manifest.runtime.runtime_type.value == "CONTAINER"  # external code never runs in the PADOS process
    assert manifest.provenance.source.value == "OPERATOR_SUPPLIED"
    assert manifest.provenance.source_digest == profile().image_digest
    assert manifest.initial_state.value == "EXPERIMENTAL"  # live UAT outcome is required before VALIDATED
    other = rp.research_pack_manifest(profile(image_digest="2" * 64))
    assert other.manifest_digest != manifest.manifest_digest


@pytest.mark.parametrize("changes", [{"secret_refs": ("plain-value-not-a-secretref",)}, {"agents": ()},
                                     {"egress_hosts": ("169.254.169.254",)}, {"upstream_revision": "main"},
                                     {"image_digest": "latest"}, {"agents": ("Fixture Agent!",)}])
def test_invalid_operator_profiles_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises((ValueError, PackManifestError)):
        rp.research_pack_manifest(profile(**changes))


def test_a_cloud_agent_profile_declares_allowlisted_egress_and_secret_refs_only() -> None:
    cloud = profile(profile_id="agsci.synthetic.cloud", agents=("claude",), egress_hosts=("api.anthropic.com",),
                    secret_refs=("secretref://pados/research/anthropic",))
    manifest = rp.research_pack_manifest(cloud)
    assert manifest.security.egress.value == "ALLOWLIST"
    assert manifest.security.secret_refs == ("secretref://pados/research/anthropic",)


# -- runtime failures ---------------------------------------------------------------------------------------------
def test_an_unavailable_runtime_makes_the_pack_unplannable(tmp_path: Path) -> None:
    fab, task, _ = _solve(tmp_path, ScriptedRuntime(available=False))
    assert fab.planner().plan(task).status is PlanStatus.INFEASIBLE


def test_a_runtime_that_becomes_unavailable_is_a_retryable_closed_failure() -> None:
    error = _code(ScriptedRuntime(raises=UNAVAILABLE))
    assert error.code == "RESEARCH_RUNTIME_UNAVAILABLE" and error.retryable is True


def test_a_timeout_fails_without_retry_or_weaker_fallback() -> None:
    error = _code(ScriptedRuntime(raises=TIMEOUT))
    assert error.code == "RESEARCH_RUNTIME_TIMEOUT" and error.retryable is False


def test_a_crashing_runtime_leaks_no_content() -> None:
    error = _code(ScriptedRuntime(raises=RuntimeError("ANTHROPIC_API_KEY=synthetic-leak")))
    assert error.code == "RESEARCH_RUNTIME_FAILED" and "synthetic-leak" not in repr(error)


@pytest.mark.parametrize(("runtime_output", "code"), [
    ("not a run", "RESEARCH_MANIFEST_MALFORMED"),
    (output(files={"task/task.md": b"x", "../escape": b"x"}), "RESEARCH_ARTIFACT_PATH_UNAUTHORIZED"),
    (output(egress_hosts_contacted=("api.example.org",)), "RESEARCH_EGRESS_VIOLATION"),
])
def test_runtime_output_violations_are_closed_codes(runtime_output: Any, code: str) -> None:
    assert _code(ScriptedRuntime(runtime_output)).code == code


# -- task-owned admission and policy ---------------------------------------------------------------------------
def test_an_agent_outside_the_profile_allowlist_is_never_launched() -> None:
    runtime = ScriptedRuntime()
    assert _code(runtime, campaign=request(agent="codex")).code == "RESEARCH_AGENT_NOT_ALLOWED"
    assert runtime.calls == 0


def test_a_request_that_changed_after_the_task_pinned_it_is_stale() -> None:
    runtime = ScriptedRuntime()
    stale = rp.task_parameters(profile(), rp.canonical_request(request(budget_profile="other")))
    assert _code(runtime, params=stale).code == "RESEARCH_REQUEST_DIGEST_STALE"
    assert runtime.calls == 0


def test_a_request_above_the_pack_classification_ceiling_is_refused() -> None:
    error = _code(ScriptedRuntime(), classification=DataClassification.RESTRICTED)
    assert error.code == "PACK_CLASSIFICATION_CEILING_EXCEEDED"


# -- end to end through the existing fabric ----------------------------------------------------------------------
class _Tampering(rp.ResearchCampaignAdapter):
    """Runs correctly, then swaps a promoted artifact's bytes after the manifest recorded its digest."""

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        produced = super().execute(request, context=context)
        swapped = []
        for ref in produced.outputs:
            if ref.kind == rp.artifact_kind("final_evaluation"):
                ref = context.artifacts.put(b'{"metrics": {"packing_density": 0.99}}', media_type=ref.media_type,
                                            kind=ref.kind)
            swapped.append(ref)
        evidence = dict(produced.evidence, artifact_digest=rp.outputs_digest(tuple(swapped)))
        return StageOutput(outputs=tuple(swapped), metrics=produced.metrics, evidence=evidence)


def _solve(tmp_path: Path, runtime: Any, *, adapter: Any = None) -> tuple[Any, Any, Any]:
    chosen = profile()
    fab = fabric(tmp_path)
    implementation = adapter or rp.ResearchCampaignAdapter(chosen, runtime)
    register_pack(fab.registry, rp.research_pack_manifest(chosen), {implementation.descriptor.adapter_id:
                                                                     implementation}, operator=OPERATOR)
    fab.schemas.register(rp.OUTPUT_CONTRACT, rp.SCHEMA_CHECKER_ID, rp.run_manifest_valid)
    references = ValidatorRegistry()
    references.register(rp.OUTPUT_CONTRACT, rp.REFERENCE_CHECKER_ID, rp.reference_checker(chosen))
    payload = rp.canonical_request(request())
    fab.templates.register(template("research.campaign", [
        {"stage_id": "run", "capability_id": rp.CAPABILITY_ID, "parameters": rp.task_parameters(chosen, payload),
         "inputs": [{"source": "TASK_INPUT", "name": "request"}]}]))
    fab.grants.grant("DOCUMENT_OCR", "TEXT_CHUNK", rp.CAPABILITY_ID)
    source = fab.store.put(payload, media_type=rp.JSON_MEDIA_TYPE, kind=rp.KIND_REQUEST)
    fab.provenance.register(source.sha256, project_id="proj-a", classification=DataClassification.INTERNAL)
    task = fab.task(task_type="research.campaign", classification=DataClassification.INTERNAL,
                    required_outputs=(rp.OUTPUT_CONTRACT,),
                    inputs=(TaskInput(name="request", ref=source, project_id="proj-a",
                                      classification=DataClassification.INTERNAL),))
    return fab, task, VerificationEngine(schemas=fab.schemas, references=references)


def _execute(tmp_path: Path, runtime: Any, **kwargs: Any) -> tuple[Any, Any]:
    fab, task, verifier = _solve(tmp_path, runtime, **kwargs)
    return fab, fab.executor(verifier=verifier).execute(fab.planner().plan(task), task)


def test_a_campaign_runs_only_through_the_existing_executor_and_stays_a_candidate(tmp_path: Path) -> None:
    fab, result = _execute(tmp_path, ScriptedRuntime())
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.PASSED and result.semantic_authority == "NONE"
    manifest = ResearchRunManifest.model_validate_json(
        fab.store.get(next(r for r in stage.outputs if r.kind == rp.KIND_MANIFEST)))
    assert manifest.status.value == "COMPLETED" and manifest.semantic_authority == "NONE"


def test_execution_completes_but_verification_fails_when_an_artifact_is_swapped(tmp_path: Path) -> None:
    chosen = profile()
    _, result = _execute(tmp_path, None, adapter=_Tampering(chosen, ScriptedRuntime()))
    assert result.status is ExecutionStatus.FAILED and result.failure_code == "run:VERIFICATION_FAILED"
    assert result.plan_receipt.stages[0].outputs == ()


# -- hand-off: PADOS supplies every authority field; the candidate stays pending ----------------------------------
def _candidate(outputs: list[tuple[ArtifactRef, bytes]]) -> Any:
    projection = rp.research_projection(outputs, goal_id="goal-research", request_digest=PINNED, profile=profile())
    authorization = AuthorizationMetadata(project_ref="proj-a", classification="INTERNAL", scope_version_ref="scope:1",
                                          grant_ref="grant:synthetic", policy_decision_ref="decision:synthetic",
                                          capability=rp.CAPABILITY_ID)
    capability_request = CapabilityRequest(protocol_version="1.0", schema_version=CapabilityRequest.SCHEMA,
                                           request_id="request-r1", task_id="task-r1", goal_id="goal-research",
                                           capability=rp.CAPABILITY_ID, authorization=authorization)
    digest = "c" * 64
    result = normalize_capability_result(
        projection, request=capability_request, result_id="result-r1", resource_id="research-runtime",
        resource_kind=ResourceKind.TOOL, provider=None,
        provenance=Provenance(worker_id="worker-r", resource_id="research-runtime", resource_kind=ResourceKind.TOOL,
                              adapter_id="research.campaign", adapter_version="1", normalized_from=rp.PACK_ID,
                              provider_response_digest=digest, input_digests=()),
        authorization=authorization,
        execution_receipt_ref=ExecutionReceiptRef(protocol_version="1.0", schema_version=ExecutionReceiptRef.SCHEMA,
                                                  receipt_id=f"execution-sha256:{digest}", digest=digest))
    goal = Goal(protocol_version="1.0", schema_version="pados.goal.v1", goal_id="goal-research",
                title="Run a synthetic research campaign", objective="Run a synthetic research campaign",
                required_capabilities=(rp.CAPABILITY_ID,))
    envelope = TaskEnvelope(protocol_version="1.0", schema_version="pados.task-envelope.v1", task_id="task-r1",
                            original_intent="Run a governed synthetic research campaign.", normalized_goals=(goal,),
                            goal_relations=(), constraints=(), project_ref="proj-a", classification="INTERNAL",
                            priority="NORMAL", deadline=None, scope_version_ref="scope:1", created_by="human:synthetic",
                            created_at="2026-10-31T00:00:00+00:00")
    return result, compose_results(envelope, (result,), build_coverage(envelope, (result,)))


def _manifest(runtime_output: Any) -> list[tuple[ArtifactRef, bytes]]:
    out, store = _direct(ScriptedRuntime(runtime_output))
    return [(ref, store.get(ref)) for ref in out.outputs]


def test_a_complete_run_becomes_a_pending_result_candidate_through_existing_functions() -> None:
    result, candidate = _candidate(_manifest(output()))
    assert result.completed_goals == ("goal-research",)
    assert candidate.semantic_acceptance == "PENDING_GOVERNED_ACCEPTANCE"
    assert candidate.verification_status.value == "NOT_VERIFIED"


def test_unevidenced_claims_never_become_contributions_or_completion() -> None:
    manifest = _manifest(output(claims=({"metric": "packing_density", "value": 0.95},)))
    result, candidate = _candidate(manifest)
    assert result.completed_goals == () and result.unresolved_goals == ("goal-research",)
    assert all("0.95" not in section.content for section in candidate.sections)


def test_a_partial_run_is_at_most_a_partial_goal() -> None:
    phases = {"task_draft": "COMPLETED", "investigation": "COMPLETED", "ablation": "PENDING",
              "final_evaluation": "PENDING", "posthoc": "PENDING", "paper": "PENDING"}
    files = {"task/task.md": b"Synthetic task.\n", "investigation/summary.json": b'{"best_score": 0.5}'}
    result, _ = _candidate(_manifest(output(phases=phases, files=files, claims=())))
    assert result.completed_goals == () and result.partial_goals == ("goal-research",)


# -- independent review: parameters, model allowlist, projection only from verified outputs, hostile checkers ----
@pytest.mark.parametrize("change", [{"extra": "x"}, {"profile_id": "agsci.other.profile"}, {"request_digest": "z"}])
def test_extra_or_foreign_task_parameters_fail_closed(change: dict[str, Any]) -> None:
    params = dict(rp.task_parameters(profile(), rp.canonical_request(request())), **change)
    assert _code(ScriptedRuntime(), params=params).code == "RESEARCH_PARAMETERS_INVALID"


def test_missing_task_parameters_fail_closed() -> None:
    assert _code(ScriptedRuntime(), params={"profile_id": profile().profile_id}).code == "RESEARCH_PARAMETERS_INVALID"


def test_a_model_or_preset_outside_the_profile_is_never_launched() -> None:
    runtime = ScriptedRuntime()
    assert _code(runtime, campaign=request(model="frontier-x")).code == "RESEARCH_AGENT_NOT_ALLOWED"
    assert _code(runtime, campaign=request(preset="gpu-large")).code == "RESEARCH_AGENT_NOT_ALLOWED"
    assert runtime.calls == 0


def _outputs(out: StageOutput, store: _Store) -> list[tuple[ArtifactRef, bytes]]:
    return [(ref, store.get(ref)) for ref in out.outputs]


SWAPPED = b'{"metrics": {"packing_density": 0.99}}'
HUGE = b'{"metrics": {"m": 1' + b"0" * 400 + b"}}"


def test_the_projection_refuses_outputs_that_do_not_verify() -> None:
    out, store = _direct(ScriptedRuntime())
    outputs = _outputs(out, store)
    final = rp.artifact_kind("final_evaluation")
    forged = [(ref, SWAPPED if ref.kind == final else data) for ref, data in outputs]
    with pytest.raises(ResearchError, match="RESEARCH_MANIFEST_UNVERIFIED"):
        rp.research_projection(forged, goal_id="goal-research", request_digest=PINNED, profile=profile())
    assert rp.research_projection(outputs, goal_id="goal-research", request_digest=PINNED, profile=profile())["completed_goals"] == ("goal-research",)


def test_a_completed_run_with_an_unsupported_claim_never_completes_the_goal() -> None:
    out, store = _direct(ScriptedRuntime(output(claims=({"metric": "packing_density", "value": 0.95},))))
    projection = rp.research_projection(_outputs(out, store), goal_id="goal-research", request_digest=PINNED, profile=profile())
    assert out.evidence["run_status"] == "COMPLETED" and out.evidence["review_required"] is True
    assert projection["completed_goals"] == () and projection["unresolved_goals"] == ("goal-research",)


@pytest.mark.parametrize("mutate", ["drop_manifest", "duplicate_manifest", "garbage_manifest", "extra_artifact",
                                    "huge_number"])
def test_checkers_return_false_never_raise_on_hostile_outputs(mutate: str) -> None:
    out, store = _direct(ScriptedRuntime())
    outputs = _outputs(out, store)
    manifest = next(item for item in outputs if item[0].kind == rp.KIND_MANIFEST)
    rest = [item for item in outputs if item[0].kind != rp.KIND_MANIFEST]
    final = rp.artifact_kind("final_evaluation")
    hostile = {
        "drop_manifest": rest,
        "duplicate_manifest": [manifest, manifest, *rest],
        "garbage_manifest": [(manifest[0], b"[" * 5000 + b"]" * 5000), *rest],
        "extra_artifact": [*outputs, rest[0]],
        "huge_number": [manifest, *[(ref, HUGE if ref.kind == final else data) for ref, data in rest]],
    }[mutate]
    assert rp.run_manifest_valid(hostile) in (True, False)
    assert rp.reference_checker(profile())(hostile) is False


# -- Codex review: manifest bytes bound to their reference; verification bound to the pinned campaign ----------


def test_manifest_bytes_that_do_not_match_their_reference_never_verify() -> None:
    out, store = _direct(ScriptedRuntime())
    outputs = _outputs(out, store)
    manifest_ref, manifest_bytes = next(item for item in outputs if item[0].kind == rp.KIND_MANIFEST)
    swapped = manifest_bytes.replace(b'"upstream_run_id":"20261031_000000"', b'"upstream_run_id":"20261031_999999"')
    assert swapped != manifest_bytes
    forged = [(ref, swapped if ref == manifest_ref else data) for ref, data in outputs]
    assert rp.run_manifest_valid(forged) is False and rp.reference_checker(profile())(forged) is False
    with pytest.raises(ResearchError, match="RESEARCH_MANIFEST_UNVERIFIED"):
        rp.research_projection(forged, goal_id="goal-research", request_digest=PINNED, profile=profile())


def test_dropping_pending_phases_cannot_turn_a_partial_run_into_a_completed_one() -> None:
    phases = {"task_draft": "COMPLETED", "investigation": "COMPLETED", "ablation": "PENDING",
              "final_evaluation": "PENDING", "posthoc": "PENDING", "paper": "PENDING"}
    files = {"task/task.md": b"Synthetic task.\n", "investigation/summary.json": b'{"best_score": 0.5}'}
    out, store = _direct(ScriptedRuntime(output(phases=phases, files=files, claims=())))
    outputs = _outputs(out, store)
    manifest_ref, manifest_bytes = next(item for item in outputs if item[0].kind == rp.KIND_MANIFEST)
    manifest = ResearchRunManifest.model_validate_json(manifest_bytes)
    kept = tuple(p for p in manifest.phases if p.state.value == "COMPLETED")
    forged_manifest = manifest.model_copy(update={"phases": kept, "status": RunStatus.COMPLETED})
    payload = rp.canonical_json(forged_manifest.model_dump(mode="json"))
    forged_ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=manifest_ref.media_type,
                             size_bytes=len(payload), kind=rp.KIND_MANIFEST)
    forged = [(forged_ref, payload) if ref == manifest_ref else (ref, data) for ref, data in outputs]
    assert rp.reference_checker(profile())(forged) is False
    with pytest.raises(ResearchError, match="RESEARCH_MANIFEST_UNVERIFIED"):
        rp.research_projection(forged, goal_id="goal-research", request_digest=PINNED, profile=profile())


def test_the_projection_is_bound_to_the_request_the_task_pinned() -> None:
    out, store = _direct(ScriptedRuntime())
    other = rp.task_parameters(profile(), rp.canonical_request(request(budget_profile="other")))["request_digest"]
    with pytest.raises(ResearchError, match="RESEARCH_MANIFEST_UNVERIFIED"):
        rp.research_projection(_outputs(out, store), goal_id="goal-research", request_digest=other,
                               profile=profile())


def test_every_valid_profile_id_yields_a_valid_pack_version() -> None:
    longest = "agsci." + "c" * 32  # 38 characters: the longest accepted profile id
    assert validate_pack(rp.research_pack_manifest(profile(profile_id=longest)).model_dump(mode="json"))
    with pytest.raises(ValueError):
        profile(profile_id="agsci.docker.cpu.claude.research.default")  # 40 characters: refused up front


# -- verification is bound to the registered operator profile -----------------------------------------------------
def _reissued(outputs: list[tuple[ArtifactRef, bytes]], **changes: Any) -> list[tuple[ArtifactRef, bytes]]:
    """Rewrite the manifest with ``changes`` and a matching reference (what a forger with write access could do)."""
    manifest_ref, manifest_bytes = next(item for item in outputs if item[0].kind == rp.KIND_MANIFEST)
    forged = ResearchRunManifest.model_validate_json(manifest_bytes).model_copy(update=changes)
    payload = rp.canonical_json(forged.model_dump(mode="json"))
    ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=manifest_ref.media_type,
                      size_bytes=len(payload), kind=rp.KIND_MANIFEST)
    return [(ref, payload) if r == manifest_ref else (r, d) for r, d in outputs]


@pytest.mark.parametrize("changes", [{"image_digest": "9" * 64}, {"upstream_revision": "9" * 40},
                                     {"profile_id": "agsci.other.profile"}, {"upstream_repository": "someone/fork"}])
def test_a_manifest_naming_another_runtime_than_the_profile_never_verifies(changes: dict[str, Any]) -> None:
    out, store = _direct(ScriptedRuntime())
    outputs = _outputs(out, store)
    assert rp.reference_checker(profile())(outputs) is True
    forged = _reissued(outputs, **changes)
    assert rp.reference_checker(profile())(forged) is False
    with pytest.raises(ResearchError, match="RESEARCH_MANIFEST_UNVERIFIED"):
        rp.research_projection(forged, goal_id="goal-research", request_digest=PINNED, profile=profile())
