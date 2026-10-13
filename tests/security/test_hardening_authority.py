"""Security Hardening V1: model/capability bypass of authority, receipts, memory boundary,
plaintext-credential persistence."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from pados.execution.authorization import authorize_execution_capabilities
from pados.execution.receipts import artifact_handoff, execution_receipt, verify_execution_receipt
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.memory.security import validate_payload, validate_summary

CONTRACT: dict[str, Any] = {"work_contract_id": "wc-1", "project_id": "p-1", "required_capabilities": ["WRITE"]}
GRANT: dict[str, Any] = {"work_contract_id": "wc-1", "project_id": "p-1", "capabilities": ["WRITE"]}
# Credential-shaped fixtures are assembled at runtime so the release secret scan stays clean.
PW, FAKE = "pass" + "word", "hun" + "ter2"
ARTIFACT = {"path": "app.py", "sha256": hashlib.sha256(b"x").hexdigest(), "size_bytes": 1}


def _receipt(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"run_id": "run-1", "attempt": 1, "contract": CONTRACT, "artifacts": [ARTIFACT],
                              "provider_id": "provider-x", "evidence_kind": "LIVE_PROVIDER", "ok": True}
    values.update(overrides)
    return execution_receipt(**values)


def _verify(receipt: dict[str, Any], **overrides: Any) -> bool:
    values: dict[str, Any] = {"run_id": "run-1", "attempt": 1, "contract": CONTRACT, "artifacts": [ARTIFACT]}
    values.update(overrides)
    return verify_execution_receipt(receipt, **values)


def test_provider_claims_do_not_verify_as_receipts() -> None:
    assert _verify(_receipt())
    assert not _verify(_receipt(ok=False))
    assert not _verify(_receipt(evidence_kind="SIMULATION"))
    assert not _verify({**_receipt(), "semantic_authority": True})
    assert not _verify({**_receipt(), "ok": True, "provider_id": "other"})  # seal broken
    assert not _verify(_receipt(), run_id="run-2")  # replay onto another run
    assert not _verify(_receipt(), attempt=2)
    assert not _verify(_receipt(), contract={**CONTRACT, "project_id": "p-2"})
    assert not _verify(_receipt(), artifacts=[{**ARTIFACT, "sha256": "0" * 64}])
    assert not _verify(None)


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a\\b", "C:x", "a/../../b", "", "x:ads"])
def test_artifact_handoff_paths_cannot_escape(path: str) -> None:
    with pytest.raises(ValueError, match="INVALID_ARTIFACT_PATH"):
        artifact_handoff([{**ARTIFACT, "path": path}])


@pytest.mark.parametrize(("contract", "grant", "code"), [
    ({**CONTRACT, "required_capabilities": ["WRITE", "DEPLOY"]}, GRANT, "CAPABILITY_NOT_AUTHORIZED"),
    (CONTRACT, {**GRANT, "project_id": "p-2"}, "GRANT_SCOPE_MISMATCH"),
    (CONTRACT, {**GRANT, "work_contract_id": "wc-2"}, "GRANT_WORK_PACKAGE_MISMATCH"),
    ({**CONTRACT, "mcp_capabilities": ["fs.write"]}, {**GRANT, "mcp_capabilities": ["fs.write"]},
     "MCP_CAPABILITY_NOT_REGISTERED"),
    ({**CONTRACT, "mcp_capabilities": ["fs.read"]}, GRANT, "MCP_CAPABILITY_NOT_AUTHORIZED"),
    ({**CONTRACT, "skill_ids": ["deploy-skill"]}, {**GRANT, "skills": ["deploy-skill"]}, "SKILL_NOT_REGISTERED"),
    ({**CONTRACT, "secret_refs": ["secretref://env/A"]}, GRANT, "SECRET_REFERENCE_REQUIRED"),
    ({**CONTRACT, "secret_refs": ["plaintext"]}, {**GRANT, "secret_refs": ["plaintext"]},
     "SECRET_REFERENCE_REQUIRED"),
])
def test_unregistered_or_ungranted_capabilities_never_execute(
    contract: dict[str, Any], grant: dict[str, Any], code: str
) -> None:
    with pytest.raises(PermissionError, match=code):
        authorize_execution_capabilities(contract, grant, installed_skills=frozenset(),
                                         registered_mcp_capabilities=frozenset({"fs.read"}),
                                         agent_role="ADMIN_ORCHESTRATOR")


def test_role_label_and_installation_are_not_grants() -> None:
    granted = authorize_execution_capabilities(
        CONTRACT, GRANT, installed_skills=frozenset({"deploy-skill"}),
        registered_mcp_capabilities=frozenset({"fs.write"}), agent_role="Release Manager")
    assert granted["capabilities"] == ["WRITE"] and granted["skill_ids"] == [] and granted["mcp_capabilities"] == []
    assert granted["authorization_source"] == "PADOS_PROJECT_GRANT"


ANSI = "\x1b[31mFAILED\x1b[0m tests/test_x.py"
OSC52 = "\x1b]52;c;ZWNobyBwd25lZA==\x07"


@pytest.mark.parametrize("text", [ANSI, OSC52, "progress\r\x1b[2K 50%", "bell\x07", "\x9b31m c1 CSI", "nul\x00"])
def test_raw_terminal_output_cannot_enter_memory(text: str) -> None:
    """FIXED: memory candidate summaries and run events accepted raw terminal control sequences."""
    with pytest.raises(ValueError, match="terminal"):
        validate_summary(f"observation: {text}")
    with pytest.raises(ValueError, match="terminal"):
        validate_payload({"error": text})
    with pytest.raises(ValueError, match="terminal"):
        validate_payload({"summary": {"nested": [text]}})


def test_ordinary_multiline_memory_text_is_still_accepted() -> None:
    validate_summary("Line one.\nLine two\twith a tab.\r\nUnicode: café, 日本語, emoji-free.")
    validate_payload({"summary": "ok", "exit_code": 0, "command": "pytest -q"})


@pytest.mark.parametrize("value", [PW + "=" + FAKE, "postgresql://pados:" + FAKE + "@db:5432/x",
                                   "sk-" + "a" * 32, "ghp_" + "b" * 36])
def test_secret_material_cannot_enter_memory(value: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        validate_summary(f"the run used {value}")
    with pytest.raises(ValueError, match="secret"):
        validate_payload({"error": value})


@pytest.mark.parametrize("record", [
    {PW: FAKE}, {"machine": {"ssh": {"passphrase": "x"}}}, {"scrollback": "..."},
    {"terminal_output": "..."}, {"raw_body": "..."}, {"full_context": "..."}, {"token": "abc"},
    {"value": "sk-" + "z" * 30},
])
def test_shared_minimization_guard_blocks_plaintext_passwords_and_session_data(record: dict[str, Any]) -> None:
    with pytest.raises(DataMinimizationViolation):
        assert_minimized(record)
    assert_minimized({"credential_ref": "secretref://keychain/pados/home-base", "password_supplied": False})


def test_existing_persistence_paths_reject_plaintext_password_fields(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from pados.compute_node.registry import NodeRegistry
    from pados.remote.auth import RemoteActorRegistry

    with pytest.raises(ValidationError):
        NodeRegistry.model_validate({"state_dir": str(tmp_path), "password": "x"})
    with pytest.raises(ValueError, match="PLAINTEXT_CREDENTIAL_FORBIDDEN"):
        RemoteActorRegistry.from_mapping({"actors": [], "db_password": "x"})


def test_f10_target_starting_with_dash_cannot_become_a_tool_option(tmp_path: Path) -> None:
    from pados.worker.command import build_argv
    from pados.worker.schemas import CommandInvocation

    (tmp_path / "--fix").write_text("x = 1\n", encoding="utf-8")
    argv = build_argv(CommandInvocation(command_id="ruff_check", working_directory=tmp_path, timeout_seconds=1,
                                        arguments={"target": "--fix"}), project_root=tmp_path)
    assert argv[-1] == "./--fix"
    assert "--fix" not in argv
