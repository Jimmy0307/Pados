# SPDX-License-Identifier: AGPL-3.0-only
"""Structured Problem Solving: evidence admission, abstention, profile genericity and the result projection.

* Evidence is admitted only when it is exactly the pack the task pinned (stale digest refused) and comes only from
  sources the task authorized (unauthorized source refused) -- the pack decides neither.
* The same material restated twice is refused as duplicate evidence, never counted as corroboration.
* With insufficient evidence the report ABSTAINS (validation required), never completes.
* 8D is one profile among others: the same pipeline runs an A3 profile unchanged.
* Every claim a report presents as supported maps to cited, same-case evidence of the pinned pack.
* ``result_projection`` turns a report into the bounded provider-response fields the existing
  ``work_protocol`` normalization accepts: only supported sections become contributions, everything else stays
  an unresolved goal -- the projection carries no task, authorization, receipt or acceptance field.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from _ps_fixtures import (
    AUTHORIZED,
    CASE_A,
    UNVETTED_SOURCE,
    a3_case,
    complete_case,
    conflicting_evidence_case,
    cross_case,
    duplicate_evidence_items,
    insufficient_evidence_case,
    missing_evidence_case,
    unauthorized_source_case,
    unsupported_claim_case,
)
from pydantic import ValidationError

from pados.packs.problem_solving.contracts import (
    A3,
    EIGHT_D,
    ClaimSet,
    EvidencePack,
    GroundingStatus,
    ProblemSolvingError,
    ProblemSolvingProfile,
    ReportStatus,
    SectionStatus,
)
from pados.packs.problem_solving.pipeline import admit_evidence, build_report, result_projection

FIXTURES: dict[str, Callable[[], tuple[EvidencePack, ClaimSet]]] = {
    "complete": complete_case, "missing": missing_evidence_case, "conflicting": conflicting_evidence_case,
    "cross_case": cross_case, "unsupported": unsupported_claim_case, "insufficient": insufficient_evidence_case,
}


# -- admission ------------------------------------------------------------------------------------------------
def test_the_pinned_pack_from_authorized_sources_is_admitted() -> None:
    pack, _ = complete_case()
    admit_evidence(pack, pinned_digest=pack.digest, authorized_sources=AUTHORIZED)


def test_a_stale_evidence_digest_is_refused() -> None:
    pack, _ = complete_case()
    newer, _ = conflicting_evidence_case()  # the evidence changed after the task pinned it
    with pytest.raises(ProblemSolvingError) as excinfo:
        admit_evidence(newer, pinned_digest=pack.digest, authorized_sources=AUTHORIZED)
    assert excinfo.value.code == "PS_EVIDENCE_DIGEST_STALE"


def test_evidence_from_an_unauthorized_source_is_refused_not_filtered() -> None:
    pack, _ = unauthorized_source_case()
    with pytest.raises(ProblemSolvingError) as excinfo:
        admit_evidence(pack, pinned_digest=pack.digest, authorized_sources=AUTHORIZED)
    assert excinfo.value.code == "PS_EVIDENCE_SOURCE_UNAUTHORIZED"
    assert UNVETTED_SOURCE not in str(excinfo.value)  # closed code only


def test_no_authorized_sources_admits_nothing() -> None:
    pack, _ = complete_case()
    with pytest.raises(ProblemSolvingError, match="PS_EVIDENCE_SOURCE_UNAUTHORIZED"):
        admit_evidence(pack, pinned_digest=pack.digest, authorized_sources=())


def test_duplicate_evidence_is_refused_as_one_fact_restated() -> None:
    with pytest.raises(ValidationError, match="PS_EVIDENCE_DUPLICATE"):
        EvidencePack(cases=(CASE_A,), items=duplicate_evidence_items())


# -- abstention -----------------------------------------------------------------------------------------------
def test_insufficient_evidence_abstains_with_nothing_completed() -> None:
    pack, claims = insufficient_evidence_case()
    report = build_report(pack, claims, EIGHT_D)
    assert report.status is ReportStatus.ABSTAINED
    assert {section.status for section in report.sections} == {SectionStatus.ABSTAINED}
    assert all(section.claim_ids == () for section in report.sections)


# -- genericity -----------------------------------------------------------------------------------------------
def test_the_same_pipeline_runs_an_a3_profile_unchanged() -> None:
    pack, claims = a3_case()
    report = build_report(pack, claims, A3)
    assert report.profile_id == "a3" and report.status is ReportStatus.COMPLETE
    assert {s.section_id: s.status for s in report.sections}["FOLLOW_UP"] is SectionStatus.ABSTAINED  # optional


def test_a3_claims_do_not_fit_the_8d_profile() -> None:
    pack, claims = a3_case()
    with pytest.raises(ProblemSolvingError, match="PS_CLAIM_SECTION_UNKNOWN"):
        build_report(pack, claims, EIGHT_D)


# -- the core invariant: every presented claim maps to evidence -----------------------------------------------
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_supported_claim_maps_to_cited_same_case_evidence(name: str) -> None:
    pack, claims = FIXTURES[name]()
    report = build_report(pack, claims, EIGHT_D)
    status = {verdict.claim_id: verdict.status for verdict in report.verdicts}
    by_id = {item.claim_id: item for item in claims.claims}
    for section in report.sections:
        if section.status is not SectionStatus.SUPPORTED:
            continue
        for claim_id in section.claim_ids:
            claim = by_id[claim_id]
            assert status[claim_id] is GroundingStatus.GROUNDED
            cited = [pack.item(evidence_id) for evidence_id in claim.citations]
            assert cited and all(item is not None for item in cited)
            assert {item.case_id for item in cited if item is not None} == set(claim.case_ids)
            assert any(item is not None and (item.subject, item.assertion) == (claim.subject, claim.assertion)
                       for item in cited)


# -- projection into the existing work-protocol fields --------------------------------------------------------
def _projection(fixture: Callable[[], tuple[EvidencePack, ClaimSet]],
                profile: ProblemSolvingProfile = EIGHT_D) -> tuple[Any, dict[str, Any]]:
    pack, claims = fixture()
    report = build_report(pack, claims, profile)
    return report, result_projection(pack, claims, report, goal_id="goal-solve")


def test_a_complete_report_projects_one_contribution_per_supported_section() -> None:
    report, projection = _projection(complete_case)
    assert projection["completed_goals"] == ("goal-solve",)
    assert projection["unresolved_goals"] == ()
    assert len(projection["contributions"]) == 8
    assert all(item["goal_id"] == "goal-solve" for item in projection["contributions"])
    assert {item["evidence_id"] for item in projection["evidence"]} == {f"ev-d{n}" for n in range(1, 9)}
    assert set(projection) <= {"completed_goals", "partial_goals", "unresolved_goals", "contributions",
                               "evidence", "metrics", "failure_reason"}


@pytest.mark.parametrize(("fixture", "outcome"), [
    (missing_evidence_case, "partial_goals"), (insufficient_evidence_case, "unresolved_goals"),
    (conflicting_evidence_case, "unresolved_goals"), (unsupported_claim_case, "unresolved_goals"),
])
def test_anything_short_of_complete_never_projects_a_completed_goal(fixture: Any, outcome: str) -> None:
    report, projection = _projection(fixture)
    assert projection["completed_goals"] == ()
    assert projection[outcome] == ("goal-solve",)
    presented = {cid for section in report.sections if section.status is SectionStatus.SUPPORTED
                 for cid in section.claim_ids}
    assert len(projection["contributions"]) == len({s.section_id for s in report.sections
                                                     if s.status is SectionStatus.SUPPORTED})
    rejected = {v.claim_id for v in report.verdicts if v.status is not GroundingStatus.GROUNDED}
    assert not presented & rejected
    if outcome == "unresolved_goals":
        assert projection["failure_reason"] in ("MISSING_DATA", "QUALITY_INSUFFICIENT")
