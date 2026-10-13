"""Static boundaries of the OCR pack: additive, no authority imports, no shell, no network, documented.

Process creation is allowed in exactly one module, ``_isolated.py`` (the bounded runner): one
``subprocess.Popen``, ``shell=False``, an explicit environment, piped streams, its own session and NO
``preexec_fn``; never ``capture_output``/``communicate``/``run``. Resource limits are set only by
``_rlimit_exec.py`` (in the child), which also holds the pack's single ``os.execv`` (the launcher)
and the only ``ctypes`` use (``PR_SET_NO_NEW_PRIVS`` in the child). Engine bindings are imported only
by ``_binding_worker.py`` (in the child): no other module uses ``importlib.import_module``. Every
other ban is unchanged.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "src" / "pados" / "packs" / "ocr"
MODULES = sorted(PACK.glob("*.py"))
ALLOWED_PADOS = ("pados.capability", "pados.packs.ocr", "pados.enterprise.classification",
                 "pados.enterprise.lineage", "pados.connectivity.requirements", "pados.lifecycle.minimization",
                 "pados.execution.context")
FORBIDDEN_MODULES = ("socket", "urllib", "http", "requests", "httpx", "sqlalchemy", "psycopg", "ftplib",
                     "smtplib", "threading", "multiprocessing", "asyncio", "sched", "shutil")
PROCESS_MODULE = "_isolated.py"  # the only module that may create processes
LIMITS_MODULE = "_rlimit_exec.py"  # the only module that sets rlimits or execs (in the child)
BINDING_MODULE = "_binding_worker.py"  # the only module that imports an operator binding (in the child)
MODULE_ONLY_IMPORTS = {"subprocess": PROCESS_MODULE, "selectors": PROCESS_MODULE, "signal": PROCESS_MODULE,
                       "resource": LIMITS_MODULE, "ctypes": LIMITS_MODULE}
BANNED_CALLS = re.compile(r"\bos\.(system|popen|exec\w*|spawn\w*|fork\w*|posix_spawn\w*)\(|shell\s*=\s*True"
                          r"|capture_output\s*=|\.communicate\(|subprocess\.(run|call|check_call|check_output)\("
                          r"|preexec_fn\s*=")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_pack_imports_no_authority_or_network_modules(path: Path) -> None:
    for name in _imports(path):
        if name.startswith("pados"):
            assert name.startswith(ALLOWED_PADOS), f"{path.name} imports {name}"
        assert name.split(".")[0] not in FORBIDDEN_MODULES, f"{path.name} imports {name}"
        owner = MODULE_ONLY_IMPORTS.get(name.split(".")[0])
        if owner is not None:
            assert path.name == owner, f"{path.name} imports {name}"


def test_process_creation_is_confined_to_the_bounded_runner() -> None:
    tree = ast.parse((PACK / PROCESS_MODULE).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"]
    assert [call.func.attr for call in calls] == ["Popen"]  # POSIX only; Windows spawns nothing
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert isinstance(keywords["start_new_session"], ast.Constant) and keywords["start_new_session"].value is True
    assert {"env", "stdin", "stdout", "stderr", "close_fds"} <= set(keywords)
    assert not {"capture_output", "input", "preexec_fn", "creationflags"} & set(keywords)
    runner = (PACK / PROCESS_MODULE).read_text(encoding="utf-8")
    assert "killpg" in runner and "tempfile" not in runner and "CREATE_NEW_PROCESS_GROUP" not in runner
    assert '"-I"' in runner and '"-m"' in runner  # the PDF worker runs as sys.executable -I -m
    limits = (PACK / LIMITS_MODULE).read_text(encoding="utf-8")
    for limit in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_NPROC"):
        assert limit in limits
    assert len(re.findall(r"\bos\.execv\(", limits)) == 1 and "os.path.isabs(binary)" in limits
    assert "executable_refusal(binary)" in limits and "nproc=inherit" not in limits  # no relaxed NPROC token
    for module in MODULES:
        if module.name != BINDING_MODULE:
            assert "import_module" not in module.read_text(encoding="utf-8"), module.name
    for module in MODULES:
        source = module.read_text(encoding="utf-8")
        checked = source.replace("os.execv(binary,", "") if module.name == LIMITS_MODULE else source
        assert not BANNED_CALLS.search(checked), module.name
        if module.name != PROCESS_MODULE:
            assert "Popen" not in source, module.name


def test_pdf_parsing_never_happens_in_process() -> None:
    for module in MODULES:
        if module.name == "_pdf_worker.py":
            continue
        assert "pypdf" not in {name.split(".")[0] for name in _imports(module)}, module.name
        assert "PdfReader" not in module.read_text(encoding="utf-8"), module.name


def test_adapters_are_only_executed_through_the_bound_stage() -> None:
    for module in MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
                raise AssertionError(f"{module.name} calls .execute directly")
    pipeline = (PACK / "pipeline.py").read_text(encoding="utf-8")
    assert "run_bound_stage(" in pipeline and "bind_stage(" in pipeline


def test_core_never_imports_the_pack() -> None:
    offenders = [path for path in (ROOT / "src" / "pados").rglob("*.py")
                 if "packs" not in path.relative_to(ROOT / "src" / "pados").parts
                 and "pados.packs" in path.read_text(encoding="utf-8")]
    assert offenders == []
    assert (ROOT / "src" / "pados" / "packs" / "__init__.py").read_text(encoding="utf-8") == (
        '# SPDX-License-Identifier: AGPL-3.0-only\n"""PADOS capability packs (additive; never Core)."""\n')


def test_engine_matrix_is_documented_with_pending_live_uat() -> None:
    from ocr_support import every_adapter

    doc = (ROOT / "docs" / "packs" / "ocr" / "OCR_CAPABILITY_PACK_V1.md").read_text(encoding="utf-8")
    assert "OCR_LIVE_ENGINE_UAT = PENDING" in doc
    for adapter in every_adapter():
        descriptor = adapter.descriptor
        row = next((line for line in doc.splitlines() if f"`{descriptor.adapter_id}`" in line), "")
        assert descriptor.license.spdx in row and descriptor.license.distribution.value in row, descriptor.adapter_id
