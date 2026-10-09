"""Static boundaries: thin Core, no second router, no authority writers, metadata never interpreted."""
from __future__ import annotations

import ast
from pathlib import Path

import pados
import pados.capability

SRC = Path(pados.__file__).resolve().parent
FABRIC = Path(pados.capability.__file__).resolve().parent
FABRIC_V1 = ("registry.py", "graph.py", "task.py", "planner.py", "executor.py", "verification.py", "inference.py",
             "provenance.py", "human_decision.py", "decision_ledger.py", "execution_ledger.py", "operator_decisions.py",
             "operator_cli.py")
CORE_PACKAGES = ("product_loop", "state_proof", "memory", "worker", "remote", "execution", "compute_node",
                 "connectivity", "enterprise")
AUTHORITY_WRITERS = ("pados.state_proof.service", "pados.state_proof.repository", "pados.state_proof.audit_chain",
                     "pados.product_loop.state", "pados.product_loop.service", "pados.memory", "pados.db",
                     "pados.core.scheduler", "pados.core.resource_scheduler", "pados.core.router",
                     "pados.core.task_service", "pados.execution.semantic_context", "pados.execution.handoff")
PROVIDER_CODE = ("pados.worker", "pados.providers", "pados.compute_node", "pados.enterprise.compute",
                 "pados.product_loop", "pados.capability.inference")
RUNTIME_SIDE_EFFECTS = ("subprocess", "socket", "threading", "sched", "asyncio", "httpx", "requests", "urllib",
                        "multiprocessing", "concurrent")


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def names_used(path: Path) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def test_core_remains_thin() -> None:
    """No Core package imports the Capability Fabric or capability packs (CORE_REMAINS_THIN)."""
    for package in CORE_PACKAGES:
        for path in sorted((SRC / package).rglob("*.py")):
            bad = {name for name in imports(path) if name.startswith(("pados.capability", "pados.packs"))}
            assert not bad, (path.relative_to(SRC).as_posix(), bad)


def test_fabric_never_imports_authority_writers() -> None:
    for name in FABRIC_V1:
        bad = {item for item in imports(FABRIC / name) if item.startswith(AUTHORITY_WRITERS)}
        assert not bad, (name, bad)


def test_no_second_router_only_the_bridge_touches_select_worker() -> None:
    for path in sorted(FABRIC.glob("*.py")):
        touches = "pados.worker.router" in imports(path) or "select_worker" in names_used(path)
        assert touches == (path.name == "inference.py"), path.name
    for name in ("planner.py", "executor.py", "registry.py", "graph.py", "task.py", "verification.py",
                 "provenance.py", "human_decision.py"):
        bad = {item for item in imports(FABRIC / name) if item.startswith(PROVIDER_CODE)}
        assert not bad, (name, bad)


def test_fabric_starts_no_processes_threads_or_network() -> None:
    for name in FABRIC_V1:
        text = (FABRIC / name).read_text(encoding="utf-8")
        found = {item for item in imports(FABRIC / name) if item.split(".")[0] in RUNTIME_SIDE_EFFECTS}
        allowed = {"threading"} if name == "registry.py" else set()  # the CF-03 dispatch RLock only
        assert not found - allowed, (name, found)
        assert "eval(" not in text and "exec(" not in text and "os.system" not in text and "__import__" not in text


def test_descriptor_display_metadata_is_never_interpreted() -> None:
    """Operation ids and descriptions are catalog text: planner/executor/registry never read them."""
    for name in ("planner.py", "executor.py", "registry.py", "graph.py", "verification.py", "provenance.py",
                 "human_decision.py"):
        used = names_used(FABRIC / name)
        assert not {"operation", "description"} & used, name


def test_registry_has_no_authorization_state() -> None:
    tree = ast.parse((FABRIC / "registry.py").read_text(encoding="utf-8"))
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not {attr for attr in attrs if "grant" in attr.lower() or "authoriz" in attr.lower()}


def test_fabric_never_imports_the_frozen_approval_or_training_decision_paths() -> None:
    """CF-04 is capability-local: no import of pados.release.approval or pados.training.*."""
    for path in sorted(FABRIC.glob("*.py")):
        bad = {item for item in imports(path) if item.startswith(("pados.release", "pados.training"))}
        assert not bad, (path.name, bad)


def test_registry_uses_threading_only_for_its_dispatch_lock() -> None:
    """CF-03: the registry may hold a re-entrant lock and a per-thread dispatch flag; it never starts threads,
    timers or pools."""
    tree = ast.parse((FABRIC / "registry.py").read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "threading"}
    assert used == {"RLock", "local"}


OPERATOR_SIDE = ("pados.capability.operator_decisions", "pados.capability.operator_cli")


def test_execution_side_never_imports_the_operator_issuer_or_its_entry_module() -> None:
    """CR13b-01: signing lives only in operator_decisions, reached only through operator_cli; no execution-side
    fabric module and no pack imports either of them (or names their entry points)."""
    execution_side = [FABRIC / name for name in ("executor.py", "planner.py", "registry.py", "verification.py",
                                                  "inference.py", "adapter.py", "graph.py", "task.py",
                                                  "provenance.py", "human_decision.py", "decision_ledger.py",
                                                  "execution_ledger.py", "seams.py", "policy.py", "receipts.py", "artifacts.py", "model.py",
                                                  "__init__.py")]
    execution_side += sorted((SRC / "packs").rglob("*.py"))
    assert {path.name for path in FABRIC.glob("*.py")} - {path.name for path in execution_side} == {
        "operator_decisions.py", "operator_cli.py"}  # every other fabric module is checked
    for path in execution_side:
        bad = {item for item in imports(path) if item.startswith(OPERATOR_SIDE)}
        assert not bad, (path.relative_to(SRC).as_posix(), bad)
        used = names_used(path)
        assert not {"open_operator_issuer", "OperatorDecisionIssuer", "operator_decisions", "operator_cli"} & used, (
            path.relative_to(SRC).as_posix())


def test_operator_cli_is_the_only_importer_of_operator_decisions() -> None:
    """Entry-module isolation: within src/pados only operator_cli imports the issuer; nothing imports the CLI."""
    importers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                 if any(item.startswith(OPERATOR_SIDE[0]) for item in imports(path))}
    assert importers == {"capability/operator_cli.py"}
    cli_importers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                     if any(item.startswith(OPERATOR_SIDE[1]) for item in imports(path))}
    assert cli_importers == set()
    relative = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig")))
                if isinstance(node, ast.ImportFrom) and node.level > 0
                and (node.module or "").split(".")[-1] in {"operator_decisions", "operator_cli"}}
    assert relative == set()


def test_in_memory_decision_ledger_is_called_only_by_tests() -> None:
    """CR13b-02: the non-durable ledger has a test-only constructor that no non-test module calls."""
    for path in sorted(SRC.rglob("*.py")):
        if path == FABRIC / "decision_ledger.py":
            continue
        used = names_used(path) | {name.rsplit(".", 1)[-1] for name in imports(path)}
        assert not {"in_memory_ledger_for_tests", "_InMemoryConsumedDecisions"} & used, path.relative_to(SRC).as_posix()


def test_only_the_executor_holds_the_execution_ledger_writer() -> None:
    """CR-16b H-04: within src/pados only the executor names the writer class (consumers get a read-only
    ``ExecutionLedgerReader``), and no module but the executor touches the dispatch context it publishes
    (CR-16b H-06: hooks only read it through ``current_dispatch``)."""
    ledger = FABRIC / "execution_ledger.py"
    writers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
               if path != ledger and "FileExecutionLedger" in names_used(path) | {
                   name.rsplit(".", 1)[-1] for name in imports(path)}}
    assert writers == {"capability/executor.py"}
    dispatch = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                if {"_DISPATCH", "DispatchContext"} & names_used(path)}
    assert dispatch == {"capability/executor.py"}
