# SPDX-License-Identifier: AGPL-3.0-only
"""Default-off invariant: the model worker runs only when an operator explicitly opts in, and nothing converts a
model automatically.

The only opt-in switch is the engine ``factory``: ``PinnedTextEmbedder`` / ``PinnedCrossEncoder`` default to the
in-process FastEmbed factories, and the worker is used only when a caller passes ``subprocess_factory(...)``.
No PADOS module or operator script does that, and the governed Document Intelligence workflow starts no process.
No module imports a model-conversion toolchain; the operator's fetch script downloads only pinned files.
"""
from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path

import pytest
from di_support import corpus, workbench

from pados.capability.executor import ExecutionStatus
from pados.packs.document_intelligence.local_models import (
    PinnedCrossEncoder,
    PinnedTextEmbedder,
    fastembed_cross_encoder,
    fastembed_embedding,
)
from pados.packs.document_intelligence.wiring import task_type

REPO = Path(__file__).resolve().parents[3]
WORKER_MODULE = "pados.packs.document_intelligence.model_worker"
WORKER_NAMES = {"subprocess_factory", "SubprocessModelBackend", "model_worker"}
CONVERSION_TOOLCHAINS = ("optimum", "onnx", "onnxmltools", "onnxconverter_common", "tf2onnx", "torch", "tensorflow",
                         "transformers", "onnxruntime.quantization", "onnxruntime.transformers", "onnxruntime.tools",
                         "ctranslate2", "openvino")


def _python_files(*roots: str) -> list[Path]:
    return sorted(path for root in roots for path in (REPO / root).rglob("*.py"))


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return names


def test_the_engines_default_to_the_in_process_factories() -> None:
    for engine, default in ((PinnedTextEmbedder, fastembed_embedding), (PinnedCrossEncoder, fastembed_cross_encoder)):
        parameters = inspect.signature(engine.__init__).parameters
        assert parameters["factory"].default is default
        assert parameters["private_copy"].default is False


def test_no_pados_module_or_operator_script_opts_into_the_worker() -> None:
    worker = REPO / "src" / "pados" / "packs" / "document_intelligence" / "model_worker.py"
    offenders = []
    for path in _python_files("src", "scripts"):
        if path == worker:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        if any(name.startswith(WORKER_MODULE) for name in _imports(tree)) or used & WORKER_NAMES:
            offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == []


def test_no_module_imports_a_model_conversion_toolchain() -> None:
    offenders = []
    for path in _python_files("src/pados/packs/document_intelligence", "scripts/models"):
        for name in _imports(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if any(name == tool or name.startswith(f"{tool}.") for tool in CONVERSION_TOOLCHAINS):
                offenders.append(f"{path.relative_to(REPO).as_posix()}: {name}")
    assert offenders == []


def test_the_fetch_script_downloads_only_inside_its_explicit_operator_action() -> None:
    script = REPO / "scripts" / "models" / "fetch_pinned_models.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    top_level = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert not any(name.startswith("huggingface_hub") for name in _imports(ast.Module(body=top_level,
                                                                                         type_ignores=[])))
    assert "--approve" in script.read_text(encoding="utf-8")


def test_the_governed_workflow_starts_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a process was started without an explicit opt-in")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    bench = workbench(tmp_path)
    cases = (corpus.case("case.ncr-2041"),)
    document = bench.put_document(cases)
    task = bench.evidence_task(document, task_type=task_type("document", "hybrid_rerank"),
                               query="NCR-2041 root cause corrective action", case_id=cases[0].case_id,
                               source_id="src.ncr-2041")
    _, result = bench.run(task)
    assert result is not None and result.status is ExecutionStatus.COMPLETED, result
    assert bench.embedder._backend is not None and bench.encoder._backend is not None
    assert type(bench.embedder._backend).__module__ != WORKER_MODULE
    assert type(bench.encoder._backend).__module__ != WORKER_MODULE
