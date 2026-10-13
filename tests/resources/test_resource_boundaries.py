"""Static boundaries: no authority imports, no probing modules, no locations or commands in sources."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "pados" / "resources"
SOURCES = sorted(PACKAGE.glob("*.py"))
ALLOWED_PADOS = (
    "pados.capability.model", "pados.capability.seams", "pados.capability.policy",
    "pados.compute_node.manifest", "pados.enterprise.compute", "pados.enterprise.classification",
    "pados.connectivity.requirements", "pados.execution.context", "pados.lifecycle.minimization",
    "pados.resources",
)
ALLOWED_STDLIB = {"__future__", "re", "datetime", "enum", "dataclasses", "collections.abc", "typing"}
ALLOWED_THIRD_PARTY = {"pydantic"}
FORBIDDEN = ("pados.state_proof", "pados.product_loop", "pados.memory", "pados.worker.router", "pados.db",
             "pados.compute_node.registry", "pados.compute_node.provider", "pados.compute_node.service",
             "socket", "subprocess", "httpx", "requests", "urllib", "platform", "psutil", "os", "ctypes")


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_package_has_the_expected_modules() -> None:
    # Stage B M3 adds capability_profile.py (measured, descriptive profiles); every rule below covers it too.
    assert {path.name for path in SOURCES} == {"__init__.py", "hardware.py", "models.py", "inventory.py",
                                               "planning.py", "capability_profile.py"}


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_imports_stay_inside_the_allowlist(path: Path) -> None:
    for module in imported_modules(path):
        assert not any(module == bad or module.startswith(bad + ".") for bad in FORBIDDEN), module
        if module.startswith("pados."):
            assert any(module == ok or module.startswith(ok + ".") for ok in ALLOWED_PADOS), module
        else:
            assert module in ALLOWED_STDLIB | ALLOWED_THIRD_PARTY, module


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_sources_hold_no_locations_or_commands(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"https?://|\b\d{1,3}(\.\d{1,3}){3}\b|/home/|[A-Za-z]:\\\\", text)
    assert "select_worker(" not in text and "eval(" not in text and "exec(" not in text


def test_whole_package_imports_without_side_effects() -> None:
    import importlib

    for name in ("hardware", "models", "inventory", "planning", "capability_profile"):
        module = importlib.import_module(f"pados.resources.{name}")
        assert "authori" in (module.__doc__ or "").lower()  # every module states its authority boundary
