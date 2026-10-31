# SPDX-License-Identifier: AGPL-3.0-only
"""ResearchRunManifest: PADOS derives a bounded manifest from an untrusted research run and re-verifies it.

A PASS means "the research runtime evidence is internally consistent", never "the science is true". Covered:
complete run, malformed runtime output, non-zero exit, missing artifact, digest mismatch, unauthorized artifact
paths, quarantine of unknown files, invalid phase and phase order, partial run, resume mismatch, egress violation,
semantic claims not evidenced by the final evaluation, and paper output without the paper phase.
"""
from __future__ import annotations

from typing import Any

import pytest
from _research_fixtures import METRICS, RUN_ID, complete_files, output, profile, request

from pados.packs.research.contracts import ClaimStatus, PhaseState, ResearchError, RunStatus
from pados.packs.research.manifest import build_run_manifest, verify_run_manifest



_profile = profile


def _build(raw: Any, campaign: Any = None, profile: Any = None) -> Any:
    return build_run_manifest(raw, campaign=campaign or request(), profile=profile or _profile(),
                              request_digest="a" * 64)


def _refused(raw: Any, **kwargs: Any) -> str:
    with pytest.raises(ResearchError) as excinfo:
        _build(raw, **kwargs)
    assert excinfo.value.__cause__ is None
    return excinfo.value.code


def test_a_complete_run_yields_a_consistent_unaccepted_manifest() -> None:
    manifest, promoted = _build(output())
    assert manifest.status is RunStatus.COMPLETED and manifest.semantic_authority == "NONE"
    assert manifest.upstream_run_id == RUN_ID and manifest.final_evaluation == METRICS
    assert {entry.relative_path for entry in manifest.artifacts} == set(complete_files())
    assert all(claim.status is ClaimStatus.EVIDENCED for claim in manifest.claims)
    assert verify_run_manifest(manifest, promoted)


@pytest.mark.parametrize("raw", [None, "free text", {"exit_code": 0}, {"unexpected": True}, 42])
def test_malformed_runtime_output_fails_closed(raw: Any) -> None:
    assert _refused(raw) == "RESEARCH_MANIFEST_MALFORMED"


def test_a_non_zero_upstream_exit_promotes_nothing() -> None:
    assert _refused(output(exit_code=3)) == "RESEARCH_RUNTIME_FAILED"


def test_a_completed_phase_whose_artifact_is_missing_is_refused() -> None:
    files = {k: v for k, v in complete_files().items() if not k.startswith("final_evaluation/")}
    assert _refused(output(files=files)) == "RESEARCH_PHASE_OUTPUT_MISSING"


def test_an_artifact_whose_bytes_changed_after_the_manifest_fails_verification() -> None:
    manifest, promoted = _build(output())
    tampered = dict(promoted, **{"final_evaluation/result.json": b'{"metrics": {"packing_density": 0.99}}'})
    assert not verify_run_manifest(manifest, tampered)
    assert not verify_run_manifest(manifest, {k: v for k, v in promoted.items() if not k.startswith("paper/")})


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "C:/Windows/win.ini", "paper\\paper.pdf",
                                  "paper/../../x", "paper//paper.pdf", "paper/./paper.pdf", "~/secrets",
                                  "investigation/" + "d/" * 9 + "deep.json", "paper/pa per.pdf"])
def test_unauthorized_artifact_paths_refuse_the_whole_run(path: str) -> None:
    files = dict(complete_files(), **{path: b"x"})
    assert _refused(output(files=files)) == "RESEARCH_ARTIFACT_PATH_UNAUTHORIZED"


def test_unknown_files_are_quarantined_never_promoted() -> None:
    files = dict(complete_files(), **{"scratch/agent_shell_history.txt": b"rm -rf", ".env": b"KEY=value"})
    manifest, promoted = _build(output(files=files))
    assert manifest.quarantined_count == 2
    assert not {"scratch/agent_shell_history.txt", ".env"} & set(promoted)
    assert all(not entry.relative_path.startswith("scratch/") for entry in manifest.artifacts)


def test_an_unknown_phase_is_refused() -> None:
    phases = dict(output().phases, write_grant_proposal="COMPLETED")
    assert _refused(output(phases=phases)) == "RESEARCH_PHASE_INVALID"


def test_an_unknown_phase_state_is_refused() -> None:
    phases = dict(output().phases, paper="ACCEPTED")
    assert _refused(output(phases=phases)) == "RESEARCH_PHASE_INVALID"


def test_a_phase_completed_after_an_unfinished_one_is_refused() -> None:
    phases = dict(output().phases, ablation="PENDING")
    assert _refused(output(phases=phases)) == "RESEARCH_PHASE_ORDER_INVALID"


def test_a_partial_run_is_recorded_as_partial_never_completed() -> None:
    phases = {"task_draft": "COMPLETED", "investigation": "COMPLETED", "ablation": "PENDING",
              "final_evaluation": "PENDING", "posthoc": "PENDING", "paper": "PENDING"}
    files = {k: v for k, v in complete_files().items() if k.startswith(("task/", "investigation/"))}
    manifest, promoted = _build(output(phases=phases, files=files, claims=()))
    assert manifest.status is RunStatus.PARTIAL and manifest.final_evaluation is None
    assert verify_run_manifest(manifest, promoted)


def test_a_failed_phase_fails_the_run() -> None:
    phases = dict(output().phases, posthoc="FAILED", paper="PENDING")
    files = {k: v for k, v in complete_files().items() if not k.startswith(("posthoc/", "paper/"))}
    manifest, _ = _build(output(phases=phases, files=files))
    assert manifest.status is RunStatus.FAILED
    assert {p.phase.value: p.state for p in manifest.phases}["posthoc"] is PhaseState.FAILED


def test_paper_output_without_a_completed_paper_phase_is_refused() -> None:
    phases = dict(output().phases, paper="SKIPPED")
    assert _refused(output(phases=phases)) == "RESEARCH_PHASE_OUTPUT_UNEXPECTED"


@pytest.mark.parametrize("resume", [{"upstream_run_id": "20261031_999999", "completed_phases": ["task_draft"]},
                                    {"upstream_run_id": RUN_ID, "completed_phases": ["task_draft", "review"]}])
def test_a_resume_that_does_not_continue_the_named_run_is_refused(resume: dict[str, Any]) -> None:
    assert _refused(output(), campaign=request(resume=resume)) == "RESEARCH_RESUME_MISMATCH"


def test_a_resume_of_the_same_run_is_accepted() -> None:
    manifest, _ = _build(output(), campaign=request(resume={"upstream_run_id": RUN_ID,
                                                             "completed_phases": ["task_draft", "investigation"]}))
    assert manifest.status is RunStatus.COMPLETED


def test_egress_to_a_host_outside_the_profile_is_a_violation() -> None:
    assert _refused(output(egress_hosts_contacted=("api.example.org",))) == "RESEARCH_EGRESS_VIOLATION"


@pytest.mark.parametrize("claim", [{"metric": "packing_density", "value": 0.95},
                                   {"metric": "human_level_insight", "value": 1.0}])
def test_a_semantic_claim_not_supported_by_the_final_evaluation_is_marked_not_evidenced(claim: dict[str, Any]
                                                                                        ) -> None:
    manifest, promoted = _build(output(claims=(claim,)))
    assert [c.status for c in manifest.claims] == [ClaimStatus.NOT_EVIDENCED]
    assert manifest.review_required is True
    assert verify_run_manifest(manifest, promoted)  # consistent: the claim is recorded as unsupported


def test_a_manifest_whose_claim_status_was_upgraded_fails_verification() -> None:
    manifest, promoted = _build(output(claims=({"metric": "packing_density", "value": 0.95},)))
    forged_claims = tuple(c.model_copy(update={"status": ClaimStatus.EVIDENCED}) for c in manifest.claims)
    forged = manifest.model_copy(update={"claims": forged_claims, "review_required": False})
    assert not verify_run_manifest(forged, promoted)
