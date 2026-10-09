"""Static and behavioural boundary: the Learning Plane cannot reach or change any authority."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from pados.capability.policy import PolicyCode, PolicyContext, authorize_scope, evaluate
from pados.capability.seams import ADVICE_BOUND, apply_advice
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.learning.advisor import AdvisorConfig, OutcomeAdvisor, advice_context_hash
from tests.learning.learning_support import (
    BINDING,
    CONF,
    NOW,
    OCR,
    SCANNED_ZH,
    ListSource,
    adapter,
    history,
    memory_log,
)

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "pados" / "learning"
# Authority writers, policy/registry/grant mutation, routers and network clients are out of reach.
FORBIDDEN_PREFIXES = (
    "pados.capability.policy", "pados.capability.adapter", "pados.capability.registry",
    "pados.execution.authorization", "pados.enterprise.grants", "pados.enterprise.approval",
    "pados.state_proof", "pados.product_loop", "pados.memory", "pados.worker", "pados.db", "pados.api",
    "pados.release.approval", "pados.machines.privilege", "pados.remote", "pados.operations", "pados.core",
    "pados.project_goal", "pados.security", "pados.models", "pados.providers", "pados.agents", "pados.loop",
    "pados.training", "sqlalchemy", "socket", "http", "urllib", "requests", "httpx", "subprocess", "threading", "asyncio",
)
TASK = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR")


def imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_learning_imports_no_authority_policy_router_or_network_module() -> None:
    files = sorted(PACKAGE.glob("*.py"))
    assert {f.name for f in files} >= {"records.py", "store.py", "profiles.py", "advisor.py"}
    for path in files:
        for module in imported_modules(path):
            assert not any(module == p or module.startswith(p + ".") for p in FORBIDDEN_PREFIXES), (
                path.name, module)


def test_importing_learning_does_not_load_router_policy_or_semantic_writers() -> None:
    probe = ("import sys, pados.learning.advisor, pados.learning.store\n"
             "bad = ('pados.worker.router', 'pados.capability.policy', 'pados.capability.adapter',"
             " 'pados.state_proof.service', 'pados.state_proof.repository', 'pados.product_loop',"
             " 'pados.memory', 'sqlalchemy')\n"
             "print(sorted(m for m in sys.modules if m.startswith(bad)))\n")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120)
    assert result.stdout.strip() == "[]"


def test_advice_cannot_change_a_policy_decision_or_make_a_denied_adapter_feasible() -> None:
    contract = {"work_contract_id": "wc-1", "project_id": "p", "required_capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
    grant = {"work_contract_id": "wc-1", "project_id": "p", "capabilities": ["CAPABILITY:DOCUMENT_OCR"]}
    context = PolicyContext(scope=authorize_scope(contract, grant), project_id="p", stage_id="s1",
                            classification=DataClassification.CONFIDENTIAL,
                            network_policy=NetworkRequirement.LOCAL_ONLY)
    local_a, local_b = adapter("ocr.engine-a"), adapter("ocr.engine-b")
    cloud = adapter("ocr.cloud-engine", network_requirement=NetworkRequirement.WAN_REQUIRED)
    candidates = [local_a, local_b, cloud]

    def decide() -> dict[str, tuple[PolicyCode, str]]:
        decisions = {c.adapter_id: evaluate(OCR, c, context, adapter_available=lambda: True) for c in candidates}
        return {name: (d.code, d.decision_hash) for name, d in decisions.items()}

    before = decide()
    assert before["ocr.cloud-engine"][0] is PolicyCode.NETWORK_NOT_PERMITTED
    # The cloud engine has a perfect record; a misbehaving planner even hands it to the advisor.
    records = [*history("ocr.engine-a", 30, 10), *history("ocr.engine-b", 20, 20, offset=100),
               *history("ocr.cloud-engine", 60, 0, offset=200)]
    target = OutcomeAdvisor(ListSource(records), config=AdvisorConfig(enabled=True), log=memory_log(),
                            clock=lambda: NOW)
    advice = target.advise(**TASK, candidates=candidates, context_features=SCANNED_ZH, classification=CONF,
                           plan_binding_hash=BINDING)
    assert max(advice, key=lambda a: a.score).adapter_id == "ocr.cloud-engine"
    assert decide() == before  # decisions (codes and hashes) are untouched
    feasible = [name for name, (code, _) in before.items() if code is PolicyCode.ALLOWED]
    adjustments = apply_advice(feasible, advice,
                               context_hash=advice_context_hash(**TASK, context_features=SCANNED_ZH,
                                                                candidates=candidates, classification=CONF,
                                                                plan_binding_hash=BINDING))
    assert set(adjustments) == {"ocr.engine-a", "ocr.engine-b"}
    assert all(abs(value) <= ADVICE_BOUND for value in adjustments.values())


def test_advisor_holds_no_authority_handles() -> None:
    target = OutcomeAdvisor(ListSource([]), config=AdvisorConfig(enabled=True), log=memory_log())
    held = {type(value).__module__ for value in vars(target).values()}
    assert all(module.startswith(("pados.learning", "tests.learning", "builtins")) for module in held), held
    public = {name for name in dir(target) if not name.startswith("_")}
    assert public == {"advise", "advise_logged", "config", "context_hash", "identity_digests", "issued", "log",
                      "profiles", "rederive", "verify"}
