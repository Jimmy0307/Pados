"""Static authority boundaries of pados.machines (import graph, no shell, no routes)."""
from __future__ import annotations

import ast
from pathlib import Path

import pados.machines

PACKAGE = Path(pados.machines.__file__).resolve().parent
FORBIDDEN_IMPORTS = ("pados.herdr", "pados.memory", "pados.state_proof", "pados.api", "pados.cli",
                     "pados.control", "pados.operations", "fastapi", "paramiko")


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_machines_never_import_herdr_memory_semantic_state_or_api() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        bad = {name for name in imports(path) if name.startswith(FORBIDDEN_IMPORTS)}
        assert not bad, (path.name, bad)


def test_only_the_ssh_probe_spawns_processes_and_never_through_a_shell() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "shell=True" not in text and "os.system" not in text and "pty" not in imports(path)
        if "subprocess" in imports(path):
            assert path.name == "ssh.py"
    assert "shell=False" in (PACKAGE / "ssh.py").read_text(encoding="utf-8")


def test_no_api_route_is_added() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "APIRouter" not in text and ".get(\"/" not in text and ".post(\"/" not in text
