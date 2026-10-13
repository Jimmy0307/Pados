from pados.memory.token_budget import BudgetRequest, calculate_budget
from pados.models.context_memory import MemoryCandidate


def _candidate(
    memory_id: str,
    *,
    project_id: str = "project-a",
    verification_status: str = "VERIFIED",
    freshness_status: str = "CURRENT",
    source_hash: str | None = None,
    estimated_tokens: int = 10,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_id=memory_id,
        project_id=project_id,
        memory_type="semantic",
        title=f"title-{memory_id}",
        content_summary="summary",
        source_type="observation",
        source_uri=f"source:{memory_id}",
        source_hash=source_hash,
        source_trust_level="CANONICAL",
        verification_status=verification_status,
        freshness_status=freshness_status,
        importance=50,
        retrieval_priority=50,
        estimated_tokens=estimated_tokens,
        score=1.0,
        authority_state="PROMOTED",
        context_eligible=True,
    )


def test_context_budget_records_exclusion_reasons_deterministically() -> None:
    request = BudgetRequest(
        max_tokens=50,
        reserved_system_tokens=5,
        reserved_instruction_tokens=5,
        reserved_output_tokens=10,
        project_id="project-a",
        purpose="test",
        allowed_memory_types=("semantic",),
    )
    candidates = [
        _candidate("one", source_hash="same"),
        _candidate("outside", project_id="project-b"),
        _candidate("unverified", verification_status="UNVERIFIED"),
        _candidate("stale", freshness_status="STALE"),
        _candidate("duplicate", source_hash="same"),
        _candidate("large", estimated_tokens=100),
    ]

    result = calculate_budget(request, candidates)

    assert [item.memory_id for item in result.selected] == ["one"]
    assert result.remaining_token_budget == 20
    assert [item.reason.value for item in result.excluded] == [
        "OUTSIDE_PROJECT",
        "UNVERIFIED",
        "STALE",
        "DUPLICATE",
        "TOKEN_BUDGET",
    ]


def test_context_budget_rejects_over_reserved_request() -> None:
    request = BudgetRequest(
        max_tokens=10,
        reserved_system_tokens=5,
        reserved_instruction_tokens=5,
        reserved_output_tokens=5,
        project_id="project-a",
        purpose="test",
        allowed_memory_types=("semantic",),
    )

    try:
        calculate_budget(request, [])
    except ValueError as exc:
        assert "reserved tokens exceed max_tokens" in str(exc)
    else:
        raise AssertionError("expected ValueError")
