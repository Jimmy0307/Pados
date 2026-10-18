# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 12: non-finite provider progress must normalize to MALFORMED_RESPONSE, never raise.

``int(progress)`` on NaN / +inf / -inf raises before Pydantic validation, which would let an exception
escape the capability loop. Numeric normalization must be guarded (type, finite, bounds, conversion) so a
malformed number becomes a normalized ``MALFORMED_RESPONSE`` outcome and a valid number is used.
"""
from __future__ import annotations

import math
from typing import Any

import pytest
from loop_learning_support import ENDPOINT, FakeHttp, learning_request

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.contracts import FailureCode


def _generate(progress: Any) -> FakeHttp:
    return FakeHttp({("POST", "/api/generate-classroom"): (202, {"jobId": "job-1", "step": "researching",
                                                                 "progress": progress})})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_progress_is_normalized_not_raised(bad: float) -> None:
    http = _generate(bad)
    outcome = OpenMaicLearningProvider(ENDPOINT, http).create_session(learning_request())
    assert outcome.failure is FailureCode.MALFORMED_RESPONSE
    assert outcome.session is None


def test_valid_int_progress_is_used() -> None:
    outcome = OpenMaicLearningProvider(ENDPOINT, _generate(42)).create_session(learning_request())
    assert outcome.failure is None and outcome.session is not None
    assert outcome.session.progress_percent == 42


def test_valid_finite_float_progress_is_truncated() -> None:
    outcome = OpenMaicLearningProvider(ENDPOINT, _generate(42.7)).create_session(learning_request())
    assert outcome.failure is None and outcome.session is not None
    assert outcome.session.progress_percent == 42


@pytest.mark.parametrize("value,expected", [(150, 100), (-5, 0)])
def test_out_of_range_finite_progress_is_clamped(value: int, expected: int) -> None:
    outcome = OpenMaicLearningProvider(ENDPOINT, _generate(value)).create_session(learning_request())
    assert outcome.failure is None and outcome.session is not None
    assert outcome.session.progress_percent == expected
