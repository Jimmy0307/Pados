# SPDX-License-Identifier: AGPL-3.0-only
"""Contracts of the Structured Problem Solving pack: evidence, claims, profiles and the report candidate.

* ``EvidenceItem`` is bound to exactly one case and to the digest of the source material it summarizes; a short,
  bounded ``statement`` is display text, never an instruction. Optional ``subject``/``assertion`` tokens make an
  item machine-comparable, which is what grounding and conflict detection use.
* ``Claim`` is produced by an UNTRUSTED provider (or a person) and must cite evidence ids; it names every case it
  speaks about (more than one = an explicit cross-case comparison).
* ``ProblemSolvingProfile`` is data: ordered sections with ids and titles. ``EIGHT_D`` is the first profile.
* ``ProblemSolvingReport`` is a candidate: every section is SUPPORTED (by grounded claims), ABSTAINED (insufficient
  evidence) or CONFLICTED; it carries ``semantic_authority = NONE`` and has no acceptance field.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.execution.context import canonical_hash

EVIDENCE_PACK_SCHEMA = "pados.problem_solving.evidence-pack.v1"
CLAIM_SET_SCHEMA = "pados.problem_solving.claims.v1"
REPORT_SCHEMA = "pados.problem_solving.report.v1"
MAX_EVIDENCE = 256
MAX_CLAIMS = 128
MAX_CITATIONS = 16
MAX_CASES = 8
MAX_STATEMENT = 280

_ID = re.compile(r"^[a-z][a-z0-9_.:-]{2,63}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECTION = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProblemSolvingError(ValueError):
    """Closed-code refusal (never echoes evidence or claim text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: str, code: str) -> str:
    if not value or len(value) > MAX_STATEMENT or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(code)
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    MEASUREMENT = "MEASUREMENT"
    DOCUMENT = "DOCUMENT"
    INTERVIEW = "INTERVIEW"
    TEST_RESULT = "TEST_RESULT"


class EvidenceItem(_Frozen):
    evidence_id: str
    case_id: str
    source_id: str  # logical source the material came from; the task authorizes sources, the pack never does
    kind: EvidenceKind
    content_digest: str  # SHA-256 of the source material this item summarizes (material itself never stored)
    statement: str
    subject: str | None = None  # comparable token, e.g. "root_cause"
    assertion: str | None = None  # comparable token, e.g. "worn_bearing"

    @field_validator("evidence_id", "case_id", "source_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("PS_EVIDENCE_ID_INVALID")
        return value

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("PS_EVIDENCE_DIGEST_INVALID")
        return value

    @field_validator("statement")
    @classmethod
    def _statement(cls, value: str) -> str:
        return _text(value, "PS_EVIDENCE_STATEMENT_INVALID")

    @field_validator("subject", "assertion")
    @classmethod
    def _token(cls, value: str | None) -> str | None:
        if value is not None and not _TOKEN.fullmatch(value):
            raise ValueError("PS_EVIDENCE_TOKEN_INVALID")
        return value

    @model_validator(mode="after")
    def _pair(self) -> EvidenceItem:
        if (self.subject is None) != (self.assertion is None):
            raise ValueError("PS_EVIDENCE_TOKEN_INVALID")  # comparable only as a (subject, assertion) pair
        return self


class EvidencePack(_Frozen):
    schema_version: Literal["pados.problem_solving.evidence-pack.v1"] = "pados.problem_solving.evidence-pack.v1"
    cases: tuple[str, ...] = Field(min_length=1, max_length=MAX_CASES)
    items: tuple[EvidenceItem, ...] = Field(max_length=MAX_EVIDENCE)

    @model_validator(mode="after")
    def _coherent(self) -> EvidencePack:
        if len(set(self.cases)) != len(self.cases) or any(not _ID.fullmatch(case) for case in self.cases):
            raise ValueError("PS_EVIDENCE_PACK_INVALID")
        ids = [item.evidence_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("PS_EVIDENCE_PACK_INVALID")
        if any(item.case_id not in self.cases for item in self.items):
            raise ValueError("PS_EVIDENCE_CASE_UNDECLARED")
        # The same material stating the same thing twice is one piece of evidence, not corroboration.
        facts = [(i.case_id, i.source_id, i.content_digest, i.subject, i.assertion) for i in self.items]
        if len(set(facts)) != len(facts):
            raise ValueError("PS_EVIDENCE_DUPLICATE")
        return self

    @property
    def source_ids(self) -> frozenset[str]:
        return frozenset(item.source_id for item in self.items)

    @property
    def digest(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((item for item in self.items if item.evidence_id == evidence_id), None)


class Claim(_Frozen):
    claim_id: str
    case_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_CASES)
    section: str
    subject: str
    assertion: str
    text: str
    citations: tuple[str, ...] = Field(default=(), max_length=MAX_CITATIONS)

    @field_validator("claim_id")
    @classmethod
    def _claim_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("PS_CLAIM_INVALID")
        return value

    @field_validator("case_ids", "citations")
    @classmethod
    def _ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _ID.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("PS_CLAIM_INVALID")
        return value

    @field_validator("section")
    @classmethod
    def _section(cls, value: str) -> str:
        if not _SECTION.fullmatch(value):
            raise ValueError("PS_CLAIM_INVALID")
        return value

    @field_validator("subject", "assertion")
    @classmethod
    def _token(cls, value: str) -> str:
        if not _TOKEN.fullmatch(value):
            raise ValueError("PS_CLAIM_INVALID")
        return value

    @field_validator("text")
    @classmethod
    def _claim_text(cls, value: str) -> str:
        return _text(value, "PS_CLAIM_INVALID")


class ClaimSet(_Frozen):
    schema_version: Literal["pados.problem_solving.claims.v1"] = "pados.problem_solving.claims.v1"
    claims: tuple[Claim, ...] = Field(max_length=MAX_CLAIMS)

    @model_validator(mode="after")
    def _unique(self) -> ClaimSet:
        ids = [claim.claim_id for claim in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("PS_CLAIM_INVALID")
        return self


class ProfileSection(_Frozen):
    section_id: str
    title: str
    required: bool = True


class ProblemSolvingProfile(_Frozen):
    profile_id: str
    version: str
    sections: tuple[ProfileSection, ...]

    def section_ids(self) -> tuple[str, ...]:
        return tuple(section.section_id for section in self.sections)


EIGHT_D = ProblemSolvingProfile(profile_id="8d", version="1", sections=(
    ProfileSection(section_id="D1", title="Team"),
    ProfileSection(section_id="D2", title="Problem description"),
    ProfileSection(section_id="D3", title="Containment actions"),
    ProfileSection(section_id="D4", title="Root cause"),
    ProfileSection(section_id="D5", title="Permanent corrective actions"),
    ProfileSection(section_id="D6", title="Implementation and validation"),
    ProfileSection(section_id="D7", title="Prevention of recurrence"),
    ProfileSection(section_id="D8", title="Closure and recognition"),
))
# A second profile proves the pipeline is generic: a profile is data, never a code path.
A3 = ProblemSolvingProfile(profile_id="a3", version="1", sections=(
    ProfileSection(section_id="BACKGROUND", title="Background"),
    ProfileSection(section_id="CURRENT", title="Current condition"),
    ProfileSection(section_id="GOAL", title="Goal"),
    ProfileSection(section_id="ROOT_CAUSE", title="Root cause analysis"),
    ProfileSection(section_id="COUNTERMEASURE", title="Countermeasures"),
    ProfileSection(section_id="PLAN", title="Implementation plan"),
    ProfileSection(section_id="FOLLOW_UP", title="Follow-up", required=False),
))
PROFILES = {profile.profile_id: profile for profile in (EIGHT_D, A3)}


class GroundingStatus(StrEnum):
    GROUNDED = "GROUNDED"  # cites same-case evidence stating the same assertion; no evidence disagrees
    UNSUPPORTED = "UNSUPPORTED"  # no / unknown citations, or the citations do not state this assertion
    CASE_MISMATCH = "CASE_MISMATCH"  # cites another case's evidence, or a cross-case claim misses a case
    CONTRADICTED = "CONTRADICTED"  # its own citations state a different assertion on the subject
    CONFLICTED = "CONFLICTED"  # its cases hold evidence disagreeing on the subject: needs human resolution


class SectionStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    ABSTAINED = "ABSTAINED"  # insufficient grounded evidence: never filled in
    CONFLICTED = "CONFLICTED"


class ReportStatus(StrEnum):
    COMPLETE = "COMPLETE"  # every required section supported, nothing rejected, critic agreed
    INCOMPLETE = "INCOMPLETE"  # some required section abstained
    ABSTAINED = "ABSTAINED"  # insufficient evidence for any section: validation required, nothing completed
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # conflicts, rejected claims or critic disagreement


class ClaimVerdict(_Frozen):
    claim_id: str
    status: GroundingStatus


class ReportSection(_Frozen):
    section_id: str
    status: SectionStatus
    claim_ids: tuple[str, ...]


class ProblemSolvingReport(_Frozen):
    schema_version: Literal["pados.problem_solving.report.v1"] = "pados.problem_solving.report.v1"
    profile_id: str
    profile_version: str
    evidence_pack_digest: str
    claim_set_digest: str
    sections: tuple[ReportSection, ...]
    verdicts: tuple[ClaimVerdict, ...]
    critic_agrees: bool | None  # None: no critic consulted
    critic_code: str | None
    status: ReportStatus
    semantic_authority: Literal["NONE"] = "NONE"
