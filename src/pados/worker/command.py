# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pados.memory.security import assert_no_secret
from pados.worker.schemas import CommandInvocation, ErrorCode

SAFE_ENV = {"PYTHONPATH", "PADOS_RUNTIME_MODE", "PADOS_LOG_LEVEL"}
SECRET_ENV_WORDS = ("PASSWORD", "TOKEN", "SECRET", "KEY", "COOKIE")


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    executable: str
    arguments: list[str]
    allowed_args: set[str]
    timeout: int
    risk_level: str


COMMANDS = {
    "python_version": CommandSpec(
        command_id="python_version",
        executable="python",
        arguments=["--version"],
        allowed_args=set(),
        timeout=15,
        risk_level="LOW",
    ),
    "ruff_check": CommandSpec(
        command_id="ruff_check",
        executable="python",
        arguments=["-m", "ruff", "check"],
        allowed_args={"target"},
        timeout=120,
        risk_level="LOW",
    ),
    "pytest_targeted": CommandSpec(
        command_id="pytest_targeted",
        executable="python",
        arguments=["-m", "pytest"],
        allowed_args={"target"},
        timeout=180,
        risk_level="LOW",
    ),
}


def _resolve_under(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError("working directory outside allowed project root")
    return resolved


def _redact(value: str) -> str:
    try:
        assert_no_secret(value, field_name="process_output")
    except ValueError:
        return "[REDACTED]"
    return value


def build_argv(invocation: CommandInvocation, *, project_root: Path) -> list[str]:
    if any(ch in invocation.command_id for ch in [";", "&", "|", "`", "$("]):
        raise PermissionError("arbitrary shell command rejected")
    spec = COMMANDS.get(invocation.command_id)
    if spec is None:
        raise PermissionError("unknown command rejected")
    unknown = set(invocation.arguments) - spec.allowed_args
    if unknown:
        raise ValueError(f"argument schema validation failed: {sorted(unknown)}")
    executable = sys.executable if spec.executable == "python" else spec.executable
    argv = [executable, *spec.arguments]
    target = invocation.arguments.get("target")
    if target:
        if any(ch in target for ch in [";", "&", "|", "`", "$("]):
            raise PermissionError("argument injection rejected")
        target_path = _resolve_under(project_root, project_root / target)
        relative = target_path.relative_to(project_root).as_posix()
        # A path such as "--fix" would otherwise be parsed as an option by the tool.
        argv.append(f"./{relative}" if relative.startswith("-") else relative)
    return argv


def allowed_environment(env: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in env.items():
        if key not in SAFE_ENV:
            continue
        if any(word in key.upper() for word in SECRET_ENV_WORDS):
            continue
        result[key] = value
    return result


def run_registered_command(invocation: CommandInvocation, *, project_root: Path) -> dict[str, object]:
    working_directory = _resolve_under(project_root, invocation.working_directory)
    argv = build_argv(invocation, project_root=project_root)
    env = allowed_environment(invocation.environment)
    process_env = {**os.environ, **env}
    for key in list(process_env):
        if any(word in key.upper() for word in SECRET_ENV_WORDS):
            process_env.pop(key, None)
    try:
        completed = subprocess.run(
            argv,
            cwd=working_directory,
            env=process_env,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=invocation.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "status": "TIMED_OUT",
            "exit_code": None,
            "stdout": _redact(stdout[:10_000]),
            "stderr": _redact(stderr[:10_000]),
            "error_code": ErrorCode.TIMED_OUT.value,
        }
    return {
        "status": "SUCCEEDED" if completed.returncode == 0 else "FAILED",
        "exit_code": completed.returncode,
        "stdout": _redact(completed.stdout[:10_000]),
        "stderr": _redact(completed.stderr[:10_000]),
        "error_code": None if completed.returncode == 0 else ErrorCode.VALIDATION_FAILED.value,
    }
