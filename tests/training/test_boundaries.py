"""Static authority boundaries of the Training Plane package."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "pados" / "training"
FORBIDDEN_PREFIXES = (
    "pados.state_proof.service", "pados.state_proof.repository", "pados.state_proof.cas",
    "pados.product_loop.state", "pados.product_loop.service", "pados.memory", "pados.worker",
    "pados.release", "pados.db", "pados.api", "pados.cli", "pados.operations", "pados.remote",
    "pados.knowledge", "pados.wiki", "pados.gitops",
)
FORBIDDEN_MODULES = ("socket", "urllib", "http", "httpx", "requests", "subprocess", "threading", "asyncio",
                     "multiprocessing", "sched", "concurrent", "numpy", "torch", "sklearn", "pickle", "shelve")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_package_does_not_import_authority_write_paths_network_or_frameworks() -> None:
    files = sorted(PACKAGE.glob("*.py"))
    assert {f.name for f in files} >= {"contracts.py", "jobs.py", "backends.py", "evaluation.py", "registry.py",
                                        "decisions.py"}
    for path in files:
        for name in _imports(path):
            assert not name.startswith(FORBIDDEN_PREFIXES), f"{path.name} imports {name}"
            assert name.split(".")[0] not in FORBIDDEN_MODULES, f"{path.name} imports {name}"
            assert "audit" not in name and "cas" not in name.split("."), f"{path.name} imports {name}"


def test_training_jobs_and_backends_cannot_reach_the_registry_or_promotion() -> None:
    for name in ("jobs.py", "backends.py", "contracts.py", "evaluation.py", "decisions.py", "_chain.py"):
        imports = _imports(PACKAGE / name)
        assert "pados.training.registry" not in imports, name
    for name in ("backends.py", "contracts.py", "evaluation.py"):
        assert "pados.training.decisions" not in _imports(PACKAGE / name), name
    jobs_source = (PACKAGE / "jobs.py").read_text(encoding="utf-8")
    for word in ("PROMOTE_MODEL", "PromotionGate", "CandidateState", "register("):
        assert word not in jobs_source


def test_no_model_downloads_or_dynamic_code() -> None:
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("eval(", "exec(", "__import__", "importlib", "from_pretrained", "hf_hub", "download"):
            assert token not in source, f"{path.name} contains {token}"
