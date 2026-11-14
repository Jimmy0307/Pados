# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic hand-off from ranked document passages to a Structured Problem Solving evidence pack.

Each ranked passage becomes evidence items bound to ONE case and ONE task-named source, with ``content_digest``
the SHA-256 of the passage's exact text in the canonical reading-order view (so a changed document yields a
different, stale pack). A line of the form ``<Label>: <value>`` whose label normalizes to a subject token of the
chosen profile (``Root cause:`` -> ``root_cause``) yields a machine-comparable (subject, assertion) item; the
assertion is the value normalized to a token. Every other passage yields one statement-only item, which can be
displayed but can never ground a claim. Statements are bounded display text, never instructions.

The companion evidence map records, for every item, the passage, page and code-point offsets it came from, so a
citation can be checked against the document without trusting the pack. Nothing here interprets meaning: a
label either matches a profile subject token exactly or it is not structured evidence.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from pados.packs.problem_solving.contracts import (
    MAX_STATEMENT,
    EvidenceItem,
    EvidenceKind,
    EvidencePack,
    ProblemSolvingProfile,
)
from pados.packs.retrieval.lexical import Passage

EVIDENCE_MAP_SCHEMA: Final = "pados.document-intelligence.evidence-map.v1"
MAX_ITEMS_PER_PASSAGE = 8
_LABELLED = re.compile(r"^\s*([^\W\d_][\w /&-]{0,47}?)\s*[:：]\s*(\S.*?)\s*$")
_NON_TOKEN = re.compile(r"[^a-z0-9]+")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def token(text: str, *, numeric_prefix: bool = False) -> str | None:
    """NFKC, casefold, non-alphanumeric runs -> ``_``; ``None`` if no valid token remains. A value that starts with
    a digit (``12 ppm``) becomes ``v_12_ppm`` when ``numeric_prefix`` is set (assertions); labels never get one."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    value = _NON_TOKEN.sub("_", folded).strip("_")
    if numeric_prefix and value[:1].isdigit():
        value = f"v_{value}"
    value = value[:64].rstrip("_")
    return value if _TOKEN.fullmatch(value) else None


def statement(text: str) -> str:
    """Display text: printable characters only, whitespace collapsed, bounded to the pack's statement limit."""
    printable = "".join(ch if unicodedata.category(ch) not in _UNPRINTABLE else " " for ch in text)
    collapsed = " ".join(printable.split())
    return collapsed[:MAX_STATEMENT].rstrip() or "(empty passage)"


@dataclass(frozen=True)
class MappedItem:
    evidence_id: str
    passage_id: str
    page: int
    char_start: int
    char_end: int
    line: int | None
    text_sha256: str


def _subjects(profile: ProblemSolvingProfile) -> frozenset[str]:
    return frozenset(subject for section in profile.sections for subject in section.subjects)


def passage_items(passage: Passage, *, case_id: str, source_id: str,
                  subjects: frozenset[str]) -> list[tuple[EvidenceItem, int | None]]:
    digest = hashlib.sha256(passage.text.encode("utf-8")).hexdigest()
    base = f"ev.{passage.passage_id}"
    items: list[tuple[EvidenceItem, int | None]] = []
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(passage.text.splitlines()):
        match = _LABELLED.match(line)
        subject = token(match.group(1)) if match else None
        assertion = token(match.group(2), numeric_prefix=True) if match else None
        if match is None or subject not in subjects or assertion is None or (subject, assertion) in seen:
            continue
        assert subject is not None
        seen.add((subject, assertion))
        items.append((EvidenceItem(evidence_id=f"{base}.{len(items)}", case_id=case_id, source_id=source_id,
                                   kind=EvidenceKind.DOCUMENT, content_digest=digest,
                                   statement=statement(f"{match.group(1)}: {match.group(2)}"),
                                   subject=subject, assertion=assertion), index))
        if len(items) >= MAX_ITEMS_PER_PASSAGE:
            break
    if not items:
        items.append((EvidenceItem(evidence_id=f"{base}.0", case_id=case_id, source_id=source_id,
                                   kind=EvidenceKind.DOCUMENT, content_digest=digest,
                                   statement=statement(passage.text)), None))
    return items


def build_evidence(ranked: Sequence[Passage], *, case_id: str, source_id: str, profile: ProblemSolvingProfile,
                   max_items: int) -> tuple[EvidencePack, tuple[MappedItem, ...]]:
    """The evidence pack for the ranked passages (rank order, at most ``max_items`` items) and its map."""
    subjects = _subjects(profile)
    items: list[EvidenceItem] = []
    mapped: list[MappedItem] = []
    facts: set[tuple[Any, ...]] = set()
    for passage in ranked:
        for item, line in passage_items(passage, case_id=case_id, source_id=source_id, subjects=subjects):
            fact = (item.content_digest, item.subject, item.assertion,
                    item.statement if item.subject is None else None)
            if fact in facts:  # the same material stating the same thing is one piece of evidence
                continue
            if len(items) >= max_items:
                break
            facts.add(fact)
            items.append(item)
            mapped.append(MappedItem(item.evidence_id, passage.passage_id, passage.page, passage.char_start,
                                     passage.char_end, line, item.content_digest))
    return EvidencePack(cases=(case_id,), items=tuple(items)), tuple(mapped)


def evidence_map(pack: EvidencePack, mapped: Sequence[MappedItem], *, document_sha256: str, source_sha256: str,
                 ranking_sha256: str) -> dict[str, Any]:
    return {"schema": EVIDENCE_MAP_SCHEMA, "semantic_authority": "NONE", "evidence_pack_digest": pack.digest,
            "document_sha256": document_sha256, "source_sha256": source_sha256, "ranking_sha256": ranking_sha256,
            "items": [{"evidence_id": item.evidence_id, "passage_id": item.passage_id, "page": item.page,
                       "char_start": item.char_start, "char_end": item.char_end, "line": item.line,
                       "text_sha256": item.text_sha256} for item in mapped]}
