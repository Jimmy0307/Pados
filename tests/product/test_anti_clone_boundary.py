"""Product differentiation gate: PADOS is an execution authority, not an AI chat workspace.

Static and behavioural checks that the remote/mobile surface stays an
interface (intent in, projections out) and that acceptance stays behind
receipt -> validation -> CAS.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from pados.remote.intent import FORBIDDEN_CONTEXT_KEYS

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "src" / "pados" / "remote"
SANDBOX = ROOT / "src" / "pados" / "sandbox"
WEB_SRC = ROOT / "web" / "src"


def _imports(package: Path) -> set[str]:
    found: set[str] = set()
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
    return found


def test_remote_and_sandbox_never_call_model_providers_directly() -> None:
    for package in (REMOTE, SANDBOX):
        imported = _imports(package)
        banned = {m for m in imported if m.startswith(("pados.providers", "pados.worker", "pados.agents",
                                                        "openai", "anthropic", "ollama", "httpx", "requests"))}
        assert banned == set(), (package.name, banned)


def test_remote_routes_have_no_chat_completion_or_passthrough_surface() -> None:
    routes = (REMOTE / "routes.py").read_text(encoding="utf-8")
    paths = re.findall(r'f?"(\{PREFIX\}[^"]*|/remote[^"]*)"', routes)
    assert paths, "expected remote route declarations"
    for path in paths:
        for word in ("chat", "completion", "prompt", "generate", "shell", "exec", "sql", "proxy", "model"):
            assert word not in path.lower(), path


def test_remote_package_never_writes_semantic_tables_directly() -> None:
    for path in REMOTE.glob("*.py"):
        text = path.read_text(encoding="utf-8").upper()
        for statement in ("INSERT INTO STATE_PROOF", "UPDATE STATE_PROOF", "INSERT INTO SCOPE",
                          "UPDATE SCOPE", "DELETE FROM", "INSERT INTO MEMORY", "UPDATE MEMORY"):
            assert statement not in text, (path.name, statement)


def test_acceptance_only_through_cas_commit_result() -> None:
    live = (REMOTE / "live.py").read_text(encoding="utf-8")
    assert "commit_result(" in live and "expected_version=expected_version" in live
    approval = (REMOTE / "approval.py").read_text(encoding="utf-8")
    assert "SCOPE_VERSION_CHANGED" in approval


def test_chat_history_is_not_memory() -> None:
    for key in ("chat_history", "history", "messages", "conversation", "memory", "provider_history"):
        assert key in FORBIDDEN_CONTEXT_KEYS


def test_web_console_does_not_persist_conversation_as_memory() -> None:
    if not WEB_SRC.exists():
        return
    for path in WEB_SRC.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"localStorage\.setItem\([^)]*(history|conversation|messages|memory)", text, re.IGNORECASE), path
