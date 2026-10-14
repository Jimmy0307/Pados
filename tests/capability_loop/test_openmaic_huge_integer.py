# SPDX-License-Identifier: AGPL-3.0-only
"""Round-2 Finding 5: an oversized JSON integer must normalize to MALFORMED_RESPONSE, never raise.

NaN/Inf were handled already, but a very large integer (e.g. 1001 digits) raises OverflowError when it is
converted to float for math.isfinite(). Numeric normalization must branch on type: bool rejected, int
compared as an integer (no float conversion), float checked with math.isfinite(); any absurd magnitude is
MALFORMED_RESPONSE and never escapes the loop.
"""
from __future__ import annotations

from typing import Any

import pytest
from loop_learning_support import ENDPOINT, FakeHttp, learning_request

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.contracts import FailureCode

HUGE_POS = 10 ** 1000
HUGE_NEG = -(10 ** 1000)


def _generate(progress: Any) -> FakeHttp:
    return FakeHttp({("POST", "/api/generate-classroom"): (202, {"jobId": "job-1", "step": "researching",
                                                                 "progress": progress})})


@pytest.mark.parametrize("bad", [HUGE_POS, HUGE_NEG])
def test_huge_integer_progress_is_malformed_not_raised(bad: int) -> None:
    outcome = OpenMaicLearningProvider(ENDPOINT, _generate(bad)).create_session(learning_request())
    assert outcome.failure is FailureCode.MALFORMED_RESPONSE
    assert outcome.session is None


def test_bool_progress_is_not_accepted_as_int() -> None:
    outcome = OpenMaicLearningProvider(ENDPOINT, _generate(True)).create_session(learning_request())
    assert outcome.failure is FailureCode.MALFORMED_RESPONSE


def test_normal_and_clamped_values_still_pass() -> None:
    # Round-1 behaviour preserved: a normal int passes, moderate out-of-range clamps.
    ok = OpenMaicLearningProvider(ENDPOINT, _generate(55)).create_session(learning_request())
    assert ok.failure is None and ok.session is not None and ok.session.progress_percent == 55
    clamped = OpenMaicLearningProvider(ENDPOINT, _generate(150)).create_session(learning_request())
    assert clamped.failure is None and clamped.session is not None and clamped.session.progress_percent == 100
