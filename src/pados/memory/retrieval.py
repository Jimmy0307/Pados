# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass

from sqlalchemy.orm import Session

from pados.memory import repository
from pados.memory.token_budget import BudgetRequest, BudgetResult, calculate_budget
from pados.models.context_memory import MemoryCandidate

POLICY_VERSION = "p6.7-context-memory-v1"


@dataclass(frozen=True)
class RetrievalRequest:
    project_id: str
    query: str
    purpose: str
    max_tokens: int
    reserved_system_tokens: int = 200
    reserved_instruction_tokens: int = 200
    reserved_output_tokens: int = 400
    allowed_memory_types: tuple[str, ...] = ()
    include_cross_project: bool = False
    minimum_verification_status: str = "VERIFIED"


def search_memory(session: Session, request: RetrievalRequest) -> list[MemoryCandidate]:
    candidates = repository.search_memory_rows(
        session,
        project_id=request.project_id,
        query=request.query,
        memory_types=request.allowed_memory_types,
        include_cross_project=request.include_cross_project,
    )
    return sorted(
        candidates,
        key=lambda item: (
            item.verification_status != "VERIFIED",
            item.freshness_status != "CURRENT",
            item.source_trust_level != "CANONICAL",
            -item.score,
            item.title,
        ),
    )


def retrieve_with_budget(session: Session, request: RetrievalRequest) -> BudgetResult:
    candidates = search_memory(session, request)
    return calculate_budget(
        BudgetRequest(
            max_tokens=request.max_tokens,
            reserved_system_tokens=request.reserved_system_tokens,
            reserved_instruction_tokens=request.reserved_instruction_tokens,
            reserved_output_tokens=request.reserved_output_tokens,
            project_id=request.project_id,
            purpose=request.purpose,
            allowed_memory_types=request.allowed_memory_types,
            minimum_verification_status=request.minimum_verification_status,
        ),
        candidates,
    )
