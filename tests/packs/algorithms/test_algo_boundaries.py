"""Static boundaries: the algorithm pack is additive and imports no authority, router or I/O modules."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "src" / "pados" / "packs" / "algorithms"
ALLOWED_PADOS = (
    "pados.capability.adapter", "pados.capability.artifacts", "pados.capability.model",
    "pados.capability.receipts", "pados.lifecycle.minimization", "pados.execution.context",
    "pados.enterprise.classification", "pados.connectivity.requirements", "pados.packs.algorithms",
)
FORBIDDEN_STDLIB = {"socket", "subprocess", "threading", "multiprocessing", "asyncio", "sched", "urllib", "http",
                    "ftplib", "smtplib", "ssl", "os", "shutil", "pickle", "ctypes", "importlib", "sqlite3"}
FORBIDDEN_THIRD_PARTY = {"httpx", "requests", "sqlalchemy", "numpy", "psycopg", "fastapi"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_packs_package_docstring_is_exact() -> None:
    assert (ROOT / "src" / "pados" / "packs" / "__init__.py").read_text(encoding="utf-8") == \
        '# SPDX-License-Identifier: AGPL-3.0-only\n"""PADOS capability packs (additive; never Core)."""\n'


def test_pack_imports_only_the_capability_contract_and_pure_helpers() -> None:
    modules = sorted(PACK.glob("*.py"))
    assert modules
    for module in modules:
        for name in _imports(module):
            root = name.split(".")[0]
            if root == "pados":
                assert name.startswith(ALLOWED_PADOS), f"{module.name} imports {name}"
            assert root not in FORBIDDEN_STDLIB, f"{module.name} imports {name}"
            assert root not in FORBIDDEN_THIRD_PARTY, f"{module.name} imports {name}"


def test_pack_never_names_authority_writers() -> None:
    forbidden = ("state_proof.service", "state_proof.repository", "product_loop", "pados.memory", "pados.audit",
                 "worker.router", "select_worker", "compare_and_swap", "ScopeVersion(")
    for module in PACK.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), module.name
