"""ALGORITHM_FIT backend: correctness, seeded determinism, strict parsing and bounds."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pados.capability.artifacts import ScopedArtifacts
from pados.training.backends import (
    MODEL_SCHEMA,
    AlgorithmFitBackend,
    TrainingBackendError,
    TrainingRunResult,
    default_backends,
    parse_model,
    resolve_backend,
)
from pados.training.contracts import DatasetSnapshot, TrainingContract, TrainingContractError, TrainingKind

from .support import Env, binary_rows, make_contract, make_env, make_snapshot


def _rebuilt(snapshot: DatasetSnapshot, **changes: Any) -> DatasetSnapshot:
    """A correctly hashed snapshot that claims different facts than its members hold."""
    values = {k: getattr(snapshot, k) for k in ("snapshot_id", "schema_ref", "members", "record_count",
                                                "classification", "license_spdx", "provenance", "created_at")}
    values.update(changes)
    return DatasetSnapshot.build(**values)


def _run(env: Env, contract: TrainingContract, dataset: DatasetSnapshot | None = None) -> TrainingRunResult:
    dataset = dataset or env.train
    scoped = ScopedArtifacts(env.store, readable=dataset.members, max_outputs=1)
    return AlgorithmFitBackend().run(contract, dataset, artifacts=scoped)


def test_ols_recovers_the_generating_coefficients(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    result = _run(env, env.contract())
    model = parse_model(env.store.get(result.artifact.ref))
    assert model["schema"] == MODEL_SCHEMA and model["algorithm"] == "OLS"
    assert model["intercept"] == pytest.approx(2.0, abs=0.05)
    assert model["coefficients"] == pytest.approx([3.0, -1.5], abs=0.02)
    assert result.metrics["train_r2"] > 0.999
    assert result.metrics["train_records"] == 80.0
    assert result.semantic_authority == "NONE"


def test_same_seed_same_artifact_hash_across_independent_runs(tmp_path: Path) -> None:
    hashes = []
    for name in ("a", "b"):
        env = make_env(tmp_path / name)
        rows = binary_rows(60, seed=5)
        data = make_snapshot(env.store, [rows], snapshot_id="bin")
        contract = make_contract(data, None, config={"algorithm": "LOGISTIC", "feature_count": 2, "max_iter": 200,
                                                      "learning_rate": 0.5, "l2": 0.001}, seed=42)
        hashes.append(_run(env, contract, data).artifact.ref.sha256)
    assert hashes[0] == hashes[1]


def test_different_seed_is_a_different_artifact(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    data = make_snapshot(env.store, [binary_rows(60, seed=5)], snapshot_id="bin")
    config = {"algorithm": "LOGISTIC", "feature_count": 2, "max_iter": 50, "learning_rate": 0.5}
    first = _run(env, make_contract(data, None, config=config, seed=1), data)
    second = _run(env, make_contract(data, None, config=config, seed=2), data)
    assert first.artifact.ref.sha256 != second.artifact.ref.sha256
    first_model = parse_model(env.store.get(first.artifact.ref))
    assert first_model["seed"] == 1 and first.metrics["train_accuracy"] >= 0.85


def test_artifact_is_canonical_json_bound_to_contract_and_dataset(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    result = _run(env, contract)
    payload = env.store.get(result.artifact.ref)
    model = json.loads(payload)
    assert payload == json.dumps(model, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    assert model["contract_hash"] == contract.contract_hash == result.artifact.contract_hash
    assert model["dataset_hash"] == env.train.content_hash == result.artifact.dataset_hash
    assert model["config_hash"] == contract.config_hash
    assert "features" not in model and "label" not in model


@pytest.mark.parametrize("config, code", [
    ({"algorithm": "OLS", "feature_count": 2, "momentum": 0.9}, "CONFIG_KEY_UNKNOWN"),
    ({"algorithm": "SVM", "feature_count": 2}, "CONFIG_ALGORITHM_INVALID"),
    ({"algorithm": "OLS", "feature_count": 0}, "CONFIG_FEATURE_COUNT_INVALID"),
    ({"algorithm": "OLS", "feature_count": 2, "max_iter": 3}, "CONFIG_KEY_NOT_APPLICABLE"),
    ({"algorithm": "LOGISTIC", "feature_count": 2, "learning_rate": 0.0}, "CONFIG_LEARNING_RATE_INVALID"),
    ({"algorithm": "LOGISTIC", "feature_count": 2, "max_iter": 10_000, "learning_rate": 0.1}, None),
])
def test_config_is_validated_and_bounded(tmp_path: Path, config: dict, code: str | None) -> None:
    env = make_env(tmp_path)
    contract = env.contract(config=config)
    if code is None:
        AlgorithmFitBackend().validate_contract(contract, env.train)
        return
    with pytest.raises(TrainingContractError, match=code):
        AlgorithmFitBackend().validate_contract(contract, env.train)


def test_work_bound_is_enforced(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    big = _rebuilt(env.train, record_count=10_000)
    config = {"algorithm": "LOGISTIC", "feature_count": 2, "max_iter": 10_000, "learning_rate": 0.1}
    with pytest.raises(TrainingContractError, match="CONFIG_WORK_BOUND_EXCEEDED"):
        AlgorithmFitBackend().validate_contract(make_contract(big, None, config=config), big)
    stale = env.train.model_copy(update={"record_count": 10_000})  # bypasses the manifest hash
    with pytest.raises(TrainingContractError, match="SNAPSHOT_INTEGRITY_INVALID"):
        AlgorithmFitBackend().validate_contract(env.contract(config=config), stale)


def test_dataset_parsing_is_strict(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    wrong_width = make_snapshot(env.store, [[{"features": [1.0, 2.0, 3.0], "label": 1.0}] * 3], snapshot_id="w")
    with pytest.raises(TrainingBackendError, match="DATASET_FEATURE_COUNT_MISMATCH"):
        _run(env, make_contract(wrong_width, None), wrong_width)
    extra = make_snapshot(env.store, [[{"features": [1.0, 2.0], "label": 1.0, "note": "x"}] * 3], snapshot_id="e")
    with pytest.raises(TrainingBackendError, match="DATASET_RECORD_INVALID"):
        _run(env, make_contract(extra, None), extra)
    lying = _rebuilt(env.train, record_count=81)
    with pytest.raises(TrainingBackendError, match="DATASET_RECORD_COUNT_MISMATCH"):
        _run(env, make_contract(lying, None), lying)
    non_binary = make_snapshot(env.store, [[{"features": [1.0, 2.0], "label": 2.0}] * 3], snapshot_id="nb")
    with pytest.raises(TrainingBackendError, match="LOGISTIC_LABEL_NOT_BINARY"):
        _run(env, make_contract(non_binary, None, config={"algorithm": "LOGISTIC", "feature_count": 2}), non_binary)


def test_backend_reads_only_authorized_members(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    scoped = ScopedArtifacts(env.store, readable=env.train.members[:1], max_outputs=1)
    with pytest.raises(PermissionError, match="ARTIFACT_NOT_AN_AUTHORIZED_INPUT"):
        AlgorithmFitBackend().run(env.contract(), env.train, artifacts=scoped)


def test_backend_resolution_is_exact_with_no_fallback(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    backends = default_backends()
    assert resolve_backend(backends, env.contract()).kind is TrainingKind.ALGORITHM_FIT
    with pytest.raises(TrainingContractError, match="BACKEND_NOT_INSTALLED"):
        resolve_backend(backends, env.contract(backend_adapter_id="training.unknown"))
    available = {adapter_id for adapter_id, backend in backends.items() if backend.available()}
    assert available == {AlgorithmFitBackend.ADAPTER_ID}
    for backend in backends.values():
        assert backend.descriptor.network_requirement.value == "LOCAL_ONLY"
