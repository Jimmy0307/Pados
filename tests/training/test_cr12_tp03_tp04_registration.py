"""CR-12 TP-03 (evaluation is recomputed, never trusted) and TP-04 (full registration binding)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pados.capability.artifacts import ScopedArtifacts
from pados.capability.model import LicenseInfo
from pados.enterprise.classification import DataClassification
from pados.training.backends import (
    CONTRACT_ONLY_SPECS,
    AlgorithmFitBackend,
    ContractOnlyBackend,
    ModelArtifact,
    TrainingBackend,
    TrainingRunResult,
    default_backends,
)
from pados.training.contracts import (
    DatasetProvenance,
    DatasetSnapshot,
    ModelRef,
    TrainingContract,
    TrainingKind,
)
from pados.training.evaluation import EvaluationReport
from pados.training.jobs import execute_job
from pados.training.registry import ModelRegistry, Provenance, RegistryError, build_candidate

from .support import CREATED, Env, linear_rows, make_contract, make_env, make_snapshot, rechain


def _fake_report(env: Env, candidate: Any, **changes: Any) -> EvaluationReport:
    genuine = candidate.evaluations[0]
    values = dict(genuine.model_dump(), report_id="invented", **changes)
    return EvaluationReport.model_validate(values)


def _with_reports(candidate: Any, *reports: EvaluationReport) -> Any:
    return type(candidate).model_validate(dict(candidate.model_dump(mode="json"),
                                               evaluations=[r.model_dump(mode="json") for r in reports]))


def test_cr12_invented_metrics_are_not_reproduced(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    forged = _with_reports(candidate, _fake_report(env, candidate, metrics={"mse": 0.0, "r2": 1.0, "records": 30.0}))
    with pytest.raises(RegistryError, match="EVALUATION_NOT_REPRODUCED"):
        env.register(forged, contract)
    env.register(candidate, contract)  # the trusted recomputation matches the genuine report


def test_cr12_report_for_an_undesignated_snapshot_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    other = make_snapshot(env.store, [linear_rows(25, seed=61)], snapshot_id="heldout-other")
    swapped = _with_reports(candidate, _fake_report(env, candidate, dataset=other.ref()))
    with pytest.raises(RegistryError, match="EVALUATION_NOT_REPRODUCED"):
        env.register(swapped, contract)
    with pytest.raises(RegistryError, match="EVAL_DATASET_BINDING_MISMATCH"):
        env.register(candidate, contract, eval_dataset=other)


def test_cr12_evaluations_without_a_designated_snapshot_are_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = make_contract(env.train, None)
    candidate, _ = env.candidate(evaluate=False, contract=contract)
    with_report = _with_reports(candidate, EvaluationReport(
        report_id="invented", model_artifact_sha256=candidate.artifact.sha256, dataset=env.held_out.ref(),
        training_dataset_hash=env.train.content_hash, metrics={"r2": 1.0}, evaluator_id="x", evaluator_version="1",
        created_at=CREATED))
    with pytest.raises(RegistryError, match="EVALUATION_DATASET_NOT_DESIGNATED"):
        env.register(with_report, contract, eval_dataset=None)
    env.register(candidate, contract, eval_dataset=None)
    with pytest.raises(RegistryError, match="PROMOTION_REQUIRES_EVALUATION"):
        env.registry.request_promotion(candidate, requested_by="bob.engineer")


def test_cr12_repackaged_training_records_fail_trusted_recomputation(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    repacked = make_snapshot(env.store, [linear_rows(40, seed=1)[:9] + linear_rows(6, seed=71)], snapshot_id="rp")
    contract = make_contract(env.train, repacked)
    env.approved_job(contract)
    result = execute_job(env.jobs, "job-00000001", contract=contract, dataset=env.train, backend=env.backend,
                         store=env.store, eval_dataset=repacked)
    with pytest.raises(RegistryError, match="EVALUATION_RECORDS_OVERLAP_TRAINING"):
        build_candidate(candidate_id="cand", job=env.jobs.get("job-00000001"), result=result, contract=contract,
                        dataset=env.train, backend=env.backend, store=env.store, eval_dataset=repacked,
                        created_at=CREATED)
    candidate = build_candidate(candidate_id="cand", job=env.jobs.get("job-00000001"), result=result,
                                contract=contract, dataset=env.train, backend=env.backend, store=env.store,
                                created_at=CREATED)
    claimed = _with_reports(candidate, EvaluationReport(
        report_id="claimed", model_artifact_sha256=candidate.artifact.sha256, dataset=repacked.ref(),
        training_dataset_hash=env.train.content_hash, metrics={"r2": 0.99}, evaluator_id="training.eval.algorithm-fit",
        evaluator_version="1", created_at=CREATED))
    with pytest.raises(RegistryError, match="EVALUATION_RECORDS_OVERLAP_TRAINING"):
        env.register(claimed, contract, eval_dataset=repacked)


class _RunnableEmbedding:
    """A trusted-registry test backend for a non-ALGORITHM_FIT kind (its evaluation is unverifiable)."""

    def __init__(self) -> None:
        self._inner = ContractOnlyBackend(TrainingKind.EMBEDDING, CONTRACT_ONLY_SPECS[TrainingKind.EMBEDDING])

    @property
    def descriptor(self) -> Any:
        return self._inner.descriptor

    @property
    def kind(self) -> TrainingKind:
        return TrainingKind.EMBEDDING

    def available(self) -> bool:
        return True

    def validate_contract(self, contract: TrainingContract, dataset: DatasetSnapshot) -> None:
        self._inner.validate_contract(contract, dataset)

    def run(self, contract: TrainingContract, dataset: DatasetSnapshot, *,
            artifacts: ScopedArtifacts) -> TrainingRunResult:
        ref = artifacts.put(b'{"weights":"opaque"}', media_type="application/json", kind="training.model")
        return TrainingRunResult(ModelArtifact(ref, self.descriptor.adapter_id, contract.contract_hash,
                                               dataset.content_hash), {"train_loss": 0.1})


def test_cr12_non_algorithm_fit_evaluation_is_unverifiable(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    backend = _RunnableEmbedding()
    backends: dict[str, TrainingBackend] = {**default_backends(), backend.descriptor.adapter_id: backend}
    registry = ModelRegistry(tmp_path / "registry.jsonl", jobs=env.jobs, verifier=env.verifier, backends=backends,
                             store=env.store, clock=env.clock, anchor=env.anchor)
    pairs = DatasetSnapshot.build(
        snapshot_id="pairs", schema_ref="training.text-pairs/v1", members=env.train.members, record_count=80,
        classification=DataClassification.INTERNAL, license_spdx="Apache-2.0",
        provenance=DatasetProvenance(source_ref="project.fixture", collection_method="OPERATOR_CURATED"),
        created_at=CREATED)
    contract = make_contract(pairs, None, kind=TrainingKind.EMBEDDING, backend_adapter_id=backend.descriptor.adapter_id,
                             base_model=ModelRef(model_id="org/encoder", version="1", license_spdx="Apache-2.0"),
                             config={"epochs": 1, "batch_size": 8, "learning_rate": 0.0001})
    env.approved_job(contract)
    result = execute_job(env.jobs, "job-00000001", contract=contract, dataset=pairs, backend=backend, store=env.store)
    candidate = build_candidate(candidate_id="cand-emb", job=env.jobs.get("job-00000001"), result=result,
                                contract=contract, dataset=pairs, backend=backend, store=env.store, created_at=CREATED)
    claimed = _with_reports(candidate, EvaluationReport(
        report_id="claimed", model_artifact_sha256=candidate.artifact.sha256, dataset=env.held_out.ref(),
        training_dataset_hash=pairs.content_hash, metrics={"accuracy": 0.99}, evaluator_id="x",
        evaluator_version="1", created_at=CREATED))
    with pytest.raises(RegistryError, match="PROMOTION_EVALUATION_UNVERIFIABLE"):
        registry.register(claimed, contract=contract, dataset=pairs, registered_by="bob.engineer")
    registry.register(candidate, contract=contract, dataset=pairs, registered_by="bob.engineer")
    with pytest.raises(RegistryError, match="PROMOTION_EVALUATION_UNVERIFIABLE"):
        registry.request_promotion(candidate, requested_by="bob.engineer")


def test_cr12_substituted_snapshot_metadata_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    relabelled = DatasetSnapshot.build(**{**{k: getattr(env.train, k) for k in (
        "snapshot_id", "schema_ref", "members", "record_count", "license_spdx", "provenance", "created_at")},
        "classification": DataClassification.PUBLIC})
    assert relabelled.content_hash == env.train.content_hash  # same members, different metadata
    substituted = type(candidate).model_validate(dict(candidate.model_dump(mode="json"), provenance=Provenance.derive(
        job_id=candidate.provenance.job_id, contract=contract, dataset=relabelled,
        backend_license_spdx="AGPL-3.0-only").model_dump(mode="json")))
    with pytest.raises(RegistryError, match="DATASET_SNAPSHOT_MISMATCH"):
        env.register(substituted, contract, dataset=relabelled)
    relabelled_eval = DatasetSnapshot.build(**{**{k: getattr(env.held_out, k) for k in (
        "snapshot_id", "schema_ref", "members", "record_count", "classification", "provenance", "created_at")},
        "license_spdx": "Apache-2.0"})
    with pytest.raises(RegistryError, match="EVAL_DATASET_BINDING_MISMATCH"):
        env.register(candidate, contract, eval_dataset=relabelled_eval)


class _RelicensedAlgorithmFit(AlgorithmFitBackend):
    """The operator's trusted registry says this backend is GPL (not allowed); the caller cannot claim otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self._descriptor = self._descriptor.model_copy(update={"license": LicenseInfo(spdx="GPL-3.0-only")})


def test_cr12_backend_license_comes_from_the_trusted_registry(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    relicensed = ModelRegistry(tmp_path / "registry-agpl.jsonl", jobs=env.jobs, verifier=env.verifier,
                               backends={AlgorithmFitBackend.ADAPTER_ID: _RelicensedAlgorithmFit()},
                               store=env.store, clock=env.clock, anchor=env.anchor, log_name="model-registry-agpl")
    with pytest.raises(RegistryError, match="BACKEND_LICENSE_NOT_ALLOWED"):
        relicensed.register(candidate, contract=contract, dataset=env.train, eval_dataset=env.held_out,
                            registered_by="bob.engineer")
    missing = ModelRegistry(tmp_path / "registry-empty.jsonl", jobs=env.jobs, verifier=env.verifier, backends={},
                            store=env.store, clock=env.clock, anchor=env.anchor, log_name="model-registry-empty")
    with pytest.raises(RegistryError, match="BACKEND_NOT_INSTALLED"):
        missing.register(candidate, contract=contract, dataset=env.train, eval_dataset=env.held_out,
                         registered_by="bob.engineer")
    with pytest.raises(TypeError):
        env.registry.register(candidate, contract=contract, dataset=env.train,  # type: ignore[call-arg]
                              backend_license_spdx="AGPL-3.0-only", registered_by="bob.engineer")


def test_cr12_artifact_must_be_the_one_the_contract_produced(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    candidate, contract = env.candidate()
    stray = env.store.put(b"not a model", media_type="application/json", kind="training.model")
    other_job, other_contract, other_result = env.trained(
        env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 2.0}), job_id="job-00000002")

    def point_job_at(sha: str) -> Callable[[list[dict[str, Any]]], None]:
        def mutate(entries: list[dict[str, Any]]) -> None:
            completed = next(e for e in entries if e["job_id"] == "job-00000001" and e["event"] == "COMPLETED")
            completed["artifact_sha256"] = sha
        return mutate

    for ref, code in ((stray, "ARTIFACT_UNREADABLE"), (other_result.artifact.ref, "ARTIFACT_NOT_FROM_CONTRACT")):
        rechain(tmp_path / "jobs.jsonl", point_job_at(ref.sha256), anchor_too=True)
        swapped = type(candidate).model_validate(dict(candidate.model_dump(mode="json"), evaluations=[],
                                                      artifact=ref.model_dump()))
        with pytest.raises(RegistryError, match=code):
            env.register(swapped, contract)
    assert other_job and other_contract
