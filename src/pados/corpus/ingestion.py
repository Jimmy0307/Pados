# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class PageLike(Protocol):
    """A parsed page; only ``page_number`` is required, other fields are optional."""

    @property
    def page_number(self) -> int: ...


MAX_PILOT_DOCUMENTS = 100
EMBEDDING_DIMENSION = 32
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


class IngestionState(StrEnum):
    DISCOVERED = "DISCOVERED"
    REGISTERED = "REGISTERED"
    HASHED = "HASHED"
    DUPLICATE_CHECKED = "DUPLICATE_CHECKED"
    PARSED = "PARSED"
    STRUCTURED = "STRUCTURED"
    CHUNKED = "CHUNKED"
    EMBEDDED = "EMBEDDED"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    QUARANTINED_ENCRYPTED = "QUARANTINED_ENCRYPTED"
    QUARANTINED_IMAGE_ONLY_OCR_DISABLED = "QUARANTINED_IMAGE_ONLY_OCR_DISABLED"
    PARSE_FAILED = "PARSE_FAILED"
    FAILED_MALFORMED = "FAILED_MALFORMED"
    FAILED_UNREADABLE = "FAILED_UNREADABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    DUPLICATE = "DUPLICATE"


FAILURE_STATES = {
    IngestionState.REJECTED,
    IngestionState.QUARANTINED,
    IngestionState.QUARANTINED_ENCRYPTED,
    IngestionState.QUARANTINED_IMAGE_ONLY_OCR_DISABLED,
    IngestionState.PARSE_FAILED,
    IngestionState.FAILED_MALFORMED,
    IngestionState.FAILED_UNREADABLE,
    IngestionState.VALIDATION_FAILED,
    IngestionState.UNSUPPORTED,
    IngestionState.DUPLICATE,
}

_FORWARD = [
    IngestionState.DISCOVERED,
    IngestionState.REGISTERED,
    IngestionState.HASHED,
    IngestionState.DUPLICATE_CHECKED,
    IngestionState.PARSED,
    IngestionState.STRUCTURED,
    IngestionState.CHUNKED,
    IngestionState.EMBEDDED,
    IngestionState.VALIDATED,
    IngestionState.PUBLISHED,
]


@dataclass(frozen=True)
class StateTransition:
    from_state: IngestionState | None
    to_state: IngestionState
    event_type: str
    detail: str = ""


@dataclass(frozen=True)
class TextChunk:
    page_number: int
    section: str | None
    chunk_order: int
    text: str
    normalized_text: str
    char_start: int
    char_end: int
    token_estimate: int


def transition(
    from_state: IngestionState | None,
    to_state: IngestionState,
    *,
    detail: str = "",
) -> StateTransition:
    """Validate a fail-closed state transition and return an auditable event."""
    if from_state in FAILURE_STATES:
        raise ValueError(f"terminal ingestion state cannot transition: {from_state}")
    if to_state == IngestionState.DISCOVERED and from_state is not None:
        raise ValueError("DISCOVERED is only a starting state")
    if to_state in FAILURE_STATES:
        return StateTransition(from_state, to_state, f"{to_state.value.lower()}", detail)
    if from_state is None:
        if to_state != IngestionState.DISCOVERED:
            raise ValueError("ingestion must start at DISCOVERED")
    elif from_state != to_state:
        try:
            expected = _FORWARD.index(from_state) + 1
            if _FORWARD[expected] != to_state:
                raise ValueError(f"invalid ingestion transition: {from_state} -> {to_state}")
        except (ValueError, IndexError) as exc:
            raise ValueError(f"invalid ingestion transition: {from_state} -> {to_state}") from exc
    return StateTransition(from_state, to_state, f"{to_state.value.lower()}", detail)


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\u00ad", "").split())


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    from pados.knowledge.identity import physical_file_sha256

    return physical_file_sha256(path, chunk_size=chunk_size)


def _token_hash(value: str) -> tuple[int, float]:
    raw = hashlib.sha256(value.encode("utf-8")).digest()
    index = int.from_bytes(raw[:4], "big") % EMBEDDING_DIMENSION
    sign = 1.0 if raw[4] & 1 else -1.0
    return index, sign


def deterministic_embedding(text: str) -> list[float]:
    """Stable local fallback embedding; no external provider or secret is used."""
    vector = [0.0] * EMBEDDING_DIMENSION
    tokens = _TOKEN_RE.findall(normalize_text(text).casefold())
    for token in tokens:
        index, sign = _token_hash(token)
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    return [round(value / norm, 8) for value in vector] if norm else vector


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if len(left_values) != len(right_values) or not left_values:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)


def chunk_text_pages(pages: Iterable[PageLike], *, chunk_size: int = 1800, overlap: int = 250) -> list[TextChunk]:
    if chunk_size < 200 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be >= 200 and overlap must be smaller than chunk_size")
    chunks: list[TextChunk] = []
    order = 0
    for page in pages:
        normalized = str(getattr(page, "normalized_text", ""))
        if not normalized:
            continue
        start = 0
        page_number = int(page.page_number)
        section = getattr(page, "section", None)
        while start < len(normalized):
            end = min(len(normalized), start + chunk_size)
            text = normalized[start:end]
            chunks.append(
                TextChunk(
                    page_number=page_number,
                    section=section,
                    chunk_order=order,
                    text=text,
                    normalized_text=normalize_text(text),
                    char_start=start,
                    char_end=end,
                    token_estimate=max(1, len(_TOKEN_RE.findall(text))),
                )
            )
            order += 1
            if end == len(normalized):
                break
            start = end - overlap
    return chunks
