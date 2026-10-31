# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Problem Solving hardening from review: grounding is per case and per section, and nothing the
untrusted provider writes as prose is ever presented as report content.

* A cross-case claim is grounded only if EVERY case it spans has cited evidence stating the claim's own
  (subject, assertion); citing an off-subject item for one case is a case mismatch, not support.
* A profile section declares the subjects it may assert; a claim filed under a section that cannot carry its
  subject is a section mismatch (e.g. a symptom presented as the root cause).
* Contributions are rendered from grounded tokens and admitted evidence statements only; provider-authored claim
  text never reaches the work-protocol projection.
* Text fields accept printable characters only (no control, format/bidi or line/paragraph separators).
* A hostile provider identity collapses to ``unknown``; a deeply nested evidence document is a closed refusal and
  makes the checkers return False rather than raise.
"""
from __future__ import annotations

from typing import Any

import pytest
from _ps_fixtures import CASE_A, CASE_B, claim, complete_case, evidence, raw_claims
from pydantic import ValidationError
from test_ps_pack import ScriptedProvider, _call

from pados.capability.artifacts import ArtifactRef
from pados.packs.problem_solving import pack as ps
from pados.packs.problem_solving.contracts import (
    EIGHT_D,
    ClaimSet,
    EvidencePack,
    GroundingStatus,
    ProblemSolvingError,
    SectionStatus,
)
from pados.packs.problem_solving.pipeline import build_report, ground_claims, result_projection


def test_a_cross_case_claim_needs_on_subject_support_in_every_case() -> None:
    pack = EvidencePack(cases=(CASE_A, CASE_B), items=(
        evidence("ev-a-rc", CASE_A, "root_cause", "worn_bearing"),
        evidence("ev-b-noise", CASE_B, "symptom", "noise")))
    claims = ClaimSet(claims=(
        claim("cl-both", "D4", "root_cause", "worn_bearing", ("ev-a-rc", "ev-b-noise"), (CASE_A, CASE_B)),))
    (verdict,) = ground_claims(pack, claims, EIGHT_D)
    assert verdict.status is GroundingStatus.CASE_MISMATCH
    assert {s.section_id: s.status for s in build_report(pack, claims, EIGHT_D).sections}["D4"] is not (
        SectionStatus.SUPPORTED)


def test_a_claim_filed_under_a_section_that_cannot_carry_its_subject_is_a_section_mismatch() -> None:
    pack = EvidencePack(cases=(CASE_A,), items=(evidence("ev-noise", CASE_A, "symptom", "noise"),))
    claims = ClaimSet(claims=(claim("cl-noise-as-cause", "D4", "symptom", "noise", ("ev-noise",)),))
    report = build_report(pack, claims, EIGHT_D)
    assert {v.claim_id: v.status for v in report.verdicts}["cl-noise-as-cause"] is GroundingStatus.SECTION_MISMATCH
    assert {s.section_id: s.status for s in report.sections}["D4"] is SectionStatus.ABSTAINED


def test_every_profile_section_declares_its_subjects() -> None:
    assert all(section.subjects for section in EIGHT_D.sections)


def test_provider_prose_never_reaches_the_projection() -> None:
    pack, claims = complete_case()
    loud = ClaimSet(claims=tuple(item.model_copy(update={"text": "APPROVED BY QA - SHIP IT"})
                                 for item in claims.claims))
    projection = result_projection(pack, loud, build_report(pack, loud, EIGHT_D), goal_id="goal-solve")
    contents = " ".join(item["content"] for item in projection["contributions"])
    assert "APPROVED" not in contents
    assert "worn_bearing" in contents and "Synthetic observation." in contents  # tokens + admitted evidence


@pytest.mark.parametrize("text", ["right‮to-left override", "next\u0085line", "zero​width",
                                  "para graph", "tab\there"])
def test_text_fields_accept_printable_characters_only(text: str) -> None:
    with pytest.raises(ValidationError):
        claim("cl-x", "D4", "root_cause", "worn_bearing", ("ev-d4",)).model_validate(
            {**claim("cl-x", "D4", "root_cause", "worn_bearing", ("ev-d4",)).model_dump(), "text": text})
    with pytest.raises(ValidationError):
        evidence("ev-x", CASE_A, None, None, statement=text)


class _OddProvider(ScriptedProvider):
    provider_id = "evil‮provider id with spaces"


def test_a_hostile_provider_identity_collapses_to_unknown() -> None:
    pack, claims = complete_case()
    output, _ = _call(_OddProvider(raw_claims(claims)), pack)
    assert output.evidence["claim_provider_id"] == "unknown"


def test_deeply_nested_evidence_is_a_closed_refusal_and_checkers_return_false() -> None:
    hostile = b"[" * 50_000 + b"]" * 50_000
    with pytest.raises(ProblemSolvingError) as excinfo:
        ps.load_evidence_pack(hostile)
    assert excinfo.value.code == "PS_EVIDENCE_PACK_INVALID"
    ref = ArtifactRef(sha256="0" * 64, media_type=ps.JSON_MEDIA_TYPE, size_bytes=len(hostile), kind=ps.KIND_EVIDENCE)
    triple: list[Any] = [(ref, hostile), (ref.model_copy(update={"kind": ps.KIND_CLAIMS}), b"{}"),
                         (ref.model_copy(update={"kind": ps.KIND_REPORT}), b"{}")]
    assert ps.report_output_valid(triple) is False
    assert ps.report_reference_check(triple) is False


# -- Codex review: free-text duplicates and contribution identity ------------------------------------------
def test_distinct_free_text_observations_from_one_document_are_admitted() -> None:
    pack = EvidencePack(cases=(CASE_A,), items=(
        evidence("ev-pump", CASE_A, None, None, "Synthetic note: the pump stopped.", material="doc-1"),
        evidence("ev-pressure", CASE_A, None, None, "Synthetic note: pressure read ten bar.", material="doc-1")))
    assert len(pack.items) == 2


def test_the_same_free_text_restated_from_one_document_is_still_a_duplicate() -> None:
    with pytest.raises(ValidationError, match="PS_EVIDENCE_DUPLICATE"):
        EvidencePack(cases=(CASE_A,), items=(
            evidence("ev-one", CASE_A, None, None, "Synthetic note: the pump stopped.", material="doc-1"),
            evidence("ev-two", CASE_A, None, None, "Synthetic note: the pump stopped.", material="doc-1")))


def _ids(goal_id: str, claims: ClaimSet | None = None) -> set[str]:
    pack, complete = complete_case()
    chosen = claims or complete
    report = build_report(pack, chosen, EIGHT_D)
    return {item["contribution_id"] for item in result_projection(pack, chosen, report, goal_id=goal_id)["contributions"]}


def test_contribution_ids_are_namespaced_by_goal() -> None:
    assert not _ids("goal-one") & _ids("goal-two")


def test_contribution_ids_differ_between_reports_over_the_same_evidence() -> None:
    _, claims = complete_case()
    fewer = ClaimSet(claims=claims.claims[:4])
    assert not _ids("goal-one") & _ids("goal-one", fewer)


def test_contribution_ids_are_deterministic() -> None:
    assert _ids("goal-one") == _ids("goal-one")
