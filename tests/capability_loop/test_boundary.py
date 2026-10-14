# SPDX-License-Identifier: AGPL-3.0-only
"""Authority boundary: the capability loop connects external runtimes but holds no PADOS authority."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
LOOP_DIR = SRC / "pados" / "capability_loop"
LOOP_MODULES = sorted(LOOP_DIR.rglob("*.py"))
# Scheduling, routing, CAS/state-proof, memory, Core receipts and registry MUTATORS are off limits.
FORBIDDEN = (
    "pados.state_proof", "pados.memory", "pados.product_loop", "pados.worker", "pados.remote",
    "pados.control", "pados.loop", "pados.scheduler", "pados.core", "pados.db", "pados.api",
    "pados.cli", "pados.gitops", "pados.agents", "pados.providers", "pados.graph",
    "pados.execution.receipts", "pados.execution.handoff", "pados.capability.registry",
    "pados.capability.executor", "pados.capability.receipts", "pados.worker.router",
)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_loop_never_imports_core_authority() -> None:
    for module in LOOP_MODULES:
        imports = _imports(module)
        for name in imports:
            for forbidden in FORBIDDEN:
                assert not (name == forbidden or name.startswith(forbidden + ".")), f"{module.name} imports {name}"


def test_every_contract_declares_no_semantic_authority() -> None:
    from pados.capability_loop.contracts import (
        CapabilityCandidate,
        DiscoveryOutcome,
    )
    from pados.capability_loop.receipts import OperationReceipt

    for model in (CapabilityCandidate, DiscoveryOutcome):
        assert model.model_fields["semantic_authority"].default == "NONE"
    assert OperationReceipt.model_fields["semantic_authority"].default == "NONE"
    assert CapabilityCandidate.model_fields["auto_promote"].default is False


def test_discovery_states_exclude_validated() -> None:
    from pados.capability_loop.contracts import DISCOVERY_STATES, CapabilityState

    assert CapabilityState.VALIDATED not in DISCOVERY_STATES
    assert CapabilityState.DISCOVERED in DISCOVERY_STATES
