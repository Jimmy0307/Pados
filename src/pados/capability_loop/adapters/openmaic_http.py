# SPDX-License-Identifier: AGPL-3.0-only
"""OpenMAIC adaptive-learning adapter (external HTTP runtime, THU-MAIC/OpenMAIC; MIT).

OpenMAIC is an optional, operator-run local runtime (Next.js + PostgreSQL) reached over loopback HTTP. It
is not PADOS authority: this adapter creates a learning session from a goal, polls its state, and reports a
normalized learner-progress observation. It never writes PADOS semantic state.

Model routing is external: OpenMAIC chooses its model from its own server configuration, so every session
is reported ``model_routing = EXTERNAL_PROVIDER_CONTROLLED``. This adapter does not claim to route it.

Boundaries:
* The endpoint must be loopback (OpenMAIC is a local runtime); a non-loopback endpoint fails closed.
* Responses are reduced to status, progress and digests. OpenMAIC free text, generated content, material
  bytes and session internals are never persisted.
* OpenMAIC's learner-progress storage is an internal, undocumented contract; this adapter does not depend
  on it. Assessment reports ``UNASSESSED`` with the limitation ``OPENMAIC_PROGRESS_API_EXTERNAL`` rather
  than inventing mastery it cannot observe through a stable public API.
* Any failure (absent, refused, timeout, malformed, oversized, unauthorized) becomes a normalized
  ``LearningOutcome`` with a ``FailureCode``.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from pados.capability_loop.contracts import (
    Confidence,
    FailureCode,
    ProviderHealth,
    ProviderStatus,
)
from pados.capability_loop.learning_contracts import (
    LearningAssessment,
    LearningOutcome,
    LearningRequest,
    LearningSession,
    LearningSessionStatus,
    MasteryLevel,
    ModelRouting,
    is_valid_session_ref,
)
from pados.connectivity.egress import endpoint_is_loopback
from pados.execution.context import canonical_hash

OPENMAIC_PROVIDER_ID = "openmaic.adaptive_learning"
DEFAULT_TIMEOUT_SECONDS = 60.0


def _normalized_percent(value: Any) -> int | None:
    """Guarded numeric normalization for a provider percent: clamp to [0, 100], or signal malformed.

    Returns an int in [0, 100] for an absent (-> 0), finite int or finite float value (out-of-range values
    clamp). Returns ``None`` for a present-but-malformed number (a bool, a non-number, or a non-finite
    NaN/Inf), which the caller turns into a normalized MALFORMED_RESPONSE rather than letting ``int()``
    raise into the loop.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, int(value)))


# Normalized HTTP transport: (method, path, body, timeout) -> (status, json | None). Injectable for tests.
LearningTransport = Callable[[str, str, dict[str, Any] | None, float], tuple[int, Any]]

_STEP_STATUS = {
    "queued": LearningSessionStatus.REQUESTED,
    "initializing": LearningSessionStatus.IN_PROGRESS,
    "researching": LearningSessionStatus.IN_PROGRESS,
    "generating_outlines": LearningSessionStatus.IN_PROGRESS,
    "generating_scenes": LearningSessionStatus.IN_PROGRESS,
    "generating_media": LearningSessionStatus.IN_PROGRESS,
    "completed": LearningSessionStatus.READY,
    "failed": LearningSessionStatus.FAILED,
}


class OpenMaicLearningProvider:
    """``LearningProvider`` backed by an OpenMAIC HTTP runtime on loopback."""

    provider_id = OPENMAIC_PROVIDER_ID

    def __init__(self, endpoint: str | None, transport: LearningTransport, *,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 expected_version: str | None = None) -> None:
        # endpoint is None when OpenMAIC is not configured: the provider is then UNAVAILABLE.
        self._endpoint = endpoint.rstrip("/") if endpoint else None
        self._call = transport
        self._timeout = timeout_seconds
        self._expected_version = expected_version

    # -- health -------------------------------------------------------------
    def health(self) -> ProviderHealth:
        if self._endpoint is None:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.UNAVAILABLE,
                                  code=FailureCode.PROVIDER_NOT_CONFIGURED.value)
        if not endpoint_is_loopback(self._endpoint):
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED,
                                  code=FailureCode.UNAUTHORIZED.value)
        status, body = self._safe("GET", "/api/health", None)
        if status != 200 or not isinstance(body, Mapping) or body.get("status") != "ok":
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED,
                                  code=FailureCode.PROVIDER_ABSENT.value)
        version = body.get("version")
        version_str = version[:40] if isinstance(version, str) else None
        if self._expected_version is not None and version_str is not None and version_str != self._expected_version:
            return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.DEGRADED,
                                  code=FailureCode.INCOMPATIBLE_VERSION.value, runtime_version=version_str)
        return ProviderHealth(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, code=None,
                              runtime_version=version_str)

    # -- operations ---------------------------------------------------------
    def create_session(self, request: LearningRequest) -> LearningOutcome:
        guard = self._guard()
        if guard is not None:
            return guard
        material_ids = [material.material_ref for material in request.materials]
        status, body = self._safe("POST", "/api/generate-classroom",
                                  {"requirement": request.learning_goal, "materialIds": material_ids})
        if status == 429:
            return self._failed(FailureCode.PROVIDER_ERROR)
        if status == 400:
            return self._failed(FailureCode.UNSUPPORTED_CAPABILITY)
        if status not in (200, 202) or not isinstance(body, Mapping):
            return self._failed(FailureCode.MALFORMED_RESPONSE)
        session = self._session_from_job(body, materials_bound=len(material_ids))
        if session is None:
            return self._failed(FailureCode.MALFORMED_RESPONSE)
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=session)

    def session_state(self, session_ref: str) -> LearningOutcome:
        guard = self._guard()
        if guard is not None:
            return guard
        # Re-validate the session ref against the closed syntax and percent-encode it as a single path
        # segment, so a forged ref can never add a path, query or fragment to the request URL.
        if not is_valid_session_ref(session_ref):
            return self._failed(FailureCode.MALFORMED_RESPONSE)
        status, body = self._safe("GET", f"/api/generate-classroom/{quote(session_ref, safe='')}", None)
        if status == 404:
            return self._failed(FailureCode.UNSUPPORTED_CAPABILITY)
        if status != 200 or not isinstance(body, Mapping):
            return self._failed(FailureCode.MALFORMED_RESPONSE)
        session = self._session_from_job(body, materials_bound=0)
        if session is None:
            return self._failed(FailureCode.MALFORMED_RESPONSE)
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE, session=session)

    def assessment(self, session_ref: str, capability_gap_ref: str) -> LearningOutcome:
        guard = self._guard()
        if guard is not None:
            return guard
        state = self.session_state(session_ref)
        if state.failure is not None or state.session is None:
            return state
        ready = state.session.status is LearningSessionStatus.READY
        # OpenMAIC exposes no stable public learner-progress API; report readiness, not invented mastery.
        digest = canonical_hash({"session": session_ref, "gap": capability_gap_ref,
                                 "status": state.session.status.value, "progress": state.session.progress_percent})
        assessment = LearningAssessment(
            provider_id=self.provider_id, session_ref=session_ref, capability_gap_ref=capability_gap_ref,
            human_mastery=MasteryLevel.UNASSESSED, score_available=False, score_percent=None,
            confidence=Confidence.INFERRED, evidence_digest=digest,
            limitations=("OPENMAIC_PROGRESS_API_EXTERNAL",) if ready else
            ("OPENMAIC_PROGRESS_API_EXTERNAL", "SESSION_NOT_READY"))
        return LearningOutcome(provider_id=self.provider_id, status=ProviderStatus.AVAILABLE,
                               session=state.session, assessment=assessment)

    # -- internals ----------------------------------------------------------
    def _guard(self) -> LearningOutcome | None:
        if self._endpoint is None:
            return self._failed(FailureCode.PROVIDER_NOT_CONFIGURED, ProviderStatus.UNAVAILABLE)
        if not endpoint_is_loopback(self._endpoint):
            return self._failed(FailureCode.UNAUTHORIZED)
        return None

    def _session_from_job(self, body: Mapping[str, Any], *, materials_bound: int) -> LearningSession | None:
        job_id = body.get("jobId") or body.get("runId")
        if not isinstance(job_id, str) or not job_id:
            return None
        step = body.get("step")
        status = _STEP_STATUS.get(step, LearningSessionStatus.UNKNOWN) if isinstance(step, str) \
            else LearningSessionStatus.UNKNOWN
        progress_int = _normalized_percent(body.get("progress"))
        if progress_int is None:  # a present-but-malformed number (non-finite / wrong type) is malformed
            return None
        try:
            return LearningSession(
                provider_id=self.provider_id, session_ref=job_id, status=status,
                model_routing=ModelRouting.EXTERNAL_PROVIDER_CONTROLLED,
                materials_bound=materials_bound, progress_percent=progress_int)
        except ValueError:
            return None

    def _safe(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        try:
            return self._call(method, f"{self._endpoint}{path}", body, self._timeout)
        except Exception:  # noqa: BLE001 - transport errors are normalized, never raised into PADOS
            return 0, None

    def _failed(self, failure: FailureCode, status: ProviderStatus = ProviderStatus.DEGRADED) -> LearningOutcome:
        return LearningOutcome(provider_id=self.provider_id, status=status, failure=failure)
