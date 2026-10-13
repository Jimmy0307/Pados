# SPDX-License-Identifier: AGPL-3.0-only
"""Algorithm specs and descriptors (installed metadata only: never registration, authorization or selection).

Each spec binds one capability descriptor to its algorithm id/version, its parameter and result schemas and
its pure compute function. Contract-only methods have ``compute = None``; their adapters are unavailable.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Level,
    LicenseInfo,
    LifecycleState,
    Modality,
    Profiles,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms import drsa, fcm, mcdm
from pados.packs.algorithms.contracts import AlgorithmError, Dataset, canonical_digest, validated
from pados.packs.algorithms.meter import Deadline

DATASET_CONTRACT = "algorithms.dataset/v1"
REPLAYABLE = (VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY, VerificationMethod.REFERENCE_CHECK)

class Compute(Protocol):
    """A pure algorithm; the optional ``deadline`` can only abort it, never change its result."""

    def __call__(self, dataset: Dataset, parameters: Any, /, *,
                 deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]: ...


@dataclass(frozen=True)
class AlgorithmSpec:
    algorithm_id: str
    algorithm_version: str
    capability: CapabilityDescriptor
    parameters_model: type[BaseModel]
    result_model: type[BaseModel]
    compute: Compute | None  # None: contract only (adapter unavailable)

    def parse_parameters(self, raw: Mapping[str, Any]) -> BaseModel:
        if not isinstance(raw, Mapping):
            raise AlgorithmError("ALGORITHM_PARAMETERS_INVALID")
        return validated(self.parameters_model, dict(raw), "ALGORITHM_PARAMETERS_INVALID")

    def parse_result(self, result: Mapping[str, Any], code: str = "ALGORITHM_RESULT_SCHEMA_VIOLATION") -> BaseModel:
        """The method's own result schema (consumers validate before reading any field)."""
        return validated(self.result_model, result, code)

    def parameters_hash(self, parameters: BaseModel) -> str:
        """Hash of the normalised parameters (defaults included)."""
        return canonical_digest(parameters.model_dump(mode="json"))

    def run(self, dataset: Dataset, parameters: BaseModel, *,
            deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
        if self.compute is None:
            raise AlgorithmError("ALGORITHM_CONTRACT_ONLY")
        result, metrics = self.compute(dataset, parameters, deadline=deadline)
        self.parse_result(result)
        return result, metrics


def _capability(capability_id: str, operation: str, output: str, *, lifecycle: LifecycleState = LifecycleState.ACTIVE,
                description: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id, version="1", family=CapabilityFamily.ALGORITHM, operation=operation,
        input_contract=DATASET_CONTRACT, output_contract=output, determinism=Determinism.DETERMINISTIC,
        modalities=(Modality.TABLE, Modality.STRUCTURED), max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY,
        profiles=Profiles(latency=Level.LOW, cost=Level.LOW, quality=Level.HIGH),
        verification=REPLAYABLE if lifecycle is LifecycleState.ACTIVE else (VerificationMethod.SCHEMA,),
        lifecycle=lifecycle, description=description)


def _run_drsa(dataset: Dataset, parameters: Any, /, *,
              deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    if not isinstance(parameters, drsa.DrsaParameters):
        raise AlgorithmError("ALGORITHM_PARAMETERS_INVALID")
    return drsa.compute(dataset, parameters, deadline=deadline)


def _run_fcm(dataset: Dataset, parameters: Any, /, *,
              deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    if not isinstance(parameters, fcm.FcmParameters):
        raise AlgorithmError("ALGORITHM_PARAMETERS_INVALID")
    return fcm.compute(dataset, parameters, deadline=deadline)


def _run_dematel(dataset: Dataset, parameters: Any, /, *,
              deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    if not isinstance(parameters, mcdm.DematelParameters):
        raise AlgorithmError("ALGORITHM_PARAMETERS_INVALID")
    return mcdm.compute_dematel(dataset, parameters, deadline=deadline)


DRSA = AlgorithmSpec(
    drsa.ALGORITHM_ID, drsa.ALGORITHM_VERSION,
    _capability("ROUGH_SET_RULE_LEARNING", "INDUCE_DOMINANCE_RULES", "algorithms.drsa-result/v1",
                description="DRSA approximations and DOMLEM certain decision rules"),
    drsa.DrsaParameters, drsa.DrsaResult, _run_drsa)
FCM = AlgorithmSpec(
    fcm.ALGORITHM_ID, fcm.ALGORITHM_VERSION,
    _capability("FUZZY_COGNITIVE_MAP", "INFER_CONCEPT_STATES", "algorithms.fcm-result/v1",
                description="Fuzzy cognitive map inference to a fixed point or limit cycle"),
    fcm.FcmParameters, fcm.FcmResult, _run_fcm)
DEMATEL = AlgorithmSpec(
    mcdm.DEMATEL_ID, mcdm.DEMATEL_VERSION,
    _capability("MULTI_CRITERIA_DECISION_DEMATEL", "ANALYZE_CAUSAL_RELATIONS", "algorithms.mcdm-result/v1",
                description="DEMATEL total relation, prominence and relation (exact rational inverse)"),
    mcdm.DematelParameters, mcdm.DematelResult, _run_dematel)
DANP = AlgorithmSpec(
    "mcdm.danp", "0",
    _capability("MULTI_CRITERIA_DECISION_DANP", "DERIVE_INFLUENTIAL_WEIGHTS", "algorithms.mcdm-result/v1",
                lifecycle=LifecycleState.EXPERIMENTAL, description="DEMATEL-based ANP weights (contract only)"),
    mcdm.DanpParameters, mcdm.DanpResult, None)
CHOQUET = AlgorithmSpec(
    "mcdm.choquet", "0",
    _capability("MULTI_CRITERIA_DECISION_CHOQUET", "AGGREGATE_WITH_CAPACITY", "algorithms.mcdm-result/v1",
                lifecycle=LifecycleState.EXPERIMENTAL, description="Discrete Choquet integral (contract only)"),
    mcdm.ChoquetParameters, mcdm.ChoquetResult, None)
ROV = AlgorithmSpec(
    "mcdm.rov", "0",
    _capability("MULTI_CRITERIA_DECISION_ROV", "RANGE_OF_VALUE", "algorithms.mcdm-result/v1",
                lifecycle=LifecycleState.EXPERIMENTAL, description="Range of value utility bounds (contract only)"),
    mcdm.RovParameters, mcdm.RovResult, None)

ALGORITHMS: Mapping[str, AlgorithmSpec] = {spec.algorithm_id: spec for spec in (DRSA, FCM, DEMATEL, DANP, CHOQUET, ROV)}


def adapter_descriptor(spec: AlgorithmSpec) -> AdapterDescriptor:
    """PADOS-owned pure-Python implementation: local only, no engine, no network."""
    return AdapterDescriptor(
        adapter_id=f"algorithms.{spec.algorithm_id}", capability_id=spec.capability.capability_id,
        version=spec.algorithm_version, backend="python", determinism=Determinism.DETERMINISTIC,
        max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY,
        license=LicenseInfo(spdx="AGPL-3.0-only", engine_version="n/a"), lifecycle=spec.capability.lifecycle)
