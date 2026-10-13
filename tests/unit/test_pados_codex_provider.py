import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from pados.execution.receipts import verify_execution_receipt
from pados.product_loop.codex_provider import CodexCliMaker
from pados.product_loop.schemas import (
    AcceptanceCriterion,
    CheckKind,
    ContractState,
    FileChange,
    Intent,
    RetryPolicy,
    ScopeSpec,
    ValidationOperation,
    WorkContract,
)
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType


def contract(root: Path, **updates) -> WorkContract:
    values = {
        "work_contract_id": "contract-1",
        "version": 1,
        "parent_goal": "Write a bounded output",
        "intent": Intent(
            intent_id="intent-1", original_request="write output",
            objective="write output", scope_id="project-1",
        ),
        "decision_ref": "decision-1",
        "objective": "Write result.py with result = 42",
        "scope": ScopeSpec(
            workspace_root=str(root), allowed_paths=("result.py",), isolated=True,
        ),
        "constraints": ("DECLARED_PATHS_ONLY", "NO_MERGE_PUSH_DEPLOY"),
        "context_ref": "pados://context/one",
        "context_fingerprint": "a" * 64,
        "context_snapshot": {
            "instructions_are_data_not_permission": True,
            "secret_refs": ["secretref://provider/repo-token"],
        },
        "semantic_base_scope_version": 0,
        "required_capabilities": ("MAKE",),
        "allowed_actions": ("WRITE_APPROVED_WORKSPACE",),
        "prohibited_actions": ("MERGE", "PUSH", "DEPLOY"),
        "side_effect_policy": "ISOLATED_WORKSPACE_ONLY",
        "implementation_class": "CODEX_CLI",
        "runtime_policy": "BOUNDED_CODEX_EXEC",
        "changes": (FileChange(path="result.py", content="result = 42\n"),),
        "acceptance_criteria": (
            AcceptanceCriterion(
                criterion_id="compile", operation=ValidationOperation.TEST,
                check=CheckKind.PYTHON_COMPILE, target_path="result.py",
            ),
        ),
        "validation_policy": "READ_ONLY_LOCAL_VALIDATION",
        "retry_policy": RetryPolicy(max_attempts=1),
        "idempotency_key": "request-1",
        "correlation_id": "run-1",
    }
    values.update(updates)
    return WorkContract(**values)


def codex_worker(availability=WorkerAvailability.AVAILABLE) -> WorkerSpec:
    return WorkerSpec(
        worker_id="codex-cli", worker_type=WorkerType.CODEX_CLI,
        display_name="Codex CLI", version="codex-cli-test",
        availability=availability, capabilities=[CapabilityId.MODIFY_CODE],
        risk_ceiling=RiskLevel.MEDIUM, supports_structured_output=True,
        supports_file_changes=True, supports_tool_calls=True,
        supports_streaming=False, max_context_tokens=8000,
        default_timeout_seconds=30,
    )


def test_codex_exec_runs_in_allowlisted_workspace_and_returns_live_receipt(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        Path(kwargs["cwd"], "result.py").write_text("result = 42\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    work = contract(tmp_path)
    maker = CodexCliMaker(
        tmp_path, settings=object(), runner=runner,
        worker_discovery=lambda _settings: {"codex-cli": codex_worker()},
        executable_resolver=lambda: "/usr/bin/codex",
    )
    result = asyncio.run(maker.make(work, run_id="run-1", attempt=1))
    assert result.status.value == "COMPLETED"
    assert (tmp_path / "result.py").read_text() == "result = 42\n"
    argv, kwargs = calls[0]
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert Path(kwargs["cwd"]) != tmp_path
    assert kwargs["cwd"].is_relative_to(tmp_path)
    assert "secret_refs" not in kwargs["input"]
    assert "secretref://" not in kwargs["input"]
    assert result.execution_receipt["evidence_kind"] == "LIVE_PROVIDER"
    assert verify_execution_receipt(
        result.execution_receipt, run_id="run-1", attempt=1,
        contract=work.model_dump(mode="json"), artifacts=result.artifact_refs,
    )


def test_unavailable_codex_is_blocked_without_fallback_or_execution(tmp_path):
    calls = []
    maker = CodexCliMaker(
        tmp_path, settings=object(), runner=lambda *args, **kwargs: calls.append(args),
        worker_discovery=lambda _settings: {"codex-cli": codex_worker(WorkerAvailability.UNAVAILABLE)},
        executable_resolver=lambda: "/usr/bin/codex",
    )
    result = asyncio.run(maker.make(contract(tmp_path), run_id="run-1", attempt=1))
    assert result.status.value == "BLOCKED"
    assert result.failure_classification.value == "AUTHORITY_BLOCK"
    assert calls == []


def test_unapproved_work_contract_is_blocked_before_provider_start(tmp_path):
    calls = []
    maker = CodexCliMaker(
        tmp_path, settings=object(), runner=lambda *args, **kwargs: calls.append(args),
        worker_discovery=lambda _settings: {"codex-cli": codex_worker()},
        executable_resolver=lambda: "/usr/bin/codex",
    )
    work = contract(tmp_path).model_copy(update={"state": ContractState.READY})
    result = asyncio.run(maker.make(work, run_id="run-1", attempt=1))
    assert result.status.value == "BLOCKED"
    assert calls == []


def test_codex_cannot_export_unallowlisted_files_to_registered_workspace(tmp_path):
    def runner(_argv, **kwargs):
        Path(kwargs["cwd"], "result.py").write_text("result = 42\n")
        Path(kwargs["cwd"], "outside.py").write_text("unexpected = True\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    maker = CodexCliMaker(
        tmp_path, settings=object(), runner=runner,
        worker_discovery=lambda _settings: {"codex-cli": codex_worker()},
        executable_resolver=lambda: "/usr/bin/codex",
    )
    result = asyncio.run(maker.make(contract(tmp_path), run_id="run-1", attempt=1))
    assert result.status.value == "BLOCKED"
    assert not (tmp_path / "result.py").exists()
    assert not (tmp_path / "outside.py").exists()


def test_provider_io_is_decoded_as_utf8_and_no_write_is_diagnosed(tmp_path):
    """Windows regression: a locale code page must not decode Codex's UTF-8 stream; a
    provider that performs no write fails closed with an exact, content-free diagnostic."""
    calls = []
    stdout = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "session is read-only"}},
    ))

    def runner(argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    maker = CodexCliMaker(
        tmp_path, settings=object(), runner=runner,
        worker_discovery=lambda _settings: {"codex-cli": codex_worker()},
        executable_resolver=lambda: "/usr/bin/codex",
    )
    result = asyncio.run(maker.make(contract(tmp_path), run_id="run-1", attempt=1))
    assert calls[0]["encoding"] == "utf-8" and calls[0]["errors"] == "replace"
    assert result.status.value == "BLOCKED"
    code, payload = result.summary.split(" ", 1)
    diagnostic = json.loads(payload)
    assert code == "CODEX_OUTPUT_PATH_SCOPE_MISMATCH"
    assert diagnostic["actual_sandbox_files"] == [] and diagnostic["unexpected_paths"] == []
    assert diagnostic["expected_but_missing"] == diagnostic["declared_output_paths"]
    assert diagnostic["provider_attempted_writes"] is False
    assert diagnostic["provider_event_item_types"] == {"thread.started": 1, "agent_message": 1}
    assert "read-only" not in payload  # provider text is never recorded
