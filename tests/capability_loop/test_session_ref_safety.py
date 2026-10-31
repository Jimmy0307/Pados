# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 11: an unconstrained session_ref must not be interpolated into the request URL.

The adapter builds ``/api/generate-classroom/<session_ref>``. A session_ref such as ``../health``,
``job?admin=true`` or ``a#frag`` could otherwise change the endpoint. The adapter re-validates the ref
against the closed LearningSession session-ref syntax before any transport call and percent-encodes the
path segment, so the final path can never escape the intended endpoint.
"""
from __future__ import annotations

import pytest
from loop_learning_support import ENDPOINT, HEALTH_OK, FakeHttp

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.contracts import FailureCode

_MALICIOUS = [
    "../health",
    "../../api/health",
    "job?admin=true",
    "job#fragment",
    "a/b",
    "a\\b",
    "a b",
    "bad\nref",
    "x" * 200,          # oversized
    "",                 # empty
    ".hidden",          # leading dot (segment could become .. style)
]


@pytest.mark.parametrize("session_ref", _MALICIOUS)
def test_malicious_session_ref_is_refused_before_transport(session_ref: str) -> None:
    http = FakeHttp({**HEALTH_OK})
    outcome = OpenMaicLearningProvider(ENDPOINT, http).session_state(session_ref)
    assert outcome.failure is FailureCode.MALFORMED_RESPONSE
    assert http.calls == []  # no request was built from an unsafe session ref


def test_valid_session_ref_builds_the_expected_path() -> None:
    http = FakeHttp({("GET", "/api/generate-classroom/job-123"): (200, {"jobId": "job-123", "step": "completed",
                                                                        "progress": 100})})
    outcome = OpenMaicLearningProvider(ENDPOINT, http).session_state("job-123")
    assert outcome.failure is None and outcome.session is not None
    assert http.calls == [("GET", "/api/generate-classroom/job-123")]  # exactly the intended endpoint
