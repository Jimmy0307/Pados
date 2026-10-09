"""DatasetSnapshot immutability and TrainingContract validation."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pados.enterprise.classification import DataClassification
from pados.training.contracts import (
    DatasetSnapshot,
    LicenseConstraints,
    ModelRef,
    TrainingContract,
    TrainingContractError,
    TrainingKind,
    check_snapshot,
    validate_contract,
)

from .support import LICENSES, Env, linear_rows, make_contract, make_env, make_snapshot


def _validate(env: Env, contract: TrainingContract, *, eval_dataset: DatasetSnapshot | None = None,
              license_spdx: str = "AGPL-3.0-only") -> None:
    validate_contract(contract, env.train, backend_kind=TrainingKind.ALGORITHM_FIT,
                      backend_adapter_id="training.algorithm-fit", backend_license_spdx=license_spdx,
                      eval_dataset=env.held_out if eval_dataset is None else eval_dataset)


def test_snapshot_hash_changes_on_any_member_change(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rows = linear_rows(10, seed=11)
    base = make_snapshot(env.store, [rows], snapshot_id="snap")
    changed_row = [dict(rows[0], label=rows[0]["label"] + 1.0), *rows[1:]]
    variants = [
        make_snapshot(env.store, [changed_row], snapshot_id="snap"),  # one record changed
        make_snapshot(env.store, [rows[:5], rows[5:]], snapshot_id="snap"),  # same records, different members
        make_snapshot(env.store, [rows[:9]], snapshot_id="snap"),  # member removed / count changed
    ]
    for variant in variants:
        assert variant.content_hash != base.content_hash
        assert variant.snapshot_hash != base.snapshot_hash
    same = make_snapshot(env.store, [rows], snapshot_id="snap")
    assert same.content_hash == base.content_hash and same.snapshot_hash == base.snapshot_hash


def test_snapshot_metadata_change_changes_identity_but_not_content(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    rows = [linear_rows(10, seed=11)]
    base = make_snapshot(env.store, rows, snapshot_id="snap")
    confidential = make_snapshot(env.store, rows, snapshot_id="snap", classification=DataClassification.CONFIDENTIAL)
    assert confidential.content_hash == base.content_hash
    assert confidential.snapshot_hash != base.snapshot_hash
    assert not base.ref().matches(confidential)


def test_snapshot_is_frozen_and_stale_hash_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    snapshot = env.train
    with pytest.raises(ValidationError):
        snapshot.record_count = 1  # type: ignore[misc]
    data = snapshot.model_dump(mode="json")
    data["record_count"] = snapshot.record_count - 1
    with pytest.raises(ValidationError, match="SNAPSHOT_HASH_MISMATCH"):
        DatasetSnapshot.model_validate(data)
    tampered = snapshot.model_copy(update={"members": snapshot.members[:1]})  # bypasses validation
    with pytest.raises(TrainingContractError, match="SNAPSHOT_INTEGRITY_INVALID"):
        check_snapshot(tampered)
    duplicate = dict(data, members=[data["members"][0], data["members"][0]], record_count=snapshot.record_count)
    with pytest.raises(ValidationError, match="SNAPSHOT_MEMBER_DUPLICATE"):
        DatasetSnapshot.model_validate(duplicate)


def test_contract_validation_accepts_a_well_formed_contract(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    _validate(env, contract)
    assert len(contract.contract_hash) == 64 and len(contract.config_hash) == 64
    assert contract.config_dict() == {"algorithm": "OLS", "feature_count": 2}


@pytest.mark.parametrize("overrides, code", [
    ({"code_version": "main"}, "CODE_VERSION_NOT_A_COMMIT"),
    ({"seed": -1}, "seed"),
    ({"seed": 2**32}, "seed"),
    ({"config": {"Bad-Key": 1}}, "CONFIG_KEY_INVALID"),
    ({"config": {"x": float("nan")}}, "CONFIG_VALUE_INVALID"),
    ({"config": {"x": "rm -rf"}}, "CONFIG_VALUE_INVALID"),
    ({"config": {"x": [1, 2]}}, "CONFIG_VALUE_INVALID"),
    ({"config": {f"k{i}": i for i in range(65)}}, "CONFIG_TOO_LARGE"),
    ({"backend_adapter_id": "Training Backend"}, "BACKEND_ADAPTER_ID_INVALID"),
    ({"compute_resource_ref": "../gpu"}, "CONTRACT_IDENTIFIER_INVALID"),
    ({"kind": TrainingKind.LORA_FINETUNE}, "BASE_MODEL_REQUIRED"),
    ({"base_model": ModelRef(model_id="org/base", version="1", license_spdx="Apache-2.0")},
     "ALGORITHM_FIT_TAKES_NO_BASE_MODEL"),
])
def test_contract_shape_is_validated(tmp_path: Path, overrides: dict, code: str) -> None:
    env = make_env(tmp_path)
    with pytest.raises(ValidationError, match=code):
        env.contract(**overrides)


def test_config_values_cannot_carry_credentials(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    shaped = "s" + "k-" + "A" * 24  # assembled at runtime: credential-shaped value
    with pytest.raises((ValidationError, ValueError)):
        env.contract(config={"algorithm": "OLS", "feature_count": 2, "note": shaped})


def test_model_ref_refuses_urls_and_paths() -> None:
    for model_id in ("https:" + "//host/model", "../model", "/abs/model"):
        with pytest.raises(ValidationError):
            ModelRef(model_id=model_id, version="1", license_spdx="Apache-2.0")


def test_contract_is_bound_to_the_exact_snapshots(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    other = make_snapshot(env.store, [linear_rows(12, seed=99)], snapshot_id="train-1")
    with pytest.raises(TrainingContractError, match="DATASET_SNAPSHOT_MISMATCH"):
        validate_contract(env.contract(), other, backend_kind=TrainingKind.ALGORITHM_FIT,
                          backend_adapter_id="training.algorithm-fit", backend_license_spdx="AGPL-3.0-only",
                          eval_dataset=env.held_out)
    with pytest.raises(TrainingContractError, match="EVAL_DATASET_BINDING_MISMATCH"):
        _validate(env, env.contract(), eval_dataset=other)
    with pytest.raises(TrainingContractError, match="BACKEND_MISMATCH"):
        validate_contract(env.contract(), env.train, backend_kind=TrainingKind.EMBEDDING,
                          backend_adapter_id="training.algorithm-fit", backend_license_spdx="AGPL-3.0-only",
                          eval_dataset=env.held_out)


def test_eval_dataset_must_be_held_out(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(ValidationError, match="EVAL_DATASET_NOT_HELD_OUT"):
        make_contract(env.train, env.train)
    overlapping = make_snapshot(env.store, [linear_rows(40, seed=1), linear_rows(5, seed=50)], snapshot_id="ho")
    with pytest.raises(TrainingContractError, match="EVAL_DATASET_NOT_HELD_OUT"):
        _validate(env, make_contract(env.train, overlapping), eval_dataset=overlapping)


def test_classification_and_license_constraints(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(TrainingContractError, match="CLASSIFICATION_EXCEEDS_CONTRACT"):
        _validate(env, env.contract(classification=DataClassification.PUBLIC))
    with pytest.raises(TrainingContractError, match="BACKEND_LICENSE_NOT_ALLOWED"):
        _validate(env, env.contract(), license_spdx="GPL-3.0-only")
    narrow = LicenseConstraints(allowed_dataset_spdx=("MIT",), allowed_backend_spdx=("AGPL-3.0-only",),
                                output_spdx="Apache-2.0")
    with pytest.raises(TrainingContractError, match="DATASET_LICENSE_NOT_ALLOWED"):
        _validate(env, env.contract(license_constraints=narrow))
    assert LICENSES.output_spdx == "Apache-2.0"
