# SPDX-License-Identifier: AGPL-3.0-only
"""Operator wiring for the Phase G pipeline: registration, templates, validators and planner config.

``wire_pipeline`` does what an operator does once, explicitly, and nothing else: it registers the
capability descriptors, installs and registers the production adapters (installed != registered), registers
the two decomposition templates, the output-contract SCHEMA validators and REFERENCE_CHECK hooks, and returns
the operator's ``PlannerConfig`` (an explicit preference for the PDF text layer; advice disabled unless asked
for). It grants nothing: authorization comes only from the project grant read through the caller's
``GrantSource``. It plans nothing and runs nothing: the caller builds the fabric ``CapabilityPlanner`` and
``CapabilityExecutor`` from this state.

Production only: ``register_production_adapter`` refuses an adapter that declares itself not production
eligible or uses the ``fixture`` backend (the OCR pack's fixture-truth adapter is test-only), so the
recognition stage can only ever be the real pypdf text layer.

Verification wired here (per output contract):

* SCHEMA - every stage output is re-parsed with its contract's strict loader and must be canonical;
* REFERENCE_CHECK ``ocr.document/v1`` (DOCUMENT_OCR) - the invocation is the one the EXECUTOR publishes
  while it verifies the stage (``pados.capability.executor.current_dispatch()``: the task it is executing
  and the stage's actual bound inputs; CR-16b H-06), never a caller-supplied scope. The document's source
  is exactly that task's ``document`` input AND one of the stage's actual inputs, and the operator's
  provenance resolves it to that task's project and to the classification its input declares, within the
  task ceiling (CR-16 H-02: never "registered anywhere"); no page came from a fixture engine. Outside an
  executor dispatch the check fails (never passes). ``PipelineWiring.invocations.bind(task)`` is now only an
  optional operator cross-check: a binding of any OTHER task than the one executing fails the check;
* REFERENCE_CHECK ``algorithms.drsa-result/v1`` - the reproducibility record names the registered
  algorithm, adapter and version, the template's request parameters hash and exactly the result bytes.
  The full recomputation (``verify_replay``) is re-performed by the report adapter inside the report stage
  (``report.replay_in_stage``); the builder's own replay (``report.replay_algorithm_stages``) is advisory.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from pados.capability.adapter import CapabilityAdapter
from pados.capability.artifacts import ArtifactRef
from pados.capability.execution_ledger import ExecutionLedgerReader
from pados.capability.executor import current_dispatch
from pados.capability.graph import TemplateRegistry
from pados.capability.model import CapabilityDescriptor
from pados.capability.planner import PlannerConfig
from pados.capability.provenance import InputProvenance, resolved_provenance
from pados.capability.receipts import parameters_hash
from pados.capability.registry import CapabilityRegistry
from pados.capability.task import TaskContract, validated_task
from pados.capability.verification import OutputValidator, ValidatorRegistry, VerificationEngine
from pados.packs.algorithms.adapters import AlgorithmAdapter
from pados.packs.algorithms.contracts import (
    JSON_MEDIA,
    RECORD_KIND,
    RESULT_KIND,
    AlgorithmError,
    canonical_json,
    load_record,
    load_result,
    parse_json_document,
    sha256_hex,
)
from pados.packs.algorithms.explanation import (
    EXPLANATION_KIND,
    EXPLANATION_LABEL,
    EXPLANATION_SCHEMA,
    TEMPLATE_CAPABILITY,
    ModelExplanationAdapter,
    TemplateExplanationAdapter,
    explanation_capability,
)
from pados.packs.algorithms.registry import DRSA, adapter_descriptor
from pados.packs.extraction.table_dataset import CAPABILITY as EXTRACTION_CAPABILITY
from pados.packs.extraction.table_dataset import TableDatasetAdapter, dataset_output_valid
from pados.packs.intelligence.report import CAPABILITY as REPORT_CAPABILITY
from pados.packs.intelligence.report import REPORT_CONTRACT, PipelineReportAdapter, report_output_valid
from pados.packs.intelligence.template import (
    ALGORITHM_PARAMETERS,
    DOCUMENT_INPUT,
    analysis_template,
    report_template,
)
from pados.packs.ocr.contracts import (
    JSON_MEDIA_TYPE,
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    OCR_DOCUMENT_CONTRACT,
    OCR_PREPROCESSED_CONTRACT,
    OcrContractError,
    OcrDocument,
    PreprocessManifest,
)
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_OCR,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    OCR_CAPABILITIES,
    TABLE_EXTRACTION,
)
from pados.packs.ocr.reference import (
    DocumentPreprocessAdapter,
    HeuristicStructureAdapter,
    LineLayoutAdapter,
    WhitespaceTableAdapter,
)
from pados.packs.ocr.text_layer import TEXT_LAYER_ADAPTER_ID, PdfTextLayerAdapter
from pados.packs.retrieval.engines import EMBEDDING_CAPABILITY, RERANK_CAPABILITY, embedding_adapter, reranker_adapter
from pados.packs.retrieval.lexical import CAPABILITY as RETRIEVAL_CAPABILITY
from pados.packs.retrieval.lexical import RANKING_CONTRACT, LexicalRetrievalAdapter, ranking_output_valid

OCR_STAGE_CAPABILITIES = (DOCUMENT_PREPROCESS, DOCUMENT_OCR, DOCUMENT_LAYOUT_ANALYSIS, TABLE_EXTRACTION,
                          DOCUMENT_STRUCTURE_PARSE)
EXPLANATION_CONTRACT = TEMPLATE_CAPABILITY.output_contract
DRSA_CONTRACT = DRSA.capability.output_contract
SCHEMA_CHECKER = "intelligence.schema.v1"
REFERENCE_CHECKER = "intelligence.reference.v1"
_EXPLANATION_KEYS = frozenset({"schema", "label", "authoritative", "semantic_authority", "source_result_sha256",
                               "source_algorithm_id", "explainer", "explainer_adapter_id", "router_selection_hash",
                               "facts_hash", "text"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WiringError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvocationScope:
    """An OPTIONAL operator cross-check of the task being executed (CR-16b H-06: never the source of truth).

    The invocation a hook matches comes from the executor (``current_dispatch()``). ``bind(task)`` around
    ``executor.execute(plan, task)`` only adds a check: a hook that sees a bound task other than the one the
    executor is running fails. The value lives in a ``ContextVar`` (one binding per thread or context;
    nesting is refused). Binding nothing is fine."""

    def __init__(self) -> None:
        self._current: ContextVar[TaskContract | None] = ContextVar("pados_pipeline_invocation", default=None)

    @contextmanager
    def bind(self, task: TaskContract) -> Iterator[TaskContract]:
        if self._current.get() is not None:
            raise WiringError("PIPELINE_INVOCATION_ALREADY_BOUND")
        bound = validated_task(task)
        previous = self._current.set(bound)
        try:
            yield bound
        finally:
            self._current.reset(previous)

    def current(self) -> TaskContract | None:
        return self._current.get()


@dataclass(frozen=True)
class PipelineWiring:
    """The trusted state the caller builds the fabric planner and executor from."""

    registry: CapabilityRegistry
    templates: TemplateRegistry
    verifier: VerificationEngine
    planner_config: PlannerConfig
    adapters: Mapping[str, CapabilityAdapter]  # installed implementations (operator side; never given to adapters)
    invocations: InvocationScope  # optional cross-check; hooks read the executor's dispatch context


def capability_catalog(*, include_model_explanation: bool = True) -> tuple[CapabilityDescriptor, ...]:
    """Every capability the pipeline wiring registers (descriptors are metadata, never authority)."""
    catalog = [*(OCR_CAPABILITIES[cid] for cid in OCR_STAGE_CAPABILITIES), EXTRACTION_CAPABILITY,
               RETRIEVAL_CAPABILITY, EMBEDDING_CAPABILITY, RERANK_CAPABILITY, DRSA.capability, TEMPLATE_CAPABILITY,
               REPORT_CAPABILITY]
    if include_model_explanation:
        catalog.append(explanation_capability())
    return tuple(catalog)


def production_adapters(*, include_model_explanation: bool = True,
                        ledger: ExecutionLedgerReader | None = None) -> tuple[CapabilityAdapter, ...]:
    """The adapters an operator installs. Contract-only backends are included and stay unavailable; the
    report adapter is unavailable without a read-only, anchored execution ledger reader."""
    adapters: list[CapabilityAdapter] = [
        DocumentPreprocessAdapter(), PdfTextLayerAdapter(), LineLayoutAdapter(), WhitespaceTableAdapter(),
        HeuristicStructureAdapter(), TableDatasetAdapter(), LexicalRetrievalAdapter(), embedding_adapter(),
        reranker_adapter(), AlgorithmAdapter(DRSA), TemplateExplanationAdapter(), PipelineReportAdapter(ledger)]
    if include_model_explanation:
        adapters.append(ModelExplanationAdapter())  # no ExplanationGenerator wired: unavailable
    return tuple(adapters)


def register_production_adapter(registry: CapabilityRegistry, adapter: CapabilityAdapter, *, operator: str) -> str:
    """Install and register one adapter for the capability it declares; test-only adapters are refused."""
    descriptor = adapter.descriptor
    if getattr(adapter, "production_eligible", True) is not True or descriptor.backend == "fixture":
        raise WiringError("PIPELINE_ADAPTER_NOT_PRODUCTION_ELIGIBLE")
    adapter_id = registry.install(adapter, operator=operator)
    registry.register_adapter(adapter_id, capability_id=descriptor.capability_id, operator=operator)
    return adapter_id


def wire_pipeline(provenance: InputProvenance, *, operator: str, clock: Callable[[], datetime] | None = None,
                  include_model_explanation: bool = True, advice_enabled: bool = False,
                  ledger: ExecutionLedgerReader | None = None) -> PipelineWiring:
    """``ledger`` is a READ-ONLY, anchored reader (``FileExecutionLedger.reader()``) of the ledger the
    operator's executor writes (``CapabilityExecutor(..., ledger=<the writer>)``); the report adapter reads it.
    The writer itself is refused (``EXECUTION_LEDGER_READER_REQUIRED``). Without an anchored reader the report
    capability is unavailable."""
    registry = CapabilityRegistry(clock=clock)
    for descriptor in capability_catalog(include_model_explanation=include_model_explanation):
        registry.register_capability(descriptor, operator=operator)
    adapters: dict[str, CapabilityAdapter] = {}
    for adapter in production_adapters(include_model_explanation=include_model_explanation, ledger=ledger):
        adapters[register_production_adapter(registry, adapter, operator=operator)] = adapter
    templates = TemplateRegistry()
    templates.register(analysis_template())
    templates.register(report_template())
    schemas, references = ValidatorRegistry(), ValidatorRegistry()
    for contract, check in ((OCR_PREPROCESSED_CONTRACT, preprocessed_output_valid),
                            (OCR_DOCUMENT_CONTRACT, document_output_valid),
                            (EXTRACTION_CAPABILITY.output_contract, dataset_output_valid),
                            (RANKING_CONTRACT, ranking_output_valid),
                            (DRSA_CONTRACT, algorithm_output_valid),
                            (EXPLANATION_CONTRACT, explanation_output_valid),
                            (REPORT_CONTRACT, report_output_valid)):
        schemas.register(contract, SCHEMA_CHECKER, check)
    invocations = InvocationScope()
    references.register(OCR_DOCUMENT_CONTRACT, REFERENCE_CHECKER, document_reference_check(provenance, invocations))
    references.register(DRSA_CONTRACT, REFERENCE_CHECKER, algorithm_reference_check())
    config = PlannerConfig(preferences={DOCUMENT_OCR: (TEXT_LAYER_ADAPTER_ID,)}, advice_enabled=advice_enabled)
    return PipelineWiring(registry, templates, VerificationEngine(schemas=schemas, references=references), config,
                          MappingProxyType(adapters), invocations)


# --- SCHEMA validators -----------------------------------------------------------------------------

Outputs = Sequence[tuple[ArtifactRef, bytes]]


def _single(outputs: Outputs, kind: str) -> bytes | None:
    if len(outputs) != 1 or outputs[0][0].kind != kind or outputs[0][0].media_type != JSON_MEDIA_TYPE:
        return None
    return outputs[0][1]


def preprocessed_output_valid(outputs: Outputs) -> bool:
    payload = _single(outputs, KIND_PREPROCESSED)
    if payload is None:
        return False
    try:
        manifest = PreprocessManifest.from_bytes(payload)
    except OcrContractError:
        return False
    return manifest.to_bytes() == payload


def document_output_valid(outputs: Outputs) -> bool:
    payload = _single(outputs, KIND_DOCUMENT)
    if payload is None:
        return False
    try:
        document = OcrDocument.from_bytes(payload)
    except OcrContractError:
        return False
    return document.to_bytes() == payload and document.semantic_authority == "NONE"


def algorithm_output_valid(outputs: Outputs) -> bool:
    """The result envelope and its reproducibility record, in that order, describing each other."""
    if len(outputs) != 2:
        return False
    (result_ref, result_bytes), (record_ref, record_bytes) = outputs
    if result_ref.kind != RESULT_KIND or record_ref.kind != RECORD_KIND or record_ref.media_type != JSON_MEDIA:
        return False
    try:
        envelope = load_result(result_bytes, result_ref)
        record = load_record(record_bytes)
    except AlgorithmError:
        return False
    return (record.result_hash == result_ref.sha256 and record.algorithm_id == envelope.algorithm_id
            and record.capability_id == envelope.capability_id and record.parameters_hash == envelope.parameters_hash
            and record.dataset_hash == envelope.dataset_hash and envelope.semantic_authority == "NONE")


def explanation_output_valid(outputs: Outputs) -> bool:
    """A labelled, non-authoritative explanation artifact that names a source result hash."""
    payload = _single(outputs, EXPLANATION_KIND)
    if payload is None:
        return False
    try:
        document = parse_json_document(payload, max_bytes=64 * 1024, code="EXPLANATION")
    except AlgorithmError:
        return False
    return (set(document) == _EXPLANATION_KEYS and document["schema"] == EXPLANATION_SCHEMA
            and document["label"] == EXPLANATION_LABEL and document["authoritative"] is False
            and document["semantic_authority"] == "NONE" and isinstance(document["source_result_sha256"], str)
            and _SHA256.fullmatch(document["source_result_sha256"]) is not None
            and isinstance(document["text"], str) and canonical_json(document) == payload)


# --- REFERENCE_CHECK hooks -------------------------------------------------------------------------

def document_reference_check(provenance: InputProvenance, invocations: InvocationScope) -> OutputValidator:
    """The recognized document derives from exactly the ``document`` input of the task the EXECUTOR is running
    (``current_dispatch()``), which is also one of the stage's actual inputs, whose trusted provenance is that
    task's project and the input's declared classification within the task ceiling, and from no fixture
    engine. A caller-bound invocation of another task fails the check."""

    def check(outputs: Outputs) -> bool:
        payload = _single(outputs, KIND_DOCUMENT)
        dispatch = current_dispatch()
        if payload is None or dispatch is None:
            return False
        task, bound = dispatch.task, invocations.current()
        if bound is not None and bound.task_hash != task.task_hash:
            return False  # the operator bound a different task than the executor is running
        try:
            document = OcrDocument.from_bytes(payload)
            item = next(entry for entry in task.inputs if entry.name == DOCUMENT_INPUT)
            resolved = resolved_provenance(provenance, item.ref.sha256)
        except Exception:  # noqa: BLE001 - no input, no resolver answer or a bad document: failed, never a pass
            return False
        return (document.source == item.ref and item.ref in dispatch.inputs
                and resolved.project_id == task.project_id == item.project_id
                and resolved.classification is item.classification
                and task.classification.permits(resolved.classification)
                and all(engine.backend != "fixture" for engine in document.evidence.engines))

    return check


def algorithm_reference_check() -> OutputValidator:
    """The record names the registered DRSA adapter and the template's own request parameters."""
    descriptor = adapter_descriptor(DRSA)
    expected_request = parameters_hash(ALGORITHM_PARAMETERS)

    def check(outputs: Outputs) -> bool:
        if len(outputs) != 2:
            return False
        (result_ref, result_bytes), (_, record_bytes) = outputs
        try:
            record = load_record(record_bytes)
        except AlgorithmError:
            return False
        return (record.algorithm_id == DRSA.algorithm_id and record.algorithm_version == DRSA.algorithm_version
                and record.capability_id == DRSA.capability.capability_id
                and record.adapter_id == descriptor.adapter_id and record.seed is None
                and record.request_parameters_hash == expected_request
                and record.result_hash == sha256_hex(result_bytes) == result_ref.sha256)

    return check
