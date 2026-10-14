# SPDX-License-Identifier: AGPL-3.0-only
"""Round-2 Finding 4: an unknown-session refusal must never let a malformed project id raise.

The refusal receipt for a guessed/unknown session used the caller's raw project_id. A malformed value
("x", "../../bad", control chars, oversized) would fail OperationReceipt validation and raise instead of
returning a normal SESSION_NOT_BOUND refusal. The caller project id is validated first and a bounded safe
placeholder is used when it is not a valid identifier.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from loop_learning_support import (
    ENDPOINT,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    contract,
    grant,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.gate import GateCode
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore

_BAD_PROJECTS = ["x", "../../bad", "bad\nproject", "p" * 300, "", "UPPER_CASE"]


def _service(tmp_path: Path, http: FakeHttp) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http), LoopStore(tmp_path / "loop.json"),
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER)


def test_valid_project_id_gives_normal_unknown_session_receipt(tmp_path: Path) -> None:
    http = FakeHttp({})
    receipt = _service(tmp_path, http).session_state(
        session_ref="job-unknown", contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_NOT_BOUND
    assert receipt.project_id == "proj-a"
    assert http.calls == []  # provider never contacted for an unknown session


@pytest.mark.parametrize("bad_project", _BAD_PROJECTS)
def test_malformed_project_id_does_not_raise(tmp_path: Path, bad_project: str) -> None:
    http = FakeHttp({})
    # Must not raise an OperationReceipt validation error; must return a normal refusal.
    receipt = _service(tmp_path, http).session_state(
        session_ref="job-unknown", contract=contract(project_id=bad_project),
        grant=grant(project_id=bad_project))
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == GateCode.SESSION_NOT_BOUND
    assert receipt.project_id == "unresolved-project"  # bounded safe placeholder
    assert http.calls == []
