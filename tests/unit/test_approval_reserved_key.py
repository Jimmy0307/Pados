# SPDX-License-Identifier: AGPL-3.0-only
"""Security review (2026-11-16) MEDIUM-3: the idempotency keys ``result-candidate:<digest>`` belong to the
ResultCandidate submission path. A caller of the generic ``POST /control/approvals`` could otherwise pre-create (and
approve) the approval a later candidate reuses, stranding that candidate (it could never be decided). The HTTP model
refuses the reserved prefix; the internal submission path does not go through it."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from pados.control.schemas import ApprovalCreate

HASH = "a" * 64


@pytest.mark.parametrize("key", ["result-candidate:" + "b" * 64, "result-candidate:", "Result-Candidate:x",
                                 " result-candidate:x"])
def test_the_result_candidate_key_prefix_is_reserved(key: str) -> None:
    with pytest.raises(ValidationError, match="APPROVAL_IDEMPOTENCY_KEY_RESERVED"):
        ApprovalCreate(project_id="p", proposal_summary="s", proposal_hash=HASH, idempotency_key=key)


@pytest.mark.parametrize("key", ["approval-1", "candidate-result:x", "my-result-candidate:x"])
def test_ordinary_keys_are_accepted(key: str) -> None:
    assert ApprovalCreate(project_id="p", proposal_summary="s", proposal_hash=HASH, idempotency_key=key).idempotency_key == key
