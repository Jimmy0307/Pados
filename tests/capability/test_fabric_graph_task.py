"""TaskContract (closed, bounded) and the capability graph (DAG, templates as data)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fabric_support import PROJECT, fabric, template
from pydantic import ValidationError

from pados.capability.artifacts import ArtifactRef
from pados.capability.graph import (
    CapabilityGraph,
    GraphError,
    GraphStage,
    TemplateRegistry,
    topological_order,
)
from pados.capability.task import TaskContract, TaskInput, validated_task
from pados.enterprise.classification import DataClassification

REF = ArtifactRef(sha256="a" * 64, media_type="text/plain", size_bytes=1, kind="doc.scan")


def stages(*specs: tuple[str, list[str]]) -> list[dict[str, Any]]:
    return [{"stage_id": sid, "capability_id": "DOCUMENT_OCR",
             "inputs": [{"source": "STAGE_OUTPUT", "name": up} for up in ups]} for sid, ups in specs]


# --- TaskContract ----------------------------------------------------------------------------

def test_task_contract_is_closed_and_bounded(tmp_path: Path) -> None:
    task = fabric(tmp_path).task()
    data = task.model_dump()
    for extra in ({"adapter_id": "ocr.alpha"}, {"command": "tesseract"}, {"authorized": True}):
        with pytest.raises(ValidationError):
            TaskContract.model_validate({**data, **extra})
    with pytest.raises(ValidationError, match="TASK_PARAMETERS_NOT_FLAT"):
        TaskContract.model_validate({**data, "parameters": {"nested": {"a": 1}}})
    with pytest.raises(ValidationError, match="TASK_PARAMETERS_TOO_MANY"):
        TaskContract.model_validate({**data, "parameters": {f"k{i}": i for i in range(40)}})
    with pytest.raises(ValidationError, match="TASK_PARAMETERS_VALUE_TOO_LONG"):
        TaskContract.model_validate({**data, "parameters": {"k": "x" * 300}})
    credential = "pass" + "word=hunter2hunter2"
    with pytest.raises(ValueError):
        TaskContract.model_validate({**data, "parameters": {"note": credential}})
    with pytest.raises(ValidationError, match="TASK_REQUIRED_OUTPUT_INVALID"):
        TaskContract.model_validate({**data, "required_outputs": ["http://x/v1"]})
    with pytest.raises(TypeError):
        task.parameters["x"] = 1  # type: ignore[index]


def test_task_inputs_are_project_and_classification_checked() -> None:
    base = dict(task_id="t", task_type="doc.extract", project_id=PROJECT, work_contract_id="wc-1",
                classification=DataClassification.INTERNAL, required_outputs=("text.chunks/v1",))
    with pytest.raises(ValidationError, match="TASK_INPUT_PROJECT_MISMATCH"):
        TaskContract(**base, inputs=(TaskInput(name="scan", ref=REF, project_id="other",  # type: ignore[arg-type]
                                               classification=DataClassification.INTERNAL),))
    with pytest.raises(ValidationError, match="TASK_INPUT_CLASSIFICATION_EXCEEDS_TASK"):
        TaskContract(**base, inputs=(TaskInput(name="scan", ref=REF, project_id=PROJECT,  # type: ignore[arg-type]
                                               classification=DataClassification.RESTRICTED),))
    dup = TaskInput(name="scan", ref=REF, project_id=PROJECT, classification=DataClassification.INTERNAL)
    with pytest.raises(ValidationError, match="TASK_INPUT_NAME_DUPLICATE"):
        TaskContract(**base, inputs=(dup, dup))  # type: ignore[arg-type]


def test_forged_task_is_revalidated(tmp_path: Path) -> None:
    task = fabric(tmp_path).task()
    forged = TaskContract.model_construct(**{**dict(task), "project_id": "evil project!"})
    with pytest.raises(ValidationError):
        validated_task(forged)
    assert validated_task(task).task_hash == task.task_hash


# --- graph -----------------------------------------------------------------------------------

def test_cycle_detection_and_unknown_upstream() -> None:
    with pytest.raises(ValidationError, match="GRAPH_CYCLE"):
        CapabilityGraph.model_validate({"stages": stages(("a", ["b"]), ("b", ["a"]))})
    with pytest.raises(ValidationError, match="GRAPH_CYCLE"):
        CapabilityGraph.model_validate({"stages": stages(("a", ["a"]))})
    with pytest.raises(ValidationError, match="GRAPH_UPSTREAM_STAGE_UNKNOWN"):
        CapabilityGraph.model_validate({"stages": stages(("a", ["zz"]))})
    with pytest.raises(ValidationError, match="GRAPH_STAGE_ID_DUPLICATE"):
        CapabilityGraph.model_validate({"stages": stages(("a", []), ("a", []))})
    with pytest.raises(GraphError, match="GRAPH_CYCLE"):
        topological_order(tuple(GraphStage.model_validate(s) for s in stages(("a", ["b"]), ("b", ["a"]))))


def test_topological_order_is_deterministic() -> None:
    graph = CapabilityGraph.model_validate({"stages": stages(("join", ["left", "right"]), ("right", ["src"]),
                                                             ("left", ["src"]), ("src", []))})
    assert [s.stage_id for s in graph.ordered()] == ["src", "right", "left", "join"]
    assert graph.terminal_stage_ids() == ("join",)
    assert graph.graph_hash == CapabilityGraph.model_validate(graph.model_dump()).graph_hash


def test_graph_metadata_cannot_carry_instructions() -> None:
    for bad in ({"stage_id": "a; rm", "capability_id": "DOCUMENT_OCR"},
                {"stage_id": "a", "capability_id": "curl http://x"},
                {"stage_id": "a", "capability_id": "DOCUMENT_OCR", "adapter_id": "ocr.alpha"},
                {"stage_id": "a", "capability_id": "DOCUMENT_OCR",
                 "inputs": [{"source": "TASK_INPUT", "name": "../etc/passwd"}]},
                {"stage_id": "a", "capability_id": "DOCUMENT_OCR",
                 "inputs": [{"source": "SHELL", "name": "x"}]}):
        with pytest.raises(ValidationError):
            GraphStage.model_validate(bad)


def test_templates_are_data_with_no_silent_override(tmp_path: Path) -> None:
    registry = TemplateRegistry()
    registry.register(template())
    with pytest.raises(GraphError, match="TEMPLATE_TASK_TYPE_ALREADY_REGISTERED"):
        registry.register(template())
    with pytest.raises(GraphError, match="TEMPLATE_INVALID"):
        registry.register({"template_id": "x", "task_type": "doc.y", "version": "1", "graph": {"stages": []}})
    with pytest.raises(GraphError, match="TEMPLATE_NOT_FOUND"):
        registry.template_for("unknown.type")
    fab = fabric(tmp_path)
    _, graph = registry.decompose(fab.task())
    assert [s.stage_id for s in graph.ordered()] == ["ocr", "chunk"]
    task_without_scan = fab.task(inputs=())
    with pytest.raises(GraphError, match="GRAPH_TASK_INPUT_MISSING"):
        registry.decompose(task_without_scan)


def test_task_parameters_flow_only_where_the_template_names_them(tmp_path: Path) -> None:
    registry = TemplateRegistry()
    registry.register(template("doc.params", [
        {"stage_id": "ocr", "capability_id": "DOCUMENT_OCR", "inputs": [{"source": "TASK_INPUT", "name": "scan"}],
         "parameters": {"dpi": 300}, "task_parameters": ["lang"]}]))
    task = fabric(tmp_path).task(task_type="doc.params", parameters={"lang": "de", "other": "ignored"})
    _, graph = registry.decompose(task)
    assert dict(graph.stage("ocr").parameters) == {"dpi": 300, "lang": "de"}
