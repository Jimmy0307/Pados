# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic fixtures for the Structured Problem Solving pack (no real company, case, person or document).

Every value here is invented and public-safe: case ids, source ids, statements and digests are synthetic labels.
"""
from __future__ import annotations

import hashlib
from typing import Any

from pados.packs.problem_solving.contracts import Claim, ClaimSet, EvidenceItem, EvidenceKind, EvidencePack

CASE_A = "case-synthetic-a"
CASE_B = "case-synthetic-b"
SOURCE = "src-synthetic-line-log"
SOURCE_LAB = "src-synthetic-lab-report"
UNVETTED_SOURCE = "src-synthetic-unvetted-forum"
AUTHORIZED = (SOURCE, SOURCE_LAB)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def evidence(evidence_id: str, case_id: str, subject: str | None, assertion: str | None,
             statement: str = "Synthetic observation.", kind: EvidenceKind = EvidenceKind.OBSERVATION,
             source_id: str = SOURCE, material: str | None = None) -> EvidenceItem:
    return EvidenceItem(evidence_id=evidence_id, case_id=case_id, source_id=source_id, kind=kind,
                        content_digest=_digest(material or evidence_id), statement=statement, subject=subject,
                        assertion=assertion)


def claim(claim_id: str, section: str, subject: str, assertion: str, citations: tuple[str, ...],
          cases: tuple[str, ...] = (CASE_A,)) -> Claim:
    return Claim(claim_id=claim_id, case_ids=cases, section=section, subject=subject, assertion=assertion,
                 text=f"Synthetic claim {claim_id}.", citations=citations)


# One supporting (subject, assertion) per 8D section for case A.
SECTION_FACTS = {
    "D1": ("team", "cross_functional_team_formed"),
    "D2": ("defect", "intermittent_seal_leak"),
    "D3": ("containment", "suspect_lot_quarantined"),
    "D4": ("root_cause", "worn_bearing"),
    "D5": ("corrective_action", "bearing_replacement_schedule"),
    "D6": ("validation", "leak_test_passed"),
    "D7": ("prevention", "preventive_maintenance_interval_updated"),
    "D8": ("closure", "team_recognized"),
}

A3_FACTS = {
    "BACKGROUND": ("background", "throughput_target_missed"),
    "CURRENT": ("current_condition", "changeover_takes_forty_minutes"),
    "GOAL": ("goal", "changeover_under_twenty_minutes"),
    "ROOT_CAUSE": ("root_cause", "tooling_not_staged"),
    "COUNTERMEASURE": ("countermeasure", "tooling_cart_introduced"),
    "PLAN": ("plan", "pilot_on_one_line"),
}


def _case(facts: dict[str, tuple[str, str]], case_id: str = CASE_A) -> tuple[EvidencePack, ClaimSet]:
    items = tuple(evidence(f"ev-{section.lower()}", case_id, subject, assertion)
                  for section, (subject, assertion) in facts.items())
    claims = tuple(claim(f"cl-{section.lower()}", section, subject, assertion, (f"ev-{section.lower()}",),
                         (case_id,)) for section, (subject, assertion) in facts.items())
    return EvidencePack(cases=(case_id,), items=items), ClaimSet(claims=claims)


def complete_case() -> tuple[EvidencePack, ClaimSet]:
    return _case(SECTION_FACTS)


def a3_case() -> tuple[EvidencePack, ClaimSet]:
    return _case(A3_FACTS)


def missing_evidence_case() -> tuple[EvidencePack, ClaimSet]:
    """No evidence for D4..D8: those sections must abstain, never be filled."""
    pack, claims = complete_case()
    kept = {"D1", "D2", "D3"}
    return (EvidencePack(cases=pack.cases, items=tuple(i for i in pack.items if i.evidence_id[-2:].upper() in kept)),
            ClaimSet(claims=tuple(c for c in claims.claims if c.section in kept)))


def insufficient_evidence_case() -> tuple[EvidencePack, ClaimSet]:
    """Only free-text observations, nothing machine-comparable: no claim can be grounded, so nothing is drafted."""
    items = (evidence("ev-note-1", CASE_A, None, None, "Synthetic note: something seemed off."),
             evidence("ev-note-2", CASE_A, None, None, "Synthetic note: a second vague remark."))
    return EvidencePack(cases=(CASE_A,), items=items), ClaimSet(claims=())


def conflicting_evidence_case() -> tuple[EvidencePack, ClaimSet]:
    """Two case-A items disagree on the root cause; the D4 claim cites one of them."""
    pack, claims = complete_case()
    extra = evidence("ev-d4-alt", CASE_A, "root_cause", "contaminated_lubricant", kind=EvidenceKind.TEST_RESULT,
                     source_id=SOURCE_LAB)
    return EvidencePack(cases=pack.cases, items=(*pack.items, extra)), claims


def duplicate_evidence_items() -> tuple[EvidenceItem, ...]:
    """The D4 item restated under a second id from the same material: one fact, not two."""
    pack, _ = complete_case()
    original = next(item for item in pack.items if item.evidence_id == "ev-d4")
    return (*pack.items, original.model_copy(update={"evidence_id": "ev-d4-again"}))


def cross_case() -> tuple[EvidencePack, ClaimSet]:
    """A comparison claim spanning two cases must cite each case; one citing only case A is a CASE_MISMATCH and
    a case-A claim citing case-B evidence is a cross-case leakage attempt."""
    items = (evidence("ev-a-rc", CASE_A, "root_cause", "worn_bearing"),
             evidence("ev-b-rc", CASE_B, "root_cause", "worn_bearing"))
    claims = (claim("cl-shared", "D4", "root_cause", "worn_bearing", ("ev-a-rc", "ev-b-rc"), (CASE_A, CASE_B)),
              claim("cl-half", "D4", "root_cause", "worn_bearing", ("ev-a-rc",), (CASE_A, CASE_B)),
              claim("cl-wrong-case", "D4", "root_cause", "worn_bearing", ("ev-b-rc",), (CASE_A,)))
    return EvidencePack(cases=(CASE_A, CASE_B), items=items), ClaimSet(claims=claims)


def unsupported_claim_case() -> tuple[EvidencePack, ClaimSet]:
    """A generated claim with an invented citation, one with none, and one its citation contradicts."""
    pack, claims = complete_case()
    invented = (claim("cl-invented", "D4", "root_cause", "operator_error", ("ev-does-not-exist",)),
                claim("cl-uncited", "D5", "corrective_action", "retrain_everyone", ()),
                claim("cl-contradicted", "D4", "root_cause", "operator_error", ("ev-d4",)))
    return pack, ClaimSet(claims=(*claims.claims, *invented))


def unauthorized_source_case() -> tuple[EvidencePack, ClaimSet]:
    """One item comes from a source the task never authorized."""
    pack, claims = complete_case()
    foreign = evidence("ev-d4-forum", CASE_A, "root_cause", "worn_bearing", source_id=UNVETTED_SOURCE)
    return EvidencePack(cases=pack.cases, items=(*pack.items, foreign)), claims


def raw_claims(claims: ClaimSet) -> dict[str, Any]:
    return claims.model_dump(mode="json")
