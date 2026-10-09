import sys
from pathlib import Path

import pytest

from pados.config import Settings
from pados.worker.command import build_argv, run_registered_command
from pados.worker.handoff import build_handoff
from pados.worker.registry import capability_registry, discover_workers
from pados.worker.router import select_worker
from pados.worker.schemas import (
    CapabilityId,
    CommandInvocation,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)


def _worker(
    worker_id: str,
    worker_type: WorkerType,
    *,
    available: bool,
    capabilities: list[CapabilityId],
    risk: RiskLevel = RiskLevel.LOW,
) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id,
        worker_type=worker_type,
        display_name=worker_id,
        availability=WorkerAvailability.AVAILABLE if available else WorkerAvailability.UNAVAILABLE,
        capabilities=capabilities,
        risk_ceiling=risk,
        supports_structured_output=True,
        supports_file_changes=False,
        supports_tool_calls=True,
        supports_streaming=False,
        max_context_tokens=2000,
        default_timeout_seconds=30,
    )


def test_capability_registry_contains_allowed_and_excludes_denied() -> None:
    registry = capability_registry()

    assert CapabilityId.RUN_TESTS in registry
    assert CapabilityId.MODIFY_CODE in registry
    assert all(cap.value not in {"DEPLOY", "SEND_EMAIL", "RELEASE_PRODUCTION"} for cap in registry)


def test_router_prefers_deterministic_worker_when_capable() -> None:
    workers = {
        "codex": _worker("codex", WorkerType.CODEX_CLI, available=True, capabilities=[CapabilityId.RUN_TESTS]),
        "det": _worker("det", WorkerType.DETERMINISTIC_TOOL, available=True, capabilities=[CapabilityId.RUN_TESTS]),
    }

    decision = select_worker(workers, capability_id=CapabilityId.RUN_TESTS, risk_level=RiskLevel.LOW)

    assert decision.selected_worker_id == "det"
    assert decision.error_code is None
    assert "deterministic priority" in decision.explanation


def test_router_fails_closed_when_worker_unavailable() -> None:
    workers = {
        "ollama": _worker("ollama", WorkerType.OLLAMA, available=False, capabilities=[CapabilityId.SUMMARIZE]),
    }

    decision = select_worker(workers, capability_id=CapabilityId.SUMMARIZE, risk_level=RiskLevel.LOW)

    assert decision.selected_worker_id is None
    assert decision.error_code == "WORKER_UNAVAILABLE"


def test_router_enforces_risk_ceiling() -> None:
    workers = {
        "det": _worker(
            "det",
            WorkerType.DETERMINISTIC_TOOL,
            available=True,
            capabilities=[CapabilityId.RUN_TESTS],
            risk=RiskLevel.LOW,
        )
    }

    decision = select_worker(workers, capability_id=CapabilityId.RUN_TESTS, risk_level=RiskLevel.HIGH)

    assert decision.selected_worker_id is None
    assert decision.error_code == "NO_ELIGIBLE_WORKER"
    assert decision.considered[0]["risk_compatible"] is False


def test_command_registry_rejects_arbitrary_shell_and_unknown_command(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="arbitrary shell"):
        build_argv(
            CommandInvocation(
                command_id="pytest; rm -rf .",
                working_directory=tmp_path,
                timeout_seconds=1,
            ),
            project_root=tmp_path,
        )
    with pytest.raises(PermissionError, match="unknown command"):
        build_argv(
            CommandInvocation(command_id="whoami", working_directory=tmp_path, timeout_seconds=1),
            project_root=tmp_path,
        )


def test_command_registry_validates_arguments_and_paths(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "sample.py").write_text("", encoding="utf-8")

    argv = build_argv(
        CommandInvocation(
            command_id="pytest_targeted",
            arguments={"target": "tests/sample.py"},
            working_directory=tmp_path,
            timeout_seconds=1,
        ),
        project_root=tmp_path,
    )

    assert argv[-1] == "tests/sample.py"
    with pytest.raises(PermissionError, match="argument injection"):
        build_argv(
            CommandInvocation(
                command_id="pytest_targeted",
                arguments={"target": "tests/sample.py; whoami"},
                working_directory=tmp_path,
                timeout_seconds=1,
            ),
            project_root=tmp_path,
        )
    with pytest.raises(PermissionError, match="outside allowed"):
        build_argv(
            CommandInvocation(
                command_id="pytest_targeted",
                arguments={"target": "../escape.py"},
                working_directory=tmp_path,
                timeout_seconds=1,
            ),
            project_root=tmp_path,
        )


def test_registered_python_commands_use_the_active_runtime(tmp_path: Path) -> None:
    argv = build_argv(
        CommandInvocation(
            command_id="python_version",
            working_directory=tmp_path,
            timeout_seconds=1,
        ),
        project_root=tmp_path,
    )

    assert argv == [sys.executable, "--version"]


def test_registered_command_redacts_secret_like_output(tmp_path: Path) -> None:
    result = run_registered_command(
        CommandInvocation(
            command_id="python_version",
            working_directory=tmp_path,
            environment={"PADOS_DB_PASSWORD": "should-not-forward", "PADOS_RUNTIME_MODE": "native"},
            timeout_seconds=5,
        ),
        project_root=tmp_path,
    )

    assert result["status"] == "SUCCEEDED"
    assert "should-not-forward" not in str(result)


def test_handoff_packet_hash_and_secret_scan() -> None:
    context_packet = {
        "context_packet": {
            "packet_id": "packet-1",
            "estimated_tokens": 10,
            "verified_facts": [{"title": "Fact", "summary": "Safe"}],
            "locked_decisions": [],
            "known_failures": [],
            "relevant_artifacts": [],
            "provenance": {"policy_version": "test"},
        }
    }

    handoff = build_handoff(
        job_id="job-1",
        project_id="project-1",
        request={"title": "Read status"},
        context_packet=context_packet,
        allowed_actions=["READ_REPOSITORY"],
        prohibited_actions=["push"],
        allowed_paths=["."],
        validation_commands=["python_version"],
    )

    assert len(handoff.packet_hash) == 64
    assert handoff.token_estimate == 10


def test_handoff_rejects_secret_context() -> None:
    secret_like = "password" + "=secret-value"
    with pytest.raises(ValueError, match="secret-like"):
        build_handoff(
            job_id="job-1",
            project_id="project-1",
            request={"title": "Bad"},
            context_packet={
                "context_packet": {
                    "packet_id": "packet-1",
                    "estimated_tokens": 1,
                    "verified_facts": [{"summary": secret_like}],
                    "locked_decisions": [],
                    "known_failures": [],
                    "relevant_artifacts": [],
                    "provenance": {},
                }
            },
            allowed_actions=[],
            prohibited_actions=[],
            allowed_paths=[],
            validation_commands=[],
        )


def test_worker_discovery_has_no_mock_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: None)

    workers = discover_workers(Settings())

    assert "mock" not in {worker.worker_id.lower() for worker in workers.values()}
    assert workers["deterministic-tool"].availability == WorkerAvailability.AVAILABLE
    assert workers["codex-cli"].availability == WorkerAvailability.UNAVAILABLE
