# SPDX-License-Identifier: AGPL-3.0-only
"""Static boundaries of Capability Pack V1: a pack is an extension contract, never an authority.

* ``pados.capability.pack`` (the contract) and every pack package under ``pados.packs`` that implements it may not
  import scheduler, router, CAS/state-proof, approval, memory, DB, product-loop or provider code;
* pack packages may not reach the executor's stage-binding path (``bind_stage`` / ``run_bound_stage`` /
  ``guarded``), the registry, the executor or the planner: they are reached only THROUGH the existing executor;
* nothing in a pack may import runtime side-effect modules (sockets, subprocesses, threads, HTTP clients).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pados

SRC = Path(pados.__file__).resolve().parent
CONTRACT = SRC / "capability" / "pack.py"
PACK_PACKAGES = ("reference", "problem_solving", "research")

AUTHORITY_AND_PROVIDERS = (
    "pados.state_proof", "pados.product_loop", "pados.memory", "pados.db", "pados.core", "pados.loop",
    "pados.worker", "pados.providers", "pados.remote", "pados.control", "pados.api", "pados.cli",
    "pados.execution.handoff", "pados.execution.semantic_context", "pados.execution.receipts",
    "pados.execution.authorization", "pados.work_protocol", "pados.capability_loop_runtime", "pados.learning",
    "pados.release.approval", "pados.capability.operator_decisions",
    "pados.capability.operator_cli", "pados.capability.inference", "pados.training", "pados.agents",
    "pados.gitops", "pados.graph",
)
FABRIC_INTERNALS = ("pados.capability.registry", "pados.capability.executor", "pados.capability.planner",
                    "pados.capability.execution_ledger", "pados.capability.decision_ledger")
STAGE_BOUNDARY = {"bind_stage", "run_bound_stage", "guarded"}
# Semantic acceptance and the single CAS live outside every pack: no pack may even name their entry points.
ACCEPTANCE_AND_CAS = {"accept_result_candidate", "submit_result_candidate", "reject_result_candidate",
                      "verified_candidate", "verify_result_candidate", "commit_result", "compare_and_swap",
                      "result_receipt_document", "ScopeVersion", "set_capability_lifecycle", "set_adapter_lifecycle"}
# The contract itself performs the operator's disable action, so it alone may name adapter lifecycle changes.
CONTRACT_OPERATOR_ACTIONS = {"set_adapter_lifecycle"}
SIDE_EFFECTS = ("subprocess", "socket", "threading", "multiprocessing", "asyncio", "httpx", "requests", "urllib",
                "http", "concurrent", "sched", "sqlalchemy", "psycopg")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _names(path: Path) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    return used


def _pack_files() -> list[Path]:
    files = [path for package in PACK_PACKAGES for path in sorted((SRC / "packs" / package).rglob("*.py"))]
    assert files, "the reference packs must exist"
    return files


def _starts(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def test_contract_imports_no_authority_provider_or_side_effect_module() -> None:
    bad = {name for name in _imports(CONTRACT) if _starts(name, AUTHORITY_AND_PROVIDERS + SIDE_EFFECTS)}
    assert not bad, bad


def test_contract_never_reaches_the_stage_binding_path() -> None:
    assert not (_names(CONTRACT) & STAGE_BOUNDARY)


def test_packs_import_no_authority_fabric_internal_provider_or_side_effect_module() -> None:
    for path in _pack_files():
        bad = {name for name in _imports(path)
               if _starts(name, AUTHORITY_AND_PROVIDERS + FABRIC_INTERNALS + SIDE_EFFECTS)}
        assert not bad, (path.relative_to(SRC).as_posix(), bad)


def test_packs_never_call_the_stage_binding_path() -> None:
    for path in _pack_files():
        assert not (_names(path) & STAGE_BOUNDARY), path.relative_to(SRC).as_posix()


def test_no_pack_and_not_the_contract_can_reach_semantic_acceptance_or_the_cas() -> None:
    assert not (_names(CONTRACT) & (ACCEPTANCE_AND_CAS - CONTRACT_OPERATOR_ACTIONS))
    for path in _pack_files():
        assert not (_names(path) & ACCEPTANCE_AND_CAS), path.relative_to(SRC).as_posix()


def test_core_does_not_import_packs_or_the_pack_contract() -> None:
    for package in ("product_loop", "state_proof", "memory", "worker", "remote", "execution", "core"):
        root = SRC / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            bad = {name for name in _imports(path) if name.startswith(("pados.packs", "pados.capability.pack"))}
            assert not bad, (path.relative_to(SRC).as_posix(), bad)
