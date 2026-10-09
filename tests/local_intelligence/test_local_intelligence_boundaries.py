"""Stage B boundaries: the Local Intelligence Stack measures; it is never a second authority."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

from pados.local_intelligence.probes import CommandNotFound, run_command

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "pados" / "local_intelligence"
FORBIDDEN_IMPORTS = (
    "pados.worker", "pados.capability.planner", "pados.capability.executor", "pados.capability.registry",
    "pados.capability.policy", "pados.capability.operator_decisions", "pados.capability.inference",
    "pados.compute_node.provider", "pados.compute_node.registry", "pados.compute_node.jobs", "pados.db",
    "pados.core", "pados.loop", "pados.state_proof", "pados.remote", "pados.memory", "pados.learning.advisor",
    "pados.product_loop", "pados.work_protocol", "pados.execution.authorization",
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _sources():
    return sorted(PACKAGE.glob("*.py"))


def test_no_router_scheduler_registry_or_authority_imports():
    for path in _sources():
        bad = sorted(name for name in _imports(path) if name.startswith(FORBIDDEN_IMPORTS))
        assert not bad, f"{path.name} imports {bad}"


# probes: fixed-argv host probes; bench_tasks: the isolated, memory-capped child that checks generated code.
PROCESS_MODULES = {"probes.py", "bench_tasks.py"}


def test_only_the_probe_and_code_check_modules_start_processes_and_never_through_a_shell():
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "subprocess" in _imports(path):
            assert path.name in PROCESS_MODULES, path.name
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "shell":
                assert isinstance(node.value, ast.Constant) and node.value.value is False
            if isinstance(node, ast.Attribute) and node.attr in {"system", "popen", "getoutput", "getstatusoutput"}:  # shell APIs
                assert not (isinstance(node.value, ast.Name) and node.value.id in {"os", "subprocess"}), path.name


def test_resources_package_still_never_probes():
    resources = ROOT / "src" / "pados" / "resources"
    assert sorted(p.stem for p in resources.glob("*.py")) == ["__init__", "capability_profile", "hardware", "inventory",
                                                              "models", "planning"]  # M3: capability_profile
    for path in resources.glob("*.py"):
        assert not {"pados.local_intelligence", "subprocess", "psutil"} & _imports(path)


def test_importing_the_stack_loads_no_router_or_scheduler():
    code = ("import sys, pados.local_intelligence.capture; "
            "bad=[m for m in sys.modules if m.startswith(('pados.worker.router','pados.worker.registry','pados.worker.service','pados.capability.planner','pados.capability.executor',"
            "'pados.core.scheduler','pados.db'))]; print(bad); raise SystemExit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_run_command_uses_argv_and_bounds_output():
    result = run_command([sys.executable, "-c", "import sys; sys.stdout.write('x' * 300000)"], 30)
    assert result.returncode == 0 and result.truncated and len(result.stdout) == 256 * 1024
    with pytest.raises(CommandNotFound):
        run_command(["pados-definitely-not-a-command-xyz"], 5)
    with pytest.raises(ValueError, match="PROBE_ARGV_INVALID"):
        run_command(["echo", "a\x00b"], 5)
    literal = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(id); `id` | rm"], 30)
    assert literal.stdout.strip() == b"$(id); `id` | rm"  # metacharacters reach the program verbatim
