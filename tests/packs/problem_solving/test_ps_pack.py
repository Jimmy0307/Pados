# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Problem Solving as a Capability Pack V1: registered and executed only through the existing fabric.

Synthetic fixtures only. Covers: end-to-end execution through ``CapabilityExecutor`` with SCHEMA + REFERENCE_CHECK
verification (re-derivation from the pack's own outputs), provider unavailable (retryable closed code), malformed
provider output, a crashing provider (no content escapes), critic disagreement (review required, nothing else
changes), malformed critic output, and a tampered report refused by the reference check.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from _ps_fixtures import AUTHORIZED, complete_case, missing_evidence_case, raw_claims

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capability"))
from fabric_support import OPERATOR, fabric, template  # noqa: E402

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest  # noqa: E402
from pados.capability.artifacts import ArtifactRef, ScopedArtifacts  # noqa: E402
from pados.capability.executor import ExecutionStatus  # noqa: E402
from pados.capability.pack import GovernedPackAdapter, register_pack, validate_pack  # noqa: E402
from pados.capability.receipts import VerificationStatus  # noqa: E402
from pados.capability.task import TaskInput  # noqa: E402
from pados.capability.verification import ValidatorRegistry, VerificationEngine  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.packs.problem_solving import pack as ps  # noqa: E402
from pados.packs.problem_solving.contracts import ClaimSet, EvidencePack  # noqa: E402


class ScriptedProvider:
    provider_id = "synthetic.claims"

    def __init__(self, output: Any = None, *, unavailable: bool = False, crash: bool = False) -> None:
        self.output, self.unavailable, self.crash = output, unavailable, crash

    def draft_claims(self, pack: EvidencePack, profile: Any) -> object:
        if self.unavailable:
            raise ps.ClaimProviderUnavailable("connection refused")
        if self.crash:
            raise RuntimeError("SYNTHETIC SECRET CUSTOMER TEXT")
        return self.output


class ScriptedCritic:
    def __init__(self, output: object) -> None:
        self.output = output

    def review(self, pack: EvidencePack, claims: ClaimSet, report: Any) -> object:
        return self.output


class _Store:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        ref = ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=media_type, size_bytes=len(payload),
                          kind=kind)
        self.blobs[ref.sha256] = payload
        return ref

    def get(self, ref: ArtifactRef) -> bytes:
        return self.blobs[ref.sha256]


def parameters(pack: EvidencePack, **overrides: Any) -> dict[str, Any]:
    """Task-owned stage parameters: the profile, the pinned evidence digest and the authorized sources."""
    return {"profile": "8d", "evidence_pack_digest": pack.digest, "authorized_sources": ",".join(AUTHORIZED),
            **overrides}


def _call(provider: ScriptedProvider, pack: EvidencePack, *, critic: ScriptedCritic | None = None,
          params: dict[str, Any] | None = None) -> tuple[StageOutput, _Store]:
    store = _Store()
    source = store.put(ps.canonical_json(pack.model_dump(mode="json")), media_type=ps.JSON_MEDIA_TYPE,
                       kind=ps.KIND_EVIDENCE)
    context = StageContext(artifacts=ScopedArtifacts(store, readable=(source,)), project_id="proj-a", stage_id="ps")
    request = StageRequest(stage_id="ps", capability_id=ps.CAPABILITY_ID, inputs=(source,),
                           parameters=parameters(pack) if params is None else params,
                           classification=DataClassification.CONFIDENTIAL)
    adapter = GovernedPackAdapter(ps.MANIFEST, ps.ProblemSolvingAdapter(provider, critic=critic))
    return adapter.execute(request, context=context), store


def _artifacts(output: StageOutput, store: _Store) -> list[tuple[ArtifactRef, bytes]]:
    return [(ref, store.get(ref)) for ref in output.outputs]


def test_manifest_is_a_valid_pack_v1() -> None:
    assert validate_pack(ps.MANIFEST.model_dump(mode="json")) == ps.MANIFEST
    assert ps.MANIFEST.required_grants == ("CAPABILITY:STRUCTURED_PROBLEM_SOLVING",)


def test_end_to_end_through_the_existing_executor_with_rederivation(tmp_path: Path) -> None:
    pack, claims = complete_case()
    fab = fabric(tmp_path)
    register_pack(fab.registry, ps.MANIFEST, {ps.ADAPTER_ID: ps.ProblemSolvingAdapter(
        ScriptedProvider(raw_claims(claims)))}, operator=OPERATOR)
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
    result = fab.executor(verifier=VerificationEngine(schemas=fab.schemas, references=references)).execute(
        fab.planner().plan(task), task)
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    (stage,) = result.plan_receipt.stages
    assert stage.verification is VerificationStatus.PASSED
    assert result.semantic_authority == "NONE"  # a verified candidate, never an accepted one
    report = json.loads(fab.store.get(next(r for r in stage.outputs if r.kind == ps.KIND_REPORT)))
    assert report["status"] == "COMPLETE" and report["semantic_authority"] == "NONE"


def test_missing_evidence_abstains_through_the_adapter() -> None:
    pack, claims = missing_evidence_case()
    output, store = _call(ScriptedProvider(raw_claims(claims)), pack)
    assert output.evidence["report_status"] == "INCOMPLETE"
    assert output.evidence["sections_abstained"] == 5
    assert ps.report_reference_check(_artifacts(output, store))


def test_provider_unavailable_is_a_retryable_closed_failure() -> None:
    pack, _ = complete_case()
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(ScriptedProvider(unavailable=True), pack)
    assert excinfo.value.code == "PS_PROVIDER_UNAVAILABLE" and excinfo.value.retryable is True


@pytest.mark.parametrize("output", [None, "free text", {"claims": [{"claim_id": "x"}]}, {"claims": [], "extra": 1}])
def test_malformed_provider_output_fails_closed(output: Any) -> None:
    pack, _ = complete_case()
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(ScriptedProvider(output), pack)
    assert excinfo.value.code == "PS_PROVIDER_OUTPUT_MALFORMED" and excinfo.value.retryable is False


def test_crashing_provider_leaks_no_content() -> None:
    pack, _ = complete_case()
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(ScriptedProvider(crash=True), pack)
    assert excinfo.value.code == "PS_PROVIDER_FAILED"
    assert "SECRET" not in repr(excinfo.value) and excinfo.value.__cause__ is None


def test_critic_disagreement_requires_review_and_changes_nothing_else() -> None:
    pack, claims = complete_case()
    agreed, _ = _call(ScriptedProvider(raw_claims(claims)), pack)
    disagreed, store = _call(ScriptedProvider(raw_claims(claims)), pack,
                             critic=ScriptedCritic({"agrees": False, "reason_code": "CRITIC_ROOT_CAUSE_SHALLOW"}))
    assert agreed.evidence["report_status"] == "COMPLETE"
    assert disagreed.evidence["report_status"] == "REVIEW_REQUIRED"
    assert disagreed.evidence["claims_grounded"] == agreed.evidence["claims_grounded"]
    assert ps.report_reference_check(_artifacts(disagreed, store))  # the critic's verdict is part of the record


@pytest.mark.parametrize("output", [None, {"agrees": "yes", "reason_code": "OK_FINE"},
                                    {"agrees": True, "reason_code": "lower case"}, {"agrees": True}])
def test_malformed_critic_output_fails_closed(output: object) -> None:
    pack, claims = complete_case()
    with pytest.raises(CapabilityAdapterError) as excinfo:
        _call(ScriptedProvider(raw_claims(claims)), pack, critic=ScriptedCritic(output))
    assert excinfo.value.code == "PS_CRITIC_OUTPUT_MALFORMED"


def test_reference_check_refuses_a_tampered_report() -> None:
    pack, claims = missing_evidence_case()
    output, store = _call(ScriptedProvider(raw_claims(claims)), pack)
    artifacts = _artifacts(output, store)
    forged: list[tuple[ArtifactRef, bytes]] = []
    for ref, data in artifacts:
        if ref.kind == ps.KIND_REPORT:
            report = json.loads(data)
            report["status"] = "COMPLETE"  # claim completeness the evidence does not support
            data = ps.canonical_json(report)
        forged.append((ref, data))
    assert ps.report_output_valid(forged)  # well-formed ...
    assert not ps.report_reference_check(forged)  # ... but not what the evidence derives
