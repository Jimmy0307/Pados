"""Requirement 8 (static, AST): the Phase G modules import only the capability fabric, capability packs,
resources and the allowlisted read-only helpers; they start no process, socket or thread, never call an
adapter or the stage boundary themselves, never use the OCR pack's own runner or fixture adapter, and Core
never imports them."""
from __future__ import annotations

import ast
from pathlib import Path

import pados

SRC = Path(pados.__file__).resolve().parent
NEW_PACKAGES = ("extraction", "retrieval", "intelligence")
MODULES = sorted(path for package in NEW_PACKAGES for path in (SRC / "packs" / package).rglob("*.py"))
ALLOWED_PADOS = ("pados.capability", "pados.packs", "pados.resources")
ALLOWED_HELPERS = frozenset({
    "pados.compute_node.manifest", "pados.connectivity.requirements", "pados.connectivity.state",
    "pados.connectivity.storage", "pados.enterprise.classification", "pados.enterprise.compute",
    "pados.enterprise.lineage", "pados.enterprise.secrets", "pados.execution.authorization",
    "pados.execution.context", "pados.lifecycle.fswalk", "pados.lifecycle.locking", "pados.lifecycle.minimization",
    "pados.lifecycle.retention", "pados.state_proof.object_store", "pados.worker.schemas",
})
FORBIDDEN_PACK_MODULES = ("pados.packs.ocr.pipeline", "pados.packs.ocr.fixture", "pados.packs.ocr.engines",
                          "pados.capability.inference", "pados.learning", "pados.training")
SIDE_EFFECT_MODULES = frozenset({"subprocess", "socket", "threading", "multiprocessing", "asyncio", "concurrent",
                                 "sched", "http", "urllib", "requests", "httpx", "ctypes", "signal", "shutil",
                                 "tempfile", "os"})
STAGE_BOUNDARY = frozenset({"bind_stage", "run_bound_stage", "guarded"})
# CR-16b H-08: the atomic provenance registrar's single lock (a Lock object; it starts no thread).
LOCK_ONLY = {SRC / "packs" / "intelligence" / "provenance.py": "threading"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.level == 0, path
            names.add(node.module)
    return names


def test_the_new_modules_exist() -> None:
    assert {path.parent.name for path in MODULES} == set(NEW_PACKAGES) and len(MODULES) >= 9


def test_imports_are_limited_to_the_fabric_packs_resources_and_allowlisted_helpers() -> None:
    for path in MODULES:
        for name in _imports(path):
            if name.startswith("pados"):
                assert name.startswith(ALLOWED_PADOS) or name in ALLOWED_HELPERS, (path.name, name)
                assert not name.startswith(FORBIDDEN_PACK_MODULES), (path.name, name)
            elif LOCK_ONLY.get(path) != name:
                assert name.split(".")[0] not in SIDE_EFFECT_MODULES, (path.name, name)


def test_the_provenance_registrar_uses_threading_only_for_its_lock() -> None:
    for path, module in LOCK_ONLY.items():
        used = {node.attr for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == module}
        assert used == {"Lock"}, (path.name, used)


def test_no_process_network_thread_or_dynamic_code() -> None:
    for path in MODULES:
        text = path.read_text(encoding="utf-8")
        for token in ("eval(", "exec(", "os.system", "__import__", "importlib", "open(", "Popen", "socket"):
            assert token not in text, (path.name, token)


def test_no_module_invokes_an_adapter_or_the_stage_boundary_itself() -> None:
    """Only the fabric executor binds and runs stages; these modules define adapters but never call one."""
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                assert name not in {"execute", "run_pipeline", *STAGE_BOUNDARY}, (path.name, name, node.lineno)
            if isinstance(node, ast.Name):
                assert node.id not in STAGE_BOUNDARY | {"run_pipeline"}, (path.name, node.id)


def test_core_never_imports_the_new_packs() -> None:
    additive = ("capability", "packs", "learning", "training", "resources")
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if len(relative.parts) > 1 and relative.parts[0] in additive:
            continue
        bad = {name for name in _imports_any(path)
               if name.startswith(tuple(f"pados.packs.{package}" for package in NEW_PACKAGES))}
        assert not bad, (str(relative), bad)


def _imports_any(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names
