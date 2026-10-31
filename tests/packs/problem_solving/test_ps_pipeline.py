# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Problem Solving pipeline: evidence first, grounded claims only, abstain when evidence is missing.

Deterministic tests over synthetic 8D fixtures: complete evidence, missing evidence, conflicting evidence,
cross-case comparison, unsupported generated claims, critic disagreement, malformed provider output.
"""
from __future__ import annotations

from typing import Any

import pytest
from _ps_fixtures import (
    CASE_A,
    claim,
    complete_case,
    conflicting_evidence_case,
    cross_case,
    missing_evidence_case,
    raw_claims,
    unsupported_claim_case,
)

from pados.packs.problem_solving.contracts import (
    EIGHT_D,
    ClaimSet,
    GroundingStatus,
    ProblemSolvingError,
    ReportStatus,
    SectionStatus,
)
from pados.packs.problem_solving.pipeline import (
    CriticVerdict,
    build_report,
    ground_claims,
    parse_claims,
    verify_report,
)


def _sections(report: Any) -> dict[str, SectionStatus]:
    return {section.section_id: section.status for section in report.sections}


def _verdicts(report: Any) -> dict[str, GroundingStatus]:
    return {verdict.claim_id: verdict.status for verdict in report.verdicts}


def test_complete_evidence_yields_a_complete_but_unaccepted_report() -> None:
    pack, claims = complete_case()
    report = build_report(pack, claims, EIGHT_D)
    assert set(_sections(report).values()) == {SectionStatus.SUPPORTED}
    assert report.status is ReportStatus.COMPLETE
    assert report.semantic_authority == "NONE" and "accepted" not in report.model_dump()
    assert report.evidence_pack_digest == pack.digest
    assert verify_report(pack, claims, report)  # deterministic re-derivation


def test_missing_evidence_abstains_instead_of_filling_sections() -> None:
    pack, claims = missing_evidence_case()
    report = build_report(pack, claims, EIGHT_D)
    sections = _sections(report)
    assert [sid for sid, status in sections.items() if status is SectionStatus.SUPPORTED] == ["D1", "D2", "D3"]
    assert all(sections[sid] is SectionStatus.ABSTAINED for sid in ("D4", "D5", "D6", "D7", "D8"))
    assert all(not section.claim_ids for section in report.sections if section.status is SectionStatus.ABSTAINED)
    assert report.status is ReportStatus.INCOMPLETE


def test_conflicting_evidence_is_surfaced_not_resolved() -> None:
    pack, claims = conflicting_evidence_case()
    report = build_report(pack, claims, EIGHT_D)
    assert _verdicts(report)["cl-d4"] is GroundingStatus.CONFLICTED
    assert _sections(report)["D4"] is SectionStatus.CONFLICTED
    assert report.status is ReportStatus.REVIEW_REQUIRED


def test_cross_case_claims_must_preserve_case_identity() -> None:
    pack, claims = cross_case()
    verdicts = {v.claim_id: v.status for v in ground_claims(pack, claims)}
    assert verdicts == {"cl-shared": GroundingStatus.GROUNDED, "cl-half": GroundingStatus.CASE_MISMATCH,
                        "cl-wrong-case": GroundingStatus.CASE_MISMATCH}


def test_unsupported_generated_claims_are_rejected_and_never_reported() -> None:
    pack, claims = unsupported_claim_case()
    report = build_report(pack, claims, EIGHT_D)
    verdicts = _verdicts(report)
    assert verdicts["cl-invented"] is GroundingStatus.UNSUPPORTED
    assert verdicts["cl-uncited"] is GroundingStatus.UNSUPPORTED
    assert verdicts["cl-contradicted"] is GroundingStatus.CONTRADICTED
    reported = {cid for section in report.sections for cid in section.claim_ids}
    assert not reported & {"cl-invented", "cl-uncited", "cl-contradicted"}
    assert report.status is ReportStatus.REVIEW_REQUIRED  # rejected claims are visible to the reviewer


def test_generation_cannot_rewrite_source_evidence() -> None:
    pack, claims = complete_case()
    before = pack.digest
    build_report(pack, claims, EIGHT_D)
    assert pack.digest == before
    tampered = pack.model_copy(update={"items": pack.items[:-1]})
    report = build_report(pack, claims, EIGHT_D)
    assert not verify_report(tampered, claims, report)  # a report is bound to the exact evidence pack


@pytest.mark.parametrize("critic", [CriticVerdict(agrees=False, reason_code="CRITIC_SECTION_TOO_BROAD"),
                                    CriticVerdict(agrees=True, reason_code="CRITIC_OK")])
def test_critic_is_advisory_and_cannot_become_semantic_authority(critic: CriticVerdict) -> None:
    pack, claims = complete_case()
    plain = build_report(pack, claims, EIGHT_D)
    critiqued = build_report(pack, claims, EIGHT_D, critic=critic)
    assert critiqued.sections == plain.sections and critiqued.verdicts == plain.verdicts  # never rewrites grounding
    assert critiqued.critic_agrees is critic.agrees and critiqued.critic_code == critic.reason_code
    expected = ReportStatus.COMPLETE if critic.agrees else ReportStatus.REVIEW_REQUIRED
    assert critiqued.status is expected  # disagreement only routes to human review


@pytest.mark.parametrize("raw", [
    {"claims": "not a list"},
    {"schema_version": "x", "claims": []},
    {"claims": [{"claim_id": "cl-1"}]},
    {"claims": [{"claim_id": "cl-1", "case_ids": [CASE_A], "section": "D4", "subject": "root_cause",
                 "assertion": "worn_bearing", "text": "ok", "citations": ["ev-d4"], "authority": "GRANTED"}]},
    ["not", "a", "mapping"],
    None,
])
def test_malformed_provider_output_fails_closed(raw: Any) -> None:
    with pytest.raises(ProblemSolvingError) as excinfo:
        parse_claims(raw)
    assert excinfo.value.code == "PS_PROVIDER_OUTPUT_MALFORMED"


def test_parse_claims_round_trips_well_formed_output() -> None:
    _, claims = complete_case()
    assert parse_claims(raw_claims(claims)) == claims


def test_claims_for_a_section_outside_the_profile_are_rejected() -> None:
    pack, _ = complete_case()
    stray = ClaimSet(claims=(claim("cl-stray", "D9", "team", "cross_functional_team_formed", ("ev-d1",)),))
    with pytest.raises(ProblemSolvingError, match="PS_CLAIM_SECTION_UNKNOWN"):
        build_report(pack, stray, EIGHT_D)
