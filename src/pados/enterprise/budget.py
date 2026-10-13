# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded evidence budgets, enforced before any model/provider invocation."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pados.enterprise.classification import DataClassification


class Budgeted(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def token_estimate(self) -> int: ...

    @property
    def classification(self) -> DataClassification: ...


@dataclass(frozen=True)
class EvidenceBudget:
    max_sources: int = 5
    max_chunks: int = 8
    max_context_tokens: int = 2_000
    max_restricted_items: int = 0

    def __post_init__(self) -> None:
        if not (1 <= self.max_sources <= 100 and 1 <= self.max_chunks <= 200
                and 1 <= self.max_context_tokens <= 64_000 and 0 <= self.max_restricted_items <= 50):
            raise ValueError("EVIDENCE_BUDGET_OUT_OF_BOUNDS")

    def as_dict(self) -> dict[str, int]:
        return {"max_sources": self.max_sources, "max_chunks": self.max_chunks,
                "max_context_tokens": self.max_context_tokens,
                "max_restricted_items": self.max_restricted_items}


def apply_budget[T: Budgeted](items: Sequence[T], budget: EvidenceBudget) -> tuple[list[T], dict[str, Any]]:
    """Keep ranked items while every limit holds; report what was dropped and why."""
    kept: list[T] = []
    sources: set[str] = set()
    tokens = restricted = 0
    dropped = {"max_chunks": 0, "max_sources": 0, "max_context_tokens": 0, "max_restricted_items": 0}
    for item in items:
        is_restricted = item.classification is DataClassification.RESTRICTED
        if len(kept) >= budget.max_chunks:
            dropped["max_chunks"] += 1
        elif item.source_id not in sources and len(sources) >= budget.max_sources:
            dropped["max_sources"] += 1
        elif tokens + item.token_estimate > budget.max_context_tokens:
            dropped["max_context_tokens"] += 1
        elif is_restricted and restricted >= budget.max_restricted_items:
            dropped["max_restricted_items"] += 1
        else:
            kept.append(item)
            sources.add(item.source_id)
            tokens += item.token_estimate
            restricted += int(is_restricted)
    return kept, {"limits": budget.as_dict(), "used": {"sources": len(sources), "chunks": len(kept),
                  "context_tokens": tokens, "restricted_items": restricted}, "dropped": dropped}


def assert_within_budget(items: Sequence[Budgeted], budget: EvidenceBudget) -> None:
    """Pre-invocation gate: a package that exceeds its budget is never sent."""
    sources = {item.source_id for item in items}
    tokens = sum(item.token_estimate for item in items)
    restricted = sum(item.classification is DataClassification.RESTRICTED for item in items)
    if (len(items) > budget.max_chunks or len(sources) > budget.max_sources
            or tokens > budget.max_context_tokens or restricted > budget.max_restricted_items):
        raise PermissionError("EVIDENCE_BUDGET_EXCEEDED")
