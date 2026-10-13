# SPDX-License-Identifier: AGPL-3.0-only
"""Decomposition templates for the Phase G pipeline (operator-registered data; never names an adapter).

``analysis_template`` maps task type ``document_decision_analysis`` to a fabric ``CapabilityGraph``. Each
stage names a capability and where its inputs come from (the task's ``document`` input or an earlier
stage's outputs, filtered by artifact kind). Stage parameters are fixed here, except the names listed in
``task_parameters``, which the fabric copies from the task (the extraction column declarations and the
retrieval query). ``report_template`` maps ``pipeline_report`` to the single PIPELINE_REPORT stage, whose
inputs are the evidence, the final outputs and the artifacts the report adapter needs to re-perform the
algorithm replay itself (the dataset and the reproducibility record; the result is a final output).

The capability of a stage may be overridden only by the operator building a variant template (for
example to plan with an accelerator-only retrieval capability); nothing in a task can change it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pados.capability.graph import DecompositionTemplate
from pados.capability.task import Scalar
from pados.packs.algorithms.explanation import EXPLANATION_KIND, TEMPLATE_CAPABILITY
from pados.packs.algorithms.contracts import DATASET_KIND, RECORD_KIND, RESULT_KIND
from pados.packs.algorithms.registry import DRSA
from pados.packs.extraction.table_dataset import COLUMN_KEYS, STRUCTURED_EXTRACTION
from pados.packs.ocr.contracts import KIND_DOCUMENT, KIND_PREPROCESSED, OCR_REQUEST_CONTRACT, PDF_MEDIA_TYPE
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_OCR,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    TABLE_EXTRACTION,
)
from pados.packs.retrieval.lexical import PASSAGE_RETRIEVAL, RANKING_CONTRACT, RANKING_KIND

TASK_TYPE = "document_decision_analysis"
TEMPLATE_ID = "intelligence.document-decision-analysis"
REPORT_TASK_TYPE = "pipeline_report"
REPORT_TEMPLATE_ID = "intelligence.pipeline-report"
PIPELINE_REPORT = "PIPELINE_REPORT"
TEMPLATE_VERSION = "1"
DOCUMENT_INPUT = "document"
EVIDENCE_INPUT = "evidence"
EXPLANATION_CONTRACT = TEMPLATE_CAPABILITY.output_contract
ANALYSIS_OUTPUTS = (RANKING_CONTRACT, EXPLANATION_CONTRACT)

# The OCR stage parameters every OCR stage receives (``ocr.request/v1`` StageParameters, closed schema).
OCR_PARAMETERS: Mapping[str, Scalar] = {"contract": OCR_REQUEST_CONTRACT, "media_type": PDF_MEDIA_TYPE,
                                        "preserve_layout": False, "require_tables": True}
ALGORITHM_PARAMETERS: Mapping[str, Scalar] = {"max_rules": 64, "operation_budget": 1_000_000,
                                              "include_covered_objects": True, "precision": 10}
RETRIEVAL_PARAMETERS: Mapping[str, Scalar] = {"top_k": 5, "k1": 1.2, "b": 0.75}
EXTRACTION_TASK_PARAMETERS = ("table_ordinal", "object_column", "decision_column", *COLUMN_KEYS)
RETRIEVAL_TASK_PARAMETERS = ("query", "top_k")

# The final outputs the report task takes as inputs: name -> artifact kind (one of each per analysis run).
FINAL_OUTPUT_KINDS: Mapping[str, str] = {"ranking": RANKING_KIND, "result": RESULT_KIND,
                                         "explanation": EXPLANATION_KIND}
# The algorithm stage's other replay artifacts the report task also takes as inputs (CR-16b H-05): the report
# adapter re-performs ``verify_replay`` on (dataset, result, record) inside its own governed stage.
REPLAY_INPUT_KINDS: Mapping[str, str] = {"dataset": DATASET_KIND, "record": RECORD_KIND}

# stage id -> default capability (declaration order is the deterministic execution order).
ANALYSIS_STAGES: Mapping[str, str] = {
    "preprocess": DOCUMENT_PREPROCESS,
    "recognize": DOCUMENT_OCR,
    "layout": DOCUMENT_LAYOUT_ANALYSIS,
    "tables": TABLE_EXTRACTION,
    "structure": DOCUMENT_STRUCTURE_PARSE,
    "extract": STRUCTURED_EXTRACTION,
    "retrieve": PASSAGE_RETRIEVAL,
    "analyze": DRSA.capability.capability_id,
    "explain": TEMPLATE_CAPABILITY.capability_id,
}


def _task(name: str) -> dict[str, Any]:
    return {"source": "TASK_INPUT", "name": name}


def _stage(name: str, kind: str | None = None) -> dict[str, Any]:
    return {"source": "STAGE_OUTPUT", "name": name, "artifact_kind": kind}


def analysis_stages(capabilities: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The stage documents of the analysis graph (``capabilities`` overrides by stage id, operator only)."""
    chosen = {**ANALYSIS_STAGES, **(capabilities or {})}
    if set(chosen) != set(ANALYSIS_STAGES):
        raise ValueError("TEMPLATE_STAGE_UNKNOWN")
    ocr = dict(OCR_PARAMETERS)
    return [
        {"stage_id": "preprocess", "capability_id": chosen["preprocess"], "inputs": [_task(DOCUMENT_INPUT)],
         "parameters": ocr},
        {"stage_id": "recognize", "capability_id": chosen["recognize"],
         "inputs": [_task(DOCUMENT_INPUT), _stage("preprocess", KIND_PREPROCESSED)], "parameters": ocr},
        {"stage_id": "layout", "capability_id": chosen["layout"], "inputs": [_stage("recognize", KIND_DOCUMENT)],
         "parameters": ocr},
        {"stage_id": "tables", "capability_id": chosen["tables"], "inputs": [_stage("layout", KIND_DOCUMENT)],
         "parameters": ocr},
        {"stage_id": "structure", "capability_id": chosen["structure"], "inputs": [_stage("tables", KIND_DOCUMENT)],
         "parameters": ocr},
        {"stage_id": "extract", "capability_id": chosen["extract"], "inputs": [_stage("structure", KIND_DOCUMENT)],
         "parameters": {"table_ordinal": 1}, "task_parameters": list(EXTRACTION_TASK_PARAMETERS)},
        {"stage_id": "retrieve", "capability_id": chosen["retrieve"], "inputs": [_stage("structure", KIND_DOCUMENT)],
         "parameters": dict(RETRIEVAL_PARAMETERS), "task_parameters": list(RETRIEVAL_TASK_PARAMETERS)},
        {"stage_id": "analyze", "capability_id": chosen["analyze"], "inputs": [_stage("extract", DATASET_KIND)],
         "parameters": dict(ALGORITHM_PARAMETERS)},
        {"stage_id": "explain", "capability_id": chosen["explain"], "inputs": [_stage("analyze", RESULT_KIND)]},
    ]


def analysis_template(capabilities: Mapping[str, str] | None = None, *,
                      task_type: str = TASK_TYPE) -> DecompositionTemplate:
    template_id = TEMPLATE_ID if task_type == TASK_TYPE else f"intelligence.{task_type}"
    return DecompositionTemplate.model_validate({
        "template_id": template_id, "version": TEMPLATE_VERSION, "task_type": task_type,
        "graph": {"stages": analysis_stages(capabilities)}})


def report_template() -> DecompositionTemplate:
    inputs = [_task(EVIDENCE_INPUT), *(_task(name) for name in (*FINAL_OUTPUT_KINDS, *REPLAY_INPUT_KINDS))]
    return DecompositionTemplate.model_validate({
        "template_id": REPORT_TEMPLATE_ID, "version": TEMPLATE_VERSION, "task_type": REPORT_TASK_TYPE,
        "graph": {"stages": [{"stage_id": "report", "capability_id": PIPELINE_REPORT, "inputs": inputs}]}})


def stage_grants(template: DecompositionTemplate) -> tuple[str, ...]:
    """The ``CAPABILITY:<id>`` names a project grant must contain for every stage of ``template``."""
    return tuple(sorted({f"CAPABILITY:{stage.capability_id}" for stage in template.graph.stages}))
