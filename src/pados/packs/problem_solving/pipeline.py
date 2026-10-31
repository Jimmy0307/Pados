# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic Structured Problem Solving pipeline: parse claims -> ground -> assemble -> critique -> verify.

Evidence first: a claim counts only if it is GROUNDED in the evidence pack, i.e. it cites evidence that exists,
belongs to the case(s) the claim names (a cross-case claim must cite every case it spans), states the claim's own
(subject, assertion), and no evidence of those cases disagrees on that subject. Everything else is rejected with a
closed status and never appears as report content. A profile section without grounded support ABSTAINS -- it is
never filled. A critic's verdict is advisory: disagreement routes the report to human review and changes nothing
else. ``verify_report`` re-derives a report from its evidence pack and claim set, so the derivation is checkable
by anyone holding the three artifacts. The pipeline never mutates evidence and grants no acceptance.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from pados.execution.context import canonical_hash
from pados.packs.problem_solving.contracts import (
    PROFILES,
    Claim,
    ClaimSet,
    ClaimVerdict,
    EvidencePack,
    GroundingStatus,
    ProblemSolvingError,
    ProblemSolvingProfile,
    ProblemSolvingReport,
    ReportSection,
    ReportStatus,
    SectionStatus,
)

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


@dataclass(frozen=True)
class CriticVerdict:
    """An untrusted critic's opinion of a report: advisory only (never a grounding or acceptance decision)."""

    agrees: bool
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.agrees, bool) or not _CODE.fullmatch(self.reason_code):
            raise ProblemSolvingError("PS_CRITIC_OUTPUT_MALFORMED")


def parse_claims(raw: object) -> ClaimSet:
    """Strictly parse an (untrusted) provider's claim output; anything off-contract is malformed (fail closed)."""
    if not isinstance(raw, Mapping):
        raise ProblemSolvingError("PS_PROVIDER_OUTPUT_MALFORMED")
    try:
        return ClaimSet.model_validate(dict(raw))
    except (ValidationError, TypeError, ValueError):
        pass
    raise ProblemSolvingError("PS_PROVIDER_OUTPUT_MALFORMED")  # outside the handler: no provider text chained


def admit_evidence(pack: EvidencePack, *, pinned_digest: str, authorized_sources: Collection[str]) -> None:
    """Admit an evidence pack only as the task pinned it and only from sources the task authorized.

    Both decisions belong to the task (PADOS), never to the pack or a provider. A changed pack is stale; one item
    from an unauthorized source refuses the whole pack (it is never silently filtered, which would change what the
    task approved).
    """
    if pack.digest != pinned_digest:
        raise ProblemSolvingError("PS_EVIDENCE_DIGEST_STALE")
    if not pack.source_ids <= frozenset(authorized_sources):
        raise ProblemSolvingError("PS_EVIDENCE_SOURCE_UNAUTHORIZED")


def _ground(claim: Claim, pack: EvidencePack) -> GroundingStatus:
    cited = [pack.item(evidence_id) for evidence_id in claim.citations]
    if not claim.citations or any(item is None for item in cited):
        return GroundingStatus.UNSUPPORTED  # nothing cited, or a citation that does not exist
    items = [item for item in cited if item is not None]
    cited_cases = {item.case_id for item in items}
    if not cited_cases <= set(claim.case_ids) or set(claim.case_ids) - cited_cases:
        return GroundingStatus.CASE_MISMATCH  # foreign-case evidence, or a spanned case left uncited
    on_subject = [item for item in items if item.subject == claim.subject]
    if any(item.assertion != claim.assertion for item in on_subject):
        return GroundingStatus.CONTRADICTED  # its own citation says otherwise
    if not on_subject:
        return GroundingStatus.UNSUPPORTED  # the citations do not state this claim
    disagreeing = [item for item in pack.items if item.case_id in claim.case_ids
                   and item.subject == claim.subject and item.assertion != claim.assertion]
    return GroundingStatus.CONFLICTED if disagreeing else GroundingStatus.GROUNDED


def ground_claims(pack: EvidencePack, claims: ClaimSet) -> tuple[ClaimVerdict, ...]:
    """Deterministic grounding verdict for every claim, in claim order."""
    return tuple(ClaimVerdict(claim_id=claim.claim_id, status=_ground(claim, pack)) for claim in claims.claims)


def build_report(pack: EvidencePack, claims: ClaimSet, profile: ProblemSolvingProfile, *,
                 critic: CriticVerdict | None = None) -> ProblemSolvingReport:
    """Assemble the report candidate from grounded claims only; abstain where evidence is insufficient."""
    known = set(profile.section_ids())
    if any(claim.section not in known for claim in claims.claims):
        raise ProblemSolvingError("PS_CLAIM_SECTION_UNKNOWN")
    verdicts = ground_claims(pack, claims)
    status_of = {verdict.claim_id: verdict.status for verdict in verdicts}
    sections: list[ReportSection] = []
    for section in profile.sections:
        in_section = [claim.claim_id for claim in claims.claims if claim.section == section.section_id]
        conflicted = tuple(cid for cid in in_section if status_of[cid] is GroundingStatus.CONFLICTED)
        grounded = tuple(cid for cid in in_section if status_of[cid] is GroundingStatus.GROUNDED)
        if conflicted:
            sections.append(ReportSection(section_id=section.section_id, status=SectionStatus.CONFLICTED,
                                          claim_ids=conflicted))
        elif grounded:
            sections.append(ReportSection(section_id=section.section_id, status=SectionStatus.SUPPORTED,
                                          claim_ids=grounded))
        else:
            sections.append(ReportSection(section_id=section.section_id, status=SectionStatus.ABSTAINED,
                                          claim_ids=()))
    required = {section.section_id for section in profile.sections if section.required}
    rejected = any(verdict.status is not GroundingStatus.GROUNDED for verdict in verdicts)
    if rejected or (critic is not None and not critic.agrees):
        status = ReportStatus.REVIEW_REQUIRED
    elif all(item.status is SectionStatus.ABSTAINED for item in sections):
        status = ReportStatus.ABSTAINED
    elif any(item.status is SectionStatus.ABSTAINED for item in sections if item.section_id in required):
        status = ReportStatus.INCOMPLETE
    else:
        status = ReportStatus.COMPLETE
    return ProblemSolvingReport(
        profile_id=profile.profile_id, profile_version=profile.version, evidence_pack_digest=pack.digest,
        claim_set_digest=canonical_hash(claims.model_dump(mode="json")), sections=tuple(sections),
        verdicts=verdicts, critic_agrees=None if critic is None else critic.agrees,
        critic_code=None if critic is None else critic.reason_code, status=status)


def verify_report(pack: EvidencePack, claims: ClaimSet, report: ProblemSolvingReport,
                  profile: ProblemSolvingProfile | None = None) -> bool:
    """Deterministic re-derivation: the report is exactly what this evidence pack and claim set produce."""
    chosen = profile or PROFILES.get(report.profile_id)
    if chosen is None or chosen.version != report.profile_version:
        return False
    critic = None if report.critic_agrees is None or report.critic_code is None else CriticVerdict(
        agrees=report.critic_agrees, reason_code=report.critic_code)
    try:
        return build_report(pack, claims, chosen, critic=critic) == report
    except ProblemSolvingError:
        return False


MAX_CONTRIBUTION_CHARS = 16_000  # the work-protocol contribution bound


def result_projection(pack: EvidencePack, claims: ClaimSet, report: ProblemSolvingReport, *,
                      goal_id: str) -> dict[str, Any]:
    """Project a report onto the bounded provider-response fields ``work_protocol`` normalization accepts.

    Only SUPPORTED sections become contributions, built from their grounded claims; every cited evidence item is
    referenced by id, digest and source. Anything short of COMPLETE never yields a completed goal: INCOMPLETE is a
    partial goal, ABSTAINED and REVIEW_REQUIRED are unresolved. The projection carries no task, authorization,
    receipt, provenance or acceptance field -- PADOS supplies those when it normalizes, composes and verifies.
    """
    by_id = {claim.claim_id: claim for claim in claims.claims}
    profile = PROFILES.get(report.profile_id)
    titles = {} if profile is None else {s.section_id: s.title for s in profile.sections}
    tag = report.evidence_pack_digest[:16]
    contributions: list[dict[str, Any]] = []
    cited: dict[str, dict[str, str]] = {}
    for section in report.sections:
        if section.status is not SectionStatus.SUPPORTED:
            continue
        presented = [by_id[claim_id] for claim_id in section.claim_ids]
        content = f"{section.section_id} {titles.get(section.section_id, '')}: " + " ".join(
            f"{claim.text} [{', '.join(claim.citations)}]" for claim in presented)
        if len(content) > MAX_CONTRIBUTION_CHARS:
            raise ProblemSolvingError("PS_PROJECTION_TOO_LARGE")
        contributions.append({"contribution_id": f"ps.{report.profile_id}.{section.section_id.lower()}.{tag}",
                              "goal_id": goal_id, "content": content, "conflict_key": None})
        for claim in presented:
            for evidence_id in claim.citations:
                item = pack.item(evidence_id)
                if item is None:  # unreachable for a GROUNDED claim; fail closed rather than reference nothing
                    raise ProblemSolvingError("PS_PROJECTION_EVIDENCE_MISSING")
                cited[evidence_id] = {"evidence_id": evidence_id, "digest": item.content_digest,
                                      "source_ref": item.source_id}
    complete = report.status is ReportStatus.COMPLETE
    partial = report.status is ReportStatus.INCOMPLETE
    unresolved = not complete and not partial
    failure = None
    if unresolved:
        failure = "MISSING_DATA" if report.status is ReportStatus.ABSTAINED else "QUALITY_INSUFFICIENT"
    rejected = sum(1 for verdict in report.verdicts if verdict.status is not GroundingStatus.GROUNDED)
    return {
        "completed_goals": (goal_id,) if complete else (),
        "partial_goals": (goal_id,) if partial else (),
        "unresolved_goals": (goal_id,) if unresolved else (),
        "contributions": tuple(contributions),
        "evidence": tuple(cited[key] for key in sorted(cited)),
        "metrics": {"report_status": report.status.value, "claims_rejected": rejected,
                    "claims_grounded": len(report.verdicts) - rejected,
                    "sections_abstained": sum(1 for s in report.sections if s.status is SectionStatus.ABSTAINED)},
        "failure_reason": failure,
    }
