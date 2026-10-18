# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for P7.2 learning tests: descriptors, a fake HTTP transport and request builders."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Distribution,
    LicenseInfo,
    Modality,
    VerificationMethod,
)
from pados.capability_loop.learning_contracts import LearningRequest
from pados.capability_loop_runtime.binding import InMemorySessionBindingAuthority
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification


def binding_authority() -> InMemorySessionBindingAuthority:
    """A fresh in-memory (immutable, un-anchored) binding authority for a test. Share ONE per test flow so a
    session bound at create is resolvable by its follow-ups; file-anchor behaviour is tested separately."""
    return InMemorySessionBindingAuthority()

PROJECT = "proj-a"
WORK = "wc-1"
LEARN_CAPABILITY_ID = "ADAPTIVE_LEARNING"
GRANT_CAPABILITY = f"CAPABILITY:{LEARN_CAPABILITY_ID}"
ENDPOINT = "http://127.0.0.1:3000"

LEARNING_CAPABILITY = CapabilityDescriptor(
    capability_id=LEARN_CAPABILITY_ID, version="1", family=CapabilityFamily.LEARNING,
    operation="RUN_LEARNING_SESSION", input_contract="learning.goal/v1", output_contract="learning.session/v1",
    determinism=Determinism.PROBABILISTIC, modalities=(Modality.TEXT,),
    max_classification=DataClassification.INTERNAL, network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.HUMAN_REVIEW,))
LEARNING_ADAPTER = AdapterDescriptor(
    adapter_id="openmaic.adaptive_learning", capability_id=LEARN_CAPABILITY_ID, version="1", backend="http",
    determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.INTERNAL,
    network_requirement=NetworkRequirement.LOCAL_ONLY,
    license=LicenseInfo(spdx="MIT", engine_version="1.2.0", distribution=Distribution.RUNTIME_OPTIONAL))


def contract(**overrides: Any) -> dict[str, Any]:
    base = {"work_contract_id": WORK, "project_id": PROJECT, "required_capabilities": [GRANT_CAPABILITY],
            "skill_ids": [], "mcp_capabilities": [], "secret_refs": []}
    base.update(overrides)
    return base


def grant(**overrides: Any) -> dict[str, Any]:
    base = {"work_contract_id": WORK, "project_id": PROJECT, "capabilities": [GRANT_CAPABILITY],
            "skills": [], "mcp_capabilities": [], "secret_refs": []}
    base.update(overrides)
    return base


def learning_request(**overrides: Any) -> LearningRequest:
    values = dict(request_id="req-1", project_id=PROJECT, work_contract_id=WORK, goal_ref="goal-1",
                  capability_gap_ref="gap:cad_edit", learning_goal="Learn parametric CAD editing", materials=())
    values.update(overrides)
    return LearningRequest(**values)  # type: ignore[arg-type]


class FakeHttp:
    """Scripted HTTP transport: (method, path-suffix) -> (status, json). Records calls; can raise."""

    def __init__(self, routes: Mapping[tuple[str, str], tuple[int, Any]], *, raise_on: str | None = None) -> None:
        self._routes = dict(routes)
        self._raise_on = raise_on
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, body: dict[str, Any] | None,
                 timeout: float) -> tuple[int, Any]:
        path = url.split("127.0.0.1:3000", 1)[-1] if "127.0.0.1:3000" in url else url
        self.calls.append((method, path))
        if self._raise_on is not None and self._raise_on in path:
            raise ConnectionError("boom")
        for (route_method, route_path), result in self._routes.items():
            if route_method == method and path.startswith(route_path):
                return result
        return 404, None


HEALTH_OK = {("GET", "/api/health"): (200, {"success": True, "status": "ok", "version": "1.2.0"})}


def generate_ok(step: str = "queued", progress: int = 0, job_id: str = "job-123") -> dict[tuple[str, str], Any]:
    return {("POST", "/api/generate-classroom"): (202, {"jobId": job_id, "runId": "run-1", "status": "queued",
                                                        "step": step, "progress": progress})}
