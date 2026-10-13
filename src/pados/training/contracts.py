# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable dataset snapshots and hashed training contracts (descriptive; never authority).

A ``DatasetSnapshot`` names its members only by content-addressed ``ArtifactRef``; its ``content_hash`` is
the canonical hash of the member manifest (member hashes, sizes, media types, kinds, schema reference and
record count), so any member change yields a different hash. ``snapshot_hash`` additionally binds the
metadata (classification, license, provenance, creation time). A ``TrainingContract`` binds both hashes,
the base model, a bounded config, the backend adapter id, the code version, the seed, the compute resource
and the license constraints into one ``contract_hash`` that run approvals and provenance are bound to.

Pydantic validation runs at construction only, so every consumer re-checks integrity with
``check_snapshot`` / ``validate_contract`` instead of trusting an object it was handed.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from pados.capability.artifacts import ArtifactRef
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_no_credentials

SNAPSHOT_SCHEMA = "pados.training.dataset-snapshot.v1"
CONTRACT_SCHEMA = "pados.training.contract.v1"
MAX_MEMBERS = 1024
MAX_RECORDS = 10_000_000
MAX_CONFIG_KEYS = 64
MAX_CONFIG_STRING = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONTRACT_REF = re.compile(r"^[a-z][a-z0-9_.-]{2,79}/v[0-9]{1,3}$")
_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{1,119}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
_SPDX = re.compile(r"^[A-Za-z0-9.+-]{1,64}( (AND|OR|WITH) [A-Za-z0-9.+-]{1,64}){0,4}$")
_CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_CONFIG_TEXT = re.compile(r"^[A-Za-z0-9_.:+-]{0,128}$")
SEED_MAX = 2**32 - 1

ConfigValue = StrictBool | StrictInt | StrictFloat | StrictStr


class TrainingContractError(ValueError):
    """Contract or snapshot validation failure with a closed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _pattern(value: str, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(code)
    return value


def _aware_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("TIMESTAMP_NOT_AWARE")
    return value


class TrainingKind(StrEnum):
    LORA_FINETUNE = "LORA_FINETUNE"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    VISION_DETECTION = "VISION_DETECTION"
    ALGORITHM_FIT = "ALGORITHM_FIT"
    EVALUATION_ONLY = "EVALUATION_ONLY"


class DatasetProvenance(BaseModel):
    """Where the snapshot came from: references only, never content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_ref: str  # identifier of the governed source (project artifact set, export id)
    collection_method: str  # closed identifier, e.g. OPERATOR_CURATED, DERIVED
    derived_from: tuple[str, ...] = Field(default=(), max_length=16)  # parent snapshot content hashes

    @field_validator("source_ref", "collection_method")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _pattern(value, _ID, "PROVENANCE_IDENTIFIER_INVALID")

    @field_validator("derived_from")
    @classmethod
    def parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SHA256.fullmatch(item) for item in value) or len(set(value)) != len(value):
            raise ValueError("PROVENANCE_PARENT_INVALID")
        return value


def manifest_hash(*, schema_ref: str, record_count: int, members: tuple[ArtifactRef, ...]) -> str:
    """Canonical hash over the member manifest (order-significant; members are what the backend reads)."""
    return canonical_hash({
        "schema": SNAPSHOT_SCHEMA, "schema_ref": schema_ref, "record_count": record_count,
        "members": [{"sha256": m.sha256, "size_bytes": m.size_bytes, "media_type": m.media_type, "kind": m.kind}
                    for m in members],
    })


class DatasetSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SNAPSHOT_SCHEMA
    snapshot_id: str
    schema_ref: str
    members: tuple[ArtifactRef, ...] = Field(min_length=1, max_length=MAX_MEMBERS)
    record_count: int = Field(ge=1, le=MAX_RECORDS)
    classification: DataClassification
    license_spdx: str
    provenance: DatasetProvenance
    created_at: str
    content_hash: str

    @classmethod
    def build(cls, *, snapshot_id: str, schema_ref: str, members: tuple[ArtifactRef, ...], record_count: int,
              classification: DataClassification, license_spdx: str, provenance: DatasetProvenance,
              created_at: str) -> DatasetSnapshot:
        return cls(snapshot_id=snapshot_id, schema_ref=schema_ref, members=members, record_count=record_count,
                   classification=classification, license_spdx=license_spdx, provenance=provenance,
                   created_at=created_at,
                   content_hash=manifest_hash(schema_ref=schema_ref, record_count=record_count, members=members))

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != SNAPSHOT_SCHEMA:
            raise ValueError("SNAPSHOT_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("snapshot_id")
    @classmethod
    def snapshot_identifier(cls, value: str) -> str:
        return _pattern(value, _ID, "SNAPSHOT_ID_INVALID")

    @field_validator("schema_ref")
    @classmethod
    def schema_reference(cls, value: str) -> str:
        return _pattern(value, _CONTRACT_REF, "SNAPSHOT_SCHEMA_REF_INVALID")

    @field_validator("license_spdx")
    @classmethod
    def license_expression(cls, value: str) -> str:
        return _pattern(value, _SPDX, "LICENSE_SPDX_INVALID")

    @field_validator("created_at")
    @classmethod
    def created(cls, value: str) -> str:
        return _aware_iso(value)

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, value: str) -> str:
        return _pattern(value, _SHA256, "SNAPSHOT_HASH_INVALID")

    @model_validator(mode="after")
    def manifest_bound(self) -> DatasetSnapshot:
        if len({m.sha256 for m in self.members}) != len(self.members):
            raise ValueError("SNAPSHOT_MEMBER_DUPLICATE")
        if self.content_hash != self.expected_content_hash():
            raise ValueError("SNAPSHOT_HASH_MISMATCH")
        return self

    def expected_content_hash(self) -> str:
        return manifest_hash(schema_ref=self.schema_ref, record_count=self.record_count, members=self.members)

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def member_hashes(self) -> frozenset[str]:
        return frozenset(m.sha256 for m in self.members)

    def ref(self) -> SnapshotRef:
        return SnapshotRef(snapshot_id=self.snapshot_id, content_hash=self.content_hash,
                           snapshot_hash=self.snapshot_hash)


def check_snapshot(snapshot: DatasetSnapshot) -> DatasetSnapshot:
    """Re-validate a snapshot handed over by anyone (``model_copy`` bypasses construction checks)."""
    try:
        return DatasetSnapshot.model_validate(snapshot.model_dump(mode="json"))
    except ValueError as exc:
        raise TrainingContractError("SNAPSHOT_INTEGRITY_INVALID") from exc


class SnapshotRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    content_hash: str
    snapshot_hash: str

    @field_validator("snapshot_id")
    @classmethod
    def snapshot_identifier(cls, value: str) -> str:
        return _pattern(value, _ID, "SNAPSHOT_ID_INVALID")

    @field_validator("content_hash", "snapshot_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        return _pattern(value, _SHA256, "SNAPSHOT_HASH_INVALID")

    def matches(self, snapshot: DatasetSnapshot) -> bool:
        return (self.snapshot_id == snapshot.snapshot_id and self.content_hash == snapshot.content_hash
                and self.snapshot_hash == snapshot.snapshot_hash)


class ModelRef(BaseModel):
    """A base model by identity and (optionally) content hash; never a URL or a path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    version: str
    artifact_sha256: str | None = None
    license_spdx: str

    @field_validator("model_id")
    @classmethod
    def model_identifier(cls, value: str) -> str:
        if "//" in value or ".." in value:
            raise ValueError("MODEL_ID_INVALID")
        return _pattern(value, _MODEL_ID, "MODEL_ID_INVALID")

    @field_validator("version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        return _pattern(value, _VERSION, "MODEL_VERSION_INVALID")

    @field_validator("artifact_sha256")
    @classmethod
    def artifact(cls, value: str | None) -> str | None:
        return None if value is None else _pattern(value, _SHA256, "MODEL_ARTIFACT_HASH_INVALID")

    @field_validator("license_spdx")
    @classmethod
    def license_expression(cls, value: str) -> str:
        return _pattern(value, _SPDX, "LICENSE_SPDX_INVALID")


class LicenseConstraints(BaseModel):
    """Which licenses the run may consume, and the license the produced model is declared under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_dataset_spdx: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_backend_spdx: tuple[str, ...] = Field(min_length=1, max_length=16)
    allowed_base_model_spdx: tuple[str, ...] = Field(default=(), max_length=16)
    output_spdx: str

    @field_validator("allowed_dataset_spdx", "allowed_backend_spdx", "allowed_base_model_spdx")
    @classmethod
    def expressions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _pattern(item, _SPDX, "LICENSE_SPDX_INVALID")
        return value

    @field_validator("output_spdx")
    @classmethod
    def output(cls, value: str) -> str:
        return _pattern(value, _SPDX, "LICENSE_SPDX_INVALID")


def _config_pairs(value: Any) -> tuple[tuple[str, Any], ...]:
    items = value.items() if isinstance(value, Mapping) else value
    pairs = tuple((key, item) for key, item in items)
    if len(pairs) > MAX_CONFIG_KEYS:
        raise ValueError("CONFIG_TOO_LARGE")
    seen: set[str] = set()
    for key, item in pairs:
        if not isinstance(key, str) or not _CONFIG_KEY.fullmatch(key) or key in seen:
            raise ValueError("CONFIG_KEY_INVALID")
        seen.add(key)
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("CONFIG_VALUE_INVALID")
        if isinstance(item, int) and not isinstance(item, bool) and abs(item) > 2**53:
            raise ValueError("CONFIG_VALUE_INVALID")
        if isinstance(item, str) and not _CONFIG_TEXT.fullmatch(item):
            raise ValueError("CONFIG_VALUE_INVALID")
        if not isinstance(item, (bool, int, float, str)):
            raise ValueError("CONFIG_VALUE_INVALID")
    assert_no_credentials(dict(pairs))
    return tuple(sorted(pairs))


class TrainingContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONTRACT_SCHEMA
    contract_id: str
    kind: TrainingKind
    base_model: ModelRef | None = None
    dataset: SnapshotRef
    eval_dataset: SnapshotRef | None = None
    config: tuple[tuple[StrictStr, ConfigValue], ...] = ()
    backend_adapter_id: str
    code_version: str  # git commit of the training code
    seed: int = Field(ge=0, le=SEED_MAX)
    compute_resource_ref: str
    classification: DataClassification  # the most sensitive data this run may handle
    license_constraints: LicenseConstraints

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != CONTRACT_SCHEMA:
            raise ValueError("CONTRACT_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("contract_id", "compute_resource_ref")
    @classmethod
    def identifiers(cls, value: str) -> str:
        return _pattern(value, _ID, "CONTRACT_IDENTIFIER_INVALID")

    @field_validator("config", mode="before")
    @classmethod
    def bounded_config(cls, value: Any) -> tuple[tuple[str, Any], ...]:
        return _config_pairs(value)

    @field_validator("backend_adapter_id")
    @classmethod
    def backend_identifier(cls, value: str) -> str:
        return _pattern(value, _ADAPTER_ID, "BACKEND_ADAPTER_ID_INVALID")

    @field_validator("code_version")
    @classmethod
    def commit(cls, value: str) -> str:
        return _pattern(value, _COMMIT, "CODE_VERSION_NOT_A_COMMIT")

    @model_validator(mode="after")
    def shape(self) -> TrainingContract:
        needs_base = self.kind is not TrainingKind.ALGORITHM_FIT
        if needs_base and self.base_model is None:
            raise ValueError("BASE_MODEL_REQUIRED")
        if not needs_base and self.base_model is not None:
            raise ValueError("ALGORITHM_FIT_TAKES_NO_BASE_MODEL")
        if self.eval_dataset is not None and self.eval_dataset.content_hash == self.dataset.content_hash:
            raise ValueError("EVAL_DATASET_NOT_HELD_OUT")
        return self

    def config_dict(self) -> dict[str, Any]:
        return dict(self.config)

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.config_dict())

    @property
    def contract_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def check_contract(contract: TrainingContract) -> TrainingContract:
    try:
        return TrainingContract.model_validate(contract.model_dump(mode="json"))
    except ValueError as exc:
        raise TrainingContractError("CONTRACT_INTEGRITY_INVALID") from exc


def validate_contract(contract: TrainingContract, dataset: DatasetSnapshot, *, backend_kind: TrainingKind,
                      backend_adapter_id: str, backend_license_spdx: str,
                      eval_dataset: DatasetSnapshot | None = None) -> None:
    """Backend-independent checks: integrity, snapshot binding, held-out split, classification, licenses."""
    contract = check_contract(contract)
    dataset = check_snapshot(dataset)
    if not contract.dataset.matches(dataset):
        raise TrainingContractError("DATASET_SNAPSHOT_MISMATCH")
    if (contract.eval_dataset is None) != (eval_dataset is None):
        raise TrainingContractError("EVAL_DATASET_BINDING_MISMATCH")
    if contract.eval_dataset is not None and eval_dataset is not None:
        eval_dataset = check_snapshot(eval_dataset)
        if not contract.eval_dataset.matches(eval_dataset):
            raise TrainingContractError("EVAL_DATASET_BINDING_MISMATCH")
        if eval_dataset.member_hashes & dataset.member_hashes:
            raise TrainingContractError("EVAL_DATASET_NOT_HELD_OUT")
        if not contract.classification.permits(eval_dataset.classification):
            raise TrainingContractError("CLASSIFICATION_EXCEEDS_CONTRACT")
    if contract.kind is not backend_kind or contract.backend_adapter_id != backend_adapter_id:
        raise TrainingContractError("BACKEND_MISMATCH")
    if not contract.classification.permits(dataset.classification):
        raise TrainingContractError("CLASSIFICATION_EXCEEDS_CONTRACT")
    constraints = contract.license_constraints
    if dataset.license_spdx not in constraints.allowed_dataset_spdx or (
            eval_dataset is not None and eval_dataset.license_spdx not in constraints.allowed_dataset_spdx):
        raise TrainingContractError("DATASET_LICENSE_NOT_ALLOWED")
    if backend_license_spdx not in constraints.allowed_backend_spdx:
        raise TrainingContractError("BACKEND_LICENSE_NOT_ALLOWED")
    if contract.base_model is not None and contract.base_model.license_spdx not in constraints.allowed_base_model_spdx:
        raise TrainingContractError("BASE_MODEL_LICENSE_NOT_ALLOWED")
