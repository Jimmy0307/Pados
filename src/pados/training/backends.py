# SPDX-License-Identifier: AGPL-3.0-only
"""Training backends: one real lightweight deterministic backend, the rest contract-only.

``AlgorithmFitBackend`` fits ordinary least squares (optionally ridge-regularized; normal equations solved
by Gaussian elimination with partial pivoting) or binary logistic regression (full-batch gradient descent
with seeded initialization) in pure Python on small tabular snapshots. It reads members only through the
stage-scoped ``ScopedArtifacts`` facade and writes one canonical-JSON model artifact. Coefficients are
rounded to 12 significant digits so the artifact hash is stable for the same contract and seed.

LoRA fine-tuning, embedding and reranker training and vision detection are adapter CONTRACTS only: their
descriptors record the third-party engine license with ``Distribution.RUNTIME_OPTIONAL``, ``available()``
is always False in V1 and ``run`` fails closed. A backend output is a candidate artifact
(``semantic_authority = NONE``), never a registered or authorized adapter.
"""
from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from pados.capability.artifacts import ArtifactRef, ScopedArtifacts
from pados.capability.model import (
    AdapterDescriptor,
    Determinism,
    Distribution,
    LicenseInfo,
    LifecycleState,
    ResourceRequirement,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.training.contracts import (
    DatasetSnapshot,
    TrainingContract,
    TrainingContractError,
    TrainingKind,
    check_snapshot,
)

MODEL_SCHEMA = "pados.training.model.algorithm-fit/v1"
MODEL_MEDIA_TYPE = "application/json"
MODEL_KIND = "training.model"
TABULAR_SCHEMA_REF = "training.tabular/v1"
TABULAR_MEDIA_TYPES = frozenset({"application/x-ndjson", "application/jsonl"})
MAX_FEATURES = 64
MAX_FIT_RECORDS = 100_000
MAX_WORK = 50_000_000  # records * features * iterations
MAX_TRAINING_METRICS = 32
_SIGNIFICANT = 12


class TrainingBackendError(RuntimeError):
    """Backend failure with a closed code; never echoes dataset content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _frozen_metrics(metrics: Mapping[str, float]) -> Mapping[str, float]:
    if len(metrics) > MAX_TRAINING_METRICS:
        raise ValueError("TRAINING_METRICS_TOO_MANY")
    cleaned: dict[str, float] = {}
    for key, value in metrics.items():
        if not isinstance(key, str) or not key or len(key) > 64 or isinstance(value, bool):
            raise ValueError("TRAINING_METRIC_INVALID")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("TRAINING_METRIC_INVALID")
        cleaned[key] = number
    return MappingProxyType(cleaned)


@dataclass(frozen=True)
class ModelArtifact:
    ref: ArtifactRef
    backend_adapter_id: str
    contract_hash: str
    dataset_hash: str


@dataclass(frozen=True)
class TrainingRunResult:
    artifact: ModelArtifact
    metrics: Mapping[str, float] = field(default_factory=dict)
    semantic_authority: str = "NONE"

    def __post_init__(self) -> None:
        if self.semantic_authority != "NONE":
            raise ValueError("TRAINING_OUTPUT_CANNOT_CARRY_AUTHORITY")
        object.__setattr__(self, "metrics", _frozen_metrics(self.metrics))


class ArtifactReader(Protocol):
    def get(self, ref: ArtifactRef) -> bytes: ...


class TrainingBackend(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...

    @property
    def kind(self) -> TrainingKind: ...

    def available(self) -> bool:
        """Side-effect free readiness probe."""

    def validate_contract(self, contract: TrainingContract, dataset: DatasetSnapshot) -> None:
        """Raise ``TrainingContractError`` unless the backend can run exactly this contract."""

    def run(self, contract: TrainingContract, dataset: DatasetSnapshot, *,
            artifacts: ScopedArtifacts) -> TrainingRunResult: ...


def _round(value: float) -> float:
    rounded = float(f"{value:.{_SIGNIFICANT}g}")
    return 0.0 if rounded == 0 else rounded


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise TrainingBackendError("DATASET_RECORD_INVALID")
    return float(value)


def load_tabular(dataset: DatasetSnapshot, reader: ArtifactReader, *,
                 feature_count: int) -> list[tuple[list[float], float]]:
    """Strictly parse ``training.tabular/v1`` members: one JSON object per line, {features, label}."""
    if dataset.schema_ref != TABULAR_SCHEMA_REF:
        raise TrainingBackendError("DATASET_SCHEMA_UNSUPPORTED")
    rows: list[tuple[list[float], float]] = []
    for member in dataset.members:
        if member.media_type not in TABULAR_MEDIA_TYPES:
            raise TrainingBackendError("DATASET_MEMBER_MEDIA_TYPE_UNSUPPORTED")
        try:
            text = reader.get(member).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TrainingBackendError("DATASET_RECORD_INVALID") from exc
        for line in text.splitlines():
            if not line.strip():
                continue
            if len(rows) >= min(dataset.record_count, MAX_FIT_RECORDS):
                raise TrainingBackendError("DATASET_RECORD_COUNT_MISMATCH")
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise TrainingBackendError("DATASET_RECORD_INVALID") from exc
            if not isinstance(record, dict) or set(record) != {"features", "label"}:
                raise TrainingBackendError("DATASET_RECORD_INVALID")
            features = record["features"]
            if not isinstance(features, list) or len(features) != feature_count:
                raise TrainingBackendError("DATASET_FEATURE_COUNT_MISMATCH")
            rows.append(([_number(item) for item in features], _number(record["label"])))
    if len(rows) != dataset.record_count:
        raise TrainingBackendError("DATASET_RECORD_COUNT_MISMATCH")
    return rows


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; refuses (near-)singular systems."""
    size = len(vector)
    rows = [list(matrix[i]) + [vector[i]] for i in range(size)]
    scale = max((abs(v) for row in matrix for v in row), default=0.0) or 1.0
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) <= 1e-12 * scale:
            raise TrainingBackendError("DESIGN_MATRIX_SINGULAR")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(column + 1, size):
            factor = rows[r][column] / rows[column][column]
            if factor:
                for c in range(column, size + 1):
                    rows[r][c] -= factor * rows[column][c]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        total = rows[r][size] - sum(rows[r][c] * solution[c] for c in range(r + 1, size))
        solution[r] = total / rows[r][r]
    return solution


def fit_ols(rows: list[tuple[list[float], float]], *, l2: float) -> tuple[float, list[float]]:
    """Least squares with an unpenalized intercept: (X'X + l2*I') b = X'y."""
    width = len(rows[0][0]) + 1
    gram = [[0.0] * width for _ in range(width)]
    moment = [0.0] * width
    for features, label in rows:
        x = [1.0, *features]
        for i in range(width):
            moment[i] += x[i] * label
            for j in range(width):
                gram[i][j] += x[i] * x[j]
    for i in range(1, width):
        gram[i][i] += l2
    beta = _solve(gram, moment)
    return beta[0], beta[1:]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 700.0)))
    e = math.exp(max(z, -700.0))
    return e / (1.0 + e)


def fit_logistic(rows: list[tuple[list[float], float]], *, l2: float, max_iter: int, learning_rate: float,
                 seed: int) -> tuple[float, list[float]]:
    """Binary logistic regression by full-batch gradient descent (order-independent; seeded init)."""
    if any(label not in (0.0, 1.0) for _, label in rows):
        raise TrainingBackendError("LOGISTIC_LABEL_NOT_BINARY")
    rng = random.Random(seed)
    width = len(rows[0][0])
    intercept = rng.uniform(-0.01, 0.01)
    weights = [rng.uniform(-0.01, 0.01) for _ in range(width)]
    count = float(len(rows))
    for _ in range(max_iter):
        grad_b = 0.0
        grad_w = [0.0] * width
        for features, label in rows:
            error = _sigmoid(intercept + sum(w * x for w, x in zip(weights, features, strict=True))) - label
            grad_b += error
            for i in range(width):
                grad_w[i] += error * features[i]
        intercept -= learning_rate * grad_b / count
        weights = [w - learning_rate * (g / count + l2 * w) for w, g in zip(weights, grad_w, strict=True)]
    return intercept, weights


def predict(model: Mapping[str, Any], features: list[float]) -> float:
    linear = float(model["intercept"]) + sum(
        float(w) * x for w, x in zip(model["coefficients"], features, strict=True))
    return _sigmoid(linear) if model["algorithm"] == "LOGISTIC" else linear


def regression_metrics(model: Mapping[str, Any], rows: list[tuple[list[float], float]]) -> dict[str, float]:
    if model["algorithm"] == "LOGISTIC":
        eps = 1e-12
        loss = 0.0
        correct = 0
        for features, label in rows:
            p = min(max(predict(model, features), eps), 1.0 - eps)
            loss -= label * math.log(p) + (1.0 - label) * math.log(1.0 - p)
            correct += int((p >= 0.5) == (label == 1.0))
        return {"log_loss": _round(loss / len(rows)), "accuracy": _round(correct / len(rows)),
                "records": float(len(rows))}
    residual = sum((label - predict(model, features)) ** 2 for features, label in rows)
    mean = sum(label for _, label in rows) / len(rows)
    total = sum((label - mean) ** 2 for _, label in rows)
    r2 = 1.0 - residual / total if total > 0 else (1.0 if residual == 0 else 0.0)
    return {"mse": _round(residual / len(rows)), "r2": _round(r2), "records": float(len(rows))}


def parse_model(payload: bytes) -> dict[str, Any]:
    """Strictly parse an ALGORITHM_FIT model artifact."""
    try:
        model = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TrainingBackendError("MODEL_ARTIFACT_INVALID") from exc
    keys = {"schema", "algorithm", "feature_count", "intercept", "coefficients", "contract_hash", "dataset_hash",
            "config_hash", "seed", "backend_adapter_id"}
    if (not isinstance(model, dict) or set(model) != keys or model["schema"] != MODEL_SCHEMA
            or model["algorithm"] not in {"OLS", "LOGISTIC"} or not isinstance(model["coefficients"], list)
            or len(model["coefficients"]) != model["feature_count"]):
        raise TrainingBackendError("MODEL_ARTIFACT_INVALID")
    for value in (model["intercept"], *model["coefficients"]):
        _number(value)
    return model


def canonical_model_bytes(model: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(model), sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


_FIT_KEYS = {"algorithm", "feature_count", "l2", "max_iter", "learning_rate"}


@dataclass(frozen=True)
class _FitConfig:
    algorithm: str
    feature_count: int
    l2: float
    max_iter: int
    learning_rate: float


def _fit_config(contract: TrainingContract, dataset: DatasetSnapshot) -> _FitConfig:
    config = contract.config_dict()
    if not set(config) <= _FIT_KEYS:
        raise TrainingContractError("CONFIG_KEY_UNKNOWN")
    algorithm = config.get("algorithm")
    feature_count = config.get("feature_count")
    l2 = config.get("l2", 0.0)
    max_iter = config.get("max_iter", 500)
    learning_rate = config.get("learning_rate", 0.1)
    if algorithm not in {"OLS", "LOGISTIC"}:
        raise TrainingContractError("CONFIG_ALGORITHM_INVALID")
    if algorithm == "OLS" and ({"max_iter", "learning_rate"} & set(config)):
        raise TrainingContractError("CONFIG_KEY_NOT_APPLICABLE")
    if not isinstance(feature_count, int) or isinstance(feature_count, bool) or not 1 <= feature_count <= MAX_FEATURES:
        raise TrainingContractError("CONFIG_FEATURE_COUNT_INVALID")
    if isinstance(l2, bool) or not isinstance(l2, (int, float)) or not 0.0 <= float(l2) <= 1e6:
        raise TrainingContractError("CONFIG_L2_INVALID")
    if not isinstance(max_iter, int) or isinstance(max_iter, bool) or not 1 <= max_iter <= 10_000:
        raise TrainingContractError("CONFIG_MAX_ITER_INVALID")
    if (isinstance(learning_rate, bool) or not isinstance(learning_rate, (int, float))
            or not 0.0 < float(learning_rate) <= 10.0):
        raise TrainingContractError("CONFIG_LEARNING_RATE_INVALID")
    iterations = max_iter if algorithm == "LOGISTIC" else feature_count + 1
    if dataset.record_count > MAX_FIT_RECORDS or dataset.record_count * feature_count * iterations > MAX_WORK:
        raise TrainingContractError("CONFIG_WORK_BOUND_EXCEEDED")
    return _FitConfig(str(algorithm), feature_count, float(l2), max_iter, float(learning_rate))


class AlgorithmFitBackend:
    """The V1 real backend: PADOS-owned, pure Python, local only, seeded-deterministic."""

    ADAPTER_ID = "training.algorithm-fit"

    def __init__(self) -> None:
        self._descriptor = AdapterDescriptor(
            adapter_id=self.ADAPTER_ID, capability_id="TRAINING_ALGORITHM_FIT", version="1",
            backend="pados.algorithm-fit", determinism=Determinism.SEEDED,
            max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY,
            license=LicenseInfo(spdx="AGPL-3.0-only", engine_version="n/a", distribution=Distribution.NONE),
            lifecycle=LifecycleState.ACTIVE)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def kind(self) -> TrainingKind:
        return TrainingKind.ALGORITHM_FIT

    def available(self) -> bool:
        return True

    def validate_contract(self, contract: TrainingContract, dataset: DatasetSnapshot) -> None:
        if contract.kind is not TrainingKind.ALGORITHM_FIT or contract.backend_adapter_id != self.ADAPTER_ID:
            raise TrainingContractError("BACKEND_MISMATCH")
        if check_snapshot(dataset).schema_ref != TABULAR_SCHEMA_REF:
            raise TrainingContractError("DATASET_SCHEMA_UNSUPPORTED")
        _fit_config(contract, dataset)

    def run(self, contract: TrainingContract, dataset: DatasetSnapshot, *,
            artifacts: ScopedArtifacts) -> TrainingRunResult:
        self.validate_contract(contract, dataset)
        config = _fit_config(contract, dataset)
        rows = load_tabular(dataset, artifacts, feature_count=config.feature_count)
        if config.algorithm == "OLS":
            intercept, weights = fit_ols(rows, l2=config.l2)
        else:
            intercept, weights = fit_logistic(rows, l2=config.l2, max_iter=config.max_iter,
                                              learning_rate=config.learning_rate, seed=contract.seed)
        model: dict[str, Any] = {
            "schema": MODEL_SCHEMA, "algorithm": config.algorithm, "feature_count": config.feature_count,
            "intercept": _round(intercept), "coefficients": [_round(w) for w in weights],
            "contract_hash": contract.contract_hash, "dataset_hash": dataset.content_hash,
            "config_hash": contract.config_hash, "seed": contract.seed, "backend_adapter_id": self.ADAPTER_ID,
        }
        if not all(math.isfinite(v) for v in (model["intercept"], *model["coefficients"])):
            raise TrainingBackendError("FIT_DIVERGED")
        ref = artifacts.put(canonical_model_bytes(model), media_type=MODEL_MEDIA_TYPE, kind=MODEL_KIND)
        metrics = {f"train_{name}": value for name, value in regression_metrics(model, rows).items()}
        return TrainingRunResult(ModelArtifact(ref, self.ADAPTER_ID, contract.contract_hash, dataset.content_hash),
                                 metrics)


@dataclass(frozen=True)
class _ContractSpec:
    adapter_id: str
    capability_id: str
    backend: str
    spdx: str
    required_config: frozenset[str]
    schema_refs: frozenset[str]
    resources: ResourceRequirement


class ContractOnlyBackend:
    """A future backend's contract: validates contract shape, is never available, never runs in V1."""

    def __init__(self, kind: TrainingKind, spec: _ContractSpec) -> None:
        self._kind = kind
        self._spec = spec
        self._descriptor = AdapterDescriptor(
            adapter_id=spec.adapter_id, capability_id=spec.capability_id, version="0", backend=spec.backend,
            determinism=Determinism.SEEDED, max_classification=DataClassification.INTERNAL,
            network_requirement=NetworkRequirement.LOCAL_ONLY, resources=spec.resources,
            license=LicenseInfo(spdx=spec.spdx, engine_version="n/a", distribution=Distribution.RUNTIME_OPTIONAL),
            lifecycle=LifecycleState.EXPERIMENTAL)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def kind(self) -> TrainingKind:
        return self._kind

    @property
    def required_config(self) -> frozenset[str]:
        return self._spec.required_config

    def available(self) -> bool:
        return False

    def validate_contract(self, contract: TrainingContract, dataset: DatasetSnapshot) -> None:
        if contract.kind is not self._kind or contract.backend_adapter_id != self._spec.adapter_id:
            raise TrainingContractError("BACKEND_MISMATCH")
        if check_snapshot(dataset).schema_ref not in self._spec.schema_refs:
            raise TrainingContractError("DATASET_SCHEMA_UNSUPPORTED")
        missing = self._spec.required_config - set(contract.config_dict())
        if missing:
            raise TrainingContractError("CONFIG_REQUIRED_KEY_MISSING")

    def run(self, contract: TrainingContract, dataset: DatasetSnapshot, *,
            artifacts: ScopedArtifacts) -> TrainingRunResult:
        raise TrainingBackendError("BACKEND_UNAVAILABLE")


_GPU = ResourceRequirement(cpu_cores=4, ram_mb=16_384, gpu_required=True, vram_mb=8_192)
CONTRACT_ONLY_SPECS: dict[TrainingKind, _ContractSpec] = {
    # Low-rank adaptation (public method: LoRA, Hu et al. 2021), e.g. via an operator-installed PEFT engine.
    TrainingKind.LORA_FINETUNE: _ContractSpec(
        "training.lora-finetune", "TRAINING_LORA_FINETUNE", "peft", "Apache-2.0",
        frozenset({"rank", "alpha", "learning_rate", "epochs"}), frozenset({"training.instruction-pairs/v1"}), _GPU),
    # Contrastive sentence-embedding training (public method: bi-encoder with in-batch negatives).
    TrainingKind.EMBEDDING: _ContractSpec(
        "training.embedding", "TRAINING_EMBEDDING", "sentence-transformers", "Apache-2.0",
        frozenset({"epochs", "batch_size", "learning_rate"}), frozenset({"training.text-pairs/v1"}), _GPU),
    # Cross-encoder reranker training (public method: pointwise/pairwise relevance classification).
    TrainingKind.RERANKER: _ContractSpec(
        "training.reranker", "TRAINING_RERANKER", "sentence-transformers", "Apache-2.0",
        frozenset({"epochs", "batch_size", "learning_rate"}), frozenset({"training.query-passage-labels/v1"}), _GPU),
    # Single-stage object detection (public method family: YOLO); the common engine is AGPL-3.0 licensed.
    TrainingKind.VISION_DETECTION: _ContractSpec(
        "training.vision-detection", "TRAINING_VISION_DETECTION", "ultralytics", "AGPL-3.0-only",
        frozenset({"epochs", "image_size", "batch_size"}), frozenset({"training.detection-boxes/v1"}), _GPU),
    # Generic evaluation jobs; V1 evaluates ALGORITHM_FIT models directly through pados.training.evaluation.
    TrainingKind.EVALUATION_ONLY: _ContractSpec(
        "training.evaluation-only", "TRAINING_EVALUATION_ONLY", "pados.evaluation", "AGPL-3.0-only",
        frozenset({"metric"}), frozenset({TABULAR_SCHEMA_REF}), ResourceRequirement()),
}


def default_backends() -> dict[str, TrainingBackend]:
    """Backends by adapter id. Installed here != registered != authorized: callers still go through policy."""
    backends: dict[str, TrainingBackend] = {AlgorithmFitBackend.ADAPTER_ID: AlgorithmFitBackend()}
    for kind, spec in CONTRACT_ONLY_SPECS.items():
        backends[spec.adapter_id] = ContractOnlyBackend(kind, spec)
    return backends


def resolve_backend(backends: Mapping[str, TrainingBackend], contract: TrainingContract) -> TrainingBackend:
    """Exact adapter id only: no fallback to another backend, kind or engine."""
    backend = backends.get(contract.backend_adapter_id)
    if backend is None or backend.kind is not contract.kind:
        raise TrainingContractError("BACKEND_NOT_INSTALLED")
    return backend
