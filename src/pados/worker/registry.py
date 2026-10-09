# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

import httpx

from pados.config import Settings
from pados.worker.schemas import (
    CapabilityId,
    CapabilitySpec,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)


def capability_registry() -> dict[CapabilityId, CapabilitySpec]:
    return {
        CapabilityId.READ_REPOSITORY: CapabilitySpec(
            capability_id=CapabilityId.READ_REPOSITORY,
            description="Read project files and metadata without mutation.",
            minimum_risk_level=RiskLevel.LOW,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=False,
            requires_validation=False,
            allowed_worker_types=[
                WorkerType.DETERMINISTIC_TOOL,
                WorkerType.OLLAMA,
                WorkerType.CLAUDE_CODE,
                WorkerType.CODEX_CLI,
            ],
            default_timeout=30,
        ),
        CapabilityId.RUN_STATIC_ANALYSIS: CapabilitySpec(
            capability_id=CapabilityId.RUN_STATIC_ANALYSIS,
            description="Run registered static analysis commands.",
            minimum_risk_level=RiskLevel.LOW,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=False,
            requires_validation=True,
            allowed_worker_types=[WorkerType.DETERMINISTIC_TOOL, WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=120,
        ),
        CapabilityId.RUN_TESTS: CapabilitySpec(
            capability_id=CapabilityId.RUN_TESTS,
            description="Run registered test commands.",
            minimum_risk_level=RiskLevel.LOW,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=False,
            requires_validation=True,
            allowed_worker_types=[WorkerType.DETERMINISTIC_TOOL, WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=180,
        ),
        CapabilityId.MODIFY_MARKDOWN: CapabilitySpec(
            capability_id=CapabilityId.MODIFY_MARKDOWN,
            description="Modify governed Markdown in an isolated worktree.",
            minimum_risk_level=RiskLevel.MEDIUM,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=True,
            requires_validation=True,
            allowed_worker_types=[WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=180,
        ),
        CapabilityId.MODIFY_CODE: CapabilitySpec(
            capability_id=CapabilityId.MODIFY_CODE,
            description="Modify code in an isolated worktree.",
            minimum_risk_level=RiskLevel.MEDIUM,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=True,
            requires_validation=True,
            allowed_worker_types=[WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=300,
        ),
        CapabilityId.SUMMARIZE: CapabilitySpec(
            capability_id=CapabilityId.SUMMARIZE,
            description="Generate a bounded structured summary.",
            minimum_risk_level=RiskLevel.LOW,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=False,
            requires_validation=True,
            allowed_worker_types=[WorkerType.OLLAMA, WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=60,
        ),
        CapabilityId.BUILD_RELEASE: CapabilitySpec(
            capability_id=CapabilityId.BUILD_RELEASE,
            description="Build allowlisted release artifacts without publishing.",
            minimum_risk_level=RiskLevel.MEDIUM,
            required_approval_type="CONTROL_APPROVAL",
            requires_isolation=True,
            requires_validation=True,
            allowed_worker_types=[WorkerType.DETERMINISTIC_TOOL, WorkerType.CLAUDE_CODE, WorkerType.CODEX_CLI],
            default_timeout=300,
        ),
    }


def _version(command: str) -> tuple[WorkerAvailability, str | None, str]:
    executable = shutil.which(command)
    if executable is None:
        return WorkerAvailability.UNAVAILABLE, None, "executable not found"
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WorkerAvailability.UNAVAILABLE, None, str(exc)
    output = (completed.stdout + completed.stderr).strip().splitlines()
    if completed.returncode != 0:
        return WorkerAvailability.UNAVAILABLE, None, output[0] if output else "version check failed"
    return WorkerAvailability.AVAILABLE, output[0] if output else command, ""


def _ollama_spec(settings: Settings) -> WorkerSpec:
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=2.0)
    except httpx.HTTPError as exc:
        return WorkerSpec(
            worker_id="ollama-local",
            worker_type=WorkerType.OLLAMA,
            display_name="Ollama Local Worker",
            availability=WorkerAvailability.UNAVAILABLE,
            capabilities=[CapabilityId.SUMMARIZE, CapabilityId.CLASSIFY_REQUEST],
            risk_ceiling=RiskLevel.LOW,
            supports_structured_output=True,
            supports_file_changes=False,
            supports_tool_calls=False,
            supports_streaming=False,
            max_context_tokens=4096,
            default_timeout_seconds=60,
            reason=str(exc),
        )
    if response.status_code != 200:
        return WorkerSpec(
            worker_id="ollama-local",
            worker_type=WorkerType.OLLAMA,
            display_name="Ollama Local Worker",
            availability=WorkerAvailability.UNAVAILABLE,
            capabilities=[CapabilityId.SUMMARIZE, CapabilityId.CLASSIFY_REQUEST],
            risk_ceiling=RiskLevel.LOW,
            supports_structured_output=True,
            supports_file_changes=False,
            supports_tool_calls=False,
            supports_streaming=False,
            max_context_tokens=4096,
            default_timeout_seconds=60,
            reason=f"endpoint returned {response.status_code}",
        )
    models = [str(item.get("name", "")) for item in response.json().get("models", [])]
    if settings.chat_model and settings.chat_model not in models:
        availability = WorkerAvailability.UNAVAILABLE
        reason = "configured model not in allowlist"
    else:
        availability = WorkerAvailability.AVAILABLE if models else WorkerAvailability.UNAVAILABLE
        reason = "" if models else "no local models listed"
    return WorkerSpec(
        worker_id="ollama-local",
        worker_type=WorkerType.OLLAMA,
        display_name="Ollama Local Worker",
        version="local-endpoint",
        availability=availability,
        capabilities=[CapabilityId.SUMMARIZE, CapabilityId.CLASSIFY_REQUEST],
        risk_ceiling=RiskLevel.LOW,
        supports_structured_output=True,
        supports_file_changes=False,
        supports_tool_calls=False,
        supports_streaming=False,
        max_context_tokens=4096,
        default_timeout_seconds=60,
        reason=reason,
    )


def discover_workers(settings: Settings) -> dict[str, WorkerSpec]:
    codex_availability, codex_version, codex_reason = _version("codex")
    claude_availability, claude_version, claude_reason = _version("claude")
    return {
        "deterministic-tool": WorkerSpec(
            worker_id="deterministic-tool",
            worker_type=WorkerType.DETERMINISTIC_TOOL,
            display_name="Deterministic Tool Worker",
            version="pados-local",
            availability=WorkerAvailability.AVAILABLE,
            capabilities=[
                CapabilityId.READ_REPOSITORY,
                CapabilityId.RUN_STATIC_ANALYSIS,
                CapabilityId.RUN_TESTS,
                CapabilityId.RUN_MIGRATIONS,
                CapabilityId.BUILD_RELEASE,
                CapabilityId.AUDIT_RELEASE,
                CapabilityId.CREATE_ARTIFACT,
            ],
            risk_ceiling=RiskLevel.MEDIUM,
            supports_structured_output=True,
            supports_file_changes=False,
            supports_tool_calls=True,
            supports_streaming=False,
            max_context_tokens=2000,
            default_timeout_seconds=120,
        ),
        "ollama-local": _ollama_spec(settings),
        "claude-code-runtime": WorkerSpec(
            worker_id="claude-code-runtime",
            worker_type=WorkerType.CLAUDE_CODE,
            display_name="Claude Code Runtime Worker",
            version=claude_version,
            availability=claude_availability,
            capabilities=[CapabilityId.READ_REPOSITORY, CapabilityId.MODIFY_MARKDOWN, CapabilityId.MODIFY_CODE],
            risk_ceiling=RiskLevel.MEDIUM,
            supports_structured_output=True,
            supports_file_changes=True,
            supports_tool_calls=True,
            supports_streaming=False,
            max_context_tokens=8000,
            default_timeout_seconds=300,
            reason=claude_reason,
        ),
        "codex-cli": WorkerSpec(
            worker_id="codex-cli",
            worker_type=WorkerType.CODEX_CLI,
            display_name="Codex CLI Worker",
            version=codex_version,
            availability=codex_availability,
            capabilities=[
                CapabilityId.READ_REPOSITORY,
                CapabilityId.MODIFY_MARKDOWN,
                CapabilityId.MODIFY_CODE,
                CapabilityId.RUN_TESTS,
                CapabilityId.AUDIT_RELEASE,
            ],
            risk_ceiling=RiskLevel.MEDIUM,
            supports_structured_output=True,
            supports_file_changes=True,
            supports_tool_calls=True,
            supports_streaming=False,
            max_context_tokens=8000,
            default_timeout_seconds=300,
            reason=codex_reason,
        ),
    }


def worker_health_payload(worker: WorkerSpec) -> dict[str, Any]:
    return {
        "worker_id": worker.worker_id,
        "available": worker.availability == WorkerAvailability.AVAILABLE,
        "availability": worker.availability.value,
        "executable_found": worker.availability != WorkerAvailability.UNAVAILABLE
        or worker.worker_type == WorkerType.DETERMINISTIC_TOOL,
        "version": worker.version,
        "authentication_state": "UNKNOWN" if worker.worker_type == WorkerType.CODEX_CLI else "NOT_REQUIRED",
        "endpoint_state": worker.reason or "ok",
        "capabilities": [cap.value for cap in worker.capabilities],
        "last_checked_at": datetime.now(UTC).isoformat(),
        "reason": worker.reason,
    }
