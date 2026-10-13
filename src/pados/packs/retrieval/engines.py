# SPDX-License-Identifier: AGPL-3.0-only
"""Contract-only retrieval backends: embedding retrieval and reranking (ENGINE NOT INSTALLED).

Dense-embedding retrieval and cross-encoder reranking are separate capabilities from lexical
``PASSAGE_RETRIEVAL``: their ranking semantics differ, so a grant names exactly the one it authorizes and
no plan can swap one for another (``FallbackPolicy.NONE``). PADOS bundles, installs and downloads no
embedding or reranker engine or model, so these adapters are descriptors only: ``available()`` is always
False and ``execute`` refuses with ``ENGINE_NOT_INSTALLED``. Their license and resource needs are declared
(model weights carry model-specific licenses; an accelerator is required), so policy and resource-aware
planning can explain why they are not feasible on a given machine.
"""
from __future__ import annotations

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Distribution,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    LifecycleState,
    Modality,
    ResourceRequirement,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr.contracts import OCR_DOCUMENT_CONTRACT
from pados.packs.retrieval.lexical import RANKING_CONTRACT

PASSAGE_RETRIEVAL_EMBEDDING = "PASSAGE_RETRIEVAL_EMBEDDING"
PASSAGE_RERANK = "PASSAGE_RERANK"
EMBEDDING_ADAPTER_ID = "retrieval.embedding-engine"
RERANKER_ADAPTER_ID = "retrieval.reranker-engine"
ENGINE_NOT_INSTALLED = "ENGINE_NOT_INSTALLED"
ACCELERATED = ResourceRequirement(cpu_cores=2, ram_mb=4096, gpu_required=True, vram_mb=2048,
                                  accelerators=("CUDA", "ROCM", "METAL", "DIRECTML"))


def _capability(capability_id: str, operation: str, input_contract: str, description: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, version="1", family=CapabilityFamily.RETRIEVAL, operation=operation,
        input_contract=input_contract, output_contract=RANKING_CONTRACT, determinism=Determinism.PROBABILISTIC,
        modalities=(Modality.TEXT, Modality.EMBEDDING), max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, verification=(VerificationMethod.SCHEMA,),
        failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE, lifecycle=LifecycleState.EXPERIMENTAL,
        description=description)


EMBEDDING_CAPABILITY = _capability(PASSAGE_RETRIEVAL_EMBEDDING, "RANK_PASSAGES_BY_EMBEDDING", OCR_DOCUMENT_CONTRACT,
                                   "Dense-embedding passage ranking (contract only, engine not installed)")
RERANK_CAPABILITY = _capability(PASSAGE_RERANK, "RERANK_PASSAGES", RANKING_CONTRACT,
                                "Cross-encoder reranking of a passage ranking (contract only)")


def _descriptor(adapter_id: str, capability_id: str, backend: str) -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=adapter_id, capability_id=capability_id, version="0", backend=backend,
        determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, resources=ACCELERATED,
        license=LicenseInfo(spdx="LicenseRef-Model-Specific", engine_version="n/a",
                            distribution=Distribution.RUNTIME_OPTIONAL),
        lifecycle=LifecycleState.EXPERIMENTAL)


class ContractOnlyRetrievalAdapter:
    """A declared backend with no engine: never available, never executes."""

    def __init__(self, descriptor: AdapterDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return False

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        raise CapabilityAdapterError(ENGINE_NOT_INSTALLED)


def embedding_adapter() -> ContractOnlyRetrievalAdapter:
    return ContractOnlyRetrievalAdapter(_descriptor(EMBEDDING_ADAPTER_ID, PASSAGE_RETRIEVAL_EMBEDDING,
                                                    "embedding-engine"))


def reranker_adapter() -> ContractOnlyRetrievalAdapter:
    return ContractOnlyRetrievalAdapter(_descriptor(RERANKER_ADAPTER_ID, PASSAGE_RERANK, "reranker-engine"))
