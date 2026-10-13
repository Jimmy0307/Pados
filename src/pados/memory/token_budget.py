# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass
from typing import Protocol

from pados.models.context_memory import ExclusionReason, MemoryCandidate


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int:
        """Return a deterministic token estimate."""


class FallbackTokenEstimator:
    def estimate(self, text: str) -> int:
        return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class BudgetRequest:
    max_tokens: int
    reserved_system_tokens: int
    reserved_instruction_tokens: int
    reserved_output_tokens: int
    project_id: str
    purpose: str
    allowed_memory_types: tuple[str, ...]
    minimum_verification_status: str = "VERIFIED"


@dataclass(frozen=True)
class ExcludedCandidate:
    memory_id: str
    title: str
    reason: ExclusionReason


@dataclass(frozen=True)
class BudgetResult:
    available_context_tokens: int
    selected: tuple[MemoryCandidate, ...]
    excluded: tuple[ExcludedCandidate, ...]
    remaining_token_budget: int


def calculate_budget(
    request: BudgetRequest,
    candidates: list[MemoryCandidate],
) -> BudgetResult:
    reserved = (
        request.reserved_system_tokens
        + request.reserved_instruction_tokens
        + request.reserved_output_tokens
    )
    available = request.max_tokens - reserved
    if available < 0:
        raise ValueError("reserved tokens exceed max_tokens")
    selected: list[MemoryCandidate] = []
    excluded: list[ExcludedCandidate] = []
    seen_hashes: set[str] = set()
    remaining = available
    for candidate in candidates:
        if candidate.project_id != request.project_id:
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.OUTSIDE_PROJECT)
            )
            continue
        if request.allowed_memory_types and candidate.memory_type not in request.allowed_memory_types:
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.LOW_RELEVANCE)
            )
            continue
        if not candidate.context_eligible:
            reason = (
                ExclusionReason.LEGACY_UNKNOWN
                if candidate.authority_state == "LEGACY_UNKNOWN"
                else ExclusionReason.SUPERSEDED
                if candidate.authority_state == "SUPERSEDED"
                else ExclusionReason.AUTHORITY_NOT_PROMOTED
            )
            excluded.append(ExcludedCandidate(candidate.memory_id, candidate.title, reason))
            continue
        if request.minimum_verification_status == "VERIFIED" and candidate.verification_status != "VERIFIED":
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.UNVERIFIED)
            )
            continue
        if candidate.freshness_status not in {"CURRENT", "UNKNOWN"}:
            excluded.append(ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.STALE))
            continue
        if candidate.verification_status == "DEPRECATED":
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.SUPERSEDED)
            )
            continue
        duplicate_key = candidate.source_hash or f"{candidate.source_uri}:{candidate.title}"
        if duplicate_key in seen_hashes:
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.DUPLICATE)
            )
            continue
        if candidate.estimated_tokens > remaining:
            excluded.append(
                ExcludedCandidate(candidate.memory_id, candidate.title, ExclusionReason.TOKEN_BUDGET)
            )
            continue
        selected.append(candidate)
        seen_hashes.add(duplicate_key)
        remaining -= candidate.estimated_tokens
    return BudgetResult(
        available_context_tokens=available,
        selected=tuple(selected),
        excluded=tuple(excluded),
        remaining_token_budget=remaining,
    )
