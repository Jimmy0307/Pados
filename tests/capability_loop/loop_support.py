# SPDX-License-Identifier: AGPL-3.0-only
"""Shared fixtures for capability-loop tests: descriptors, a fake MCP transport, grants and targets."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
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
from pados.capability_loop.gate import TargetRegistration
from pados.capability_loop.mcp_stdio import McpError
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification

PROJECT = "proj-a"
WORK = "wc-1"
CAPABILITY_ID = "BINARY_INSPECTION"
DISCOVERY_CAPABILITY = CapabilityDescriptor(
    capability_id=CAPABILITY_ID, version="1", family=CapabilityFamily.CODE_INTELLIGENCE,
    operation="INSPECT_RUNTIME_CATALOG", input_contract="target.ref/v1", output_contract="discovery.evidence/v1",
    determinism=Determinism.PROBABILISTIC, modalities=(Modality.CODE,),
    max_classification=DataClassification.CONFIDENTIAL, network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.HUMAN_REVIEW,))
DISCOVERY_ADAPTER = AdapterDescriptor(
    adapter_id="rea.capability_discovery", capability_id=CAPABILITY_ID, version="1", backend="mcp",
    determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.CONFIDENTIAL,
    network_requirement=NetworkRequirement.LOCAL_ONLY,
    license=LicenseInfo(spdx="MIT", engine_version="6.4.0", distribution=Distribution.RUNTIME_OPTIONAL))

GRANT_CAPABILITY = f"CAPABILITY:{CAPABILITY_ID}"


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


def target(tmp_path: Path, *, name: str = "app.bin",
           classification: DataClassification = DataClassification.CONFIDENTIAL,
           authorization: str = "operator-auth-1", project_id: str = PROJECT,
           root: Path | None = None) -> TargetRegistration:
    allowed_root = root or tmp_path
    allowed_root.mkdir(parents=True, exist_ok=True)
    path = allowed_root / name
    path.write_bytes(b"\x7fELF binary fixture")
    return TargetRegistration(target_ref="target:app", project_id=project_id, path=path,
                              classification=classification, owner_authorization_ref=authorization,
                              allowed_root=allowed_root)


def session_document(*, available: bool = True, version: str = "6.4.0",
                     reason: str | None = None) -> dict[str, Any]:
    group: dict[str, Any] = {"names": ["binary_overview"], "available": available}
    if reason is not None:
        group["reason"] = reason
    return {"result": {"capabilities": {}, "tool_availability": {"groups": [group]},
                       "server_identity": {"package": {"name": "rea-agents", "version": version},
                                           "catalog": {"digests": {"combined_sha256": "a" * 64}}}}}


class FakeTransport:
    """A scripted MCP transport: maps tool name -> result dict, or raises a configured McpError."""

    def __init__(self, results: Mapping[str, Any], *, raise_on: str | None = None,
                 error_code: str = "MCP_TIMEOUT") -> None:
        self._results = dict(results)
        self._raise_on = raise_on
        self._error_code = error_code
        self.calls: list[str] = []
        self.closed = False

    def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        self.calls.append(name)
        if self._raise_on == name:
            raise McpError(self._error_code)
        if name not in self._results:
            return {"structuredContent": self._results.get("binary_session", {})}
        return self._results[name]

    def close(self) -> None:
        self.closed = True


def ok_results(session: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = session or session_document()
    return {"binary_session": {"structuredContent": doc}, "open_binary": {"structuredContent": {"result": {}}}}
