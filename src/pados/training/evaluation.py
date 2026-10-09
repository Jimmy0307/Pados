# SPDX-License-Identifier: AGPL-3.0-only
"""Held-out evaluation reports and benchmark comparisons (evidence; never authorization).

An ``EvaluationReport`` binds a model artifact hash to a held-out ``DatasetSnapshot`` (snapshot id, content
and snapshot hash) and to the training dataset hash it must differ from, with bounded metrics. The held-out
property is checked, not asserted: evaluation snapshots that share a member artifact or any canonical record
with the training snapshot are refused.

The registry never trusts a presented report: it recomputes the evaluation through
``evaluate_algorithm_fit`` from the contract's designated evaluation snapshot and compares
``evidence_hash`` (CR-12 TP-03).

A ``BenchmarkComparison`` says whether a candidate beat a baseline on the same held-out data. It is advisory
evidence only (``authorizes = False``): BETTER BENCHMARK != AUTHORIZED. Nothing in this module can change a
registry state.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.artifacts import ArtifactRef, ArtifactStore
from pados.execution.context import canonical_hash
from pados.training.backends import load_tabular, parse_model, regression_metrics
from pados.training.contracts import DatasetSnapshot, SnapshotRef, TrainingContractError, check_snapshot

REPORT_SCHEMA = "pados.training.evaluation-report.v1"
EVALUATOR_ID = "training.eval.algorithm-fit"
EVALUATOR_VERSION = "1"
MAX_REPORT_METRICS = 32
# Direction of improvement per known metric; unknown metrics cannot be compared.
METRIC_DIRECTION: dict[str, bool] = {"mse": False, "r2": True, "log_loss": False, "accuracy": True}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def metric_pairs(value: Any) -> tuple[tuple[str, float], ...]:
    """Sorted, finite, uniquely named metric pairs from a mapping or a sequence of pairs."""
    items = value.items() if isinstance(value, Mapping) else value
    pairs: list[tuple[str, float]] = []
    try:
        for key, number in items:
            if (not isinstance(key, str) or not _METRIC.fullmatch(key) or isinstance(number, bool)
                    or not isinstance(number, (int, float)) or not math.isfinite(float(number))):
                raise ValueError("METRIC_INVALID")
            pairs.append((key, float(number)))
    except TypeError as exc:
        raise ValueError("METRIC_INVALID") from exc
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("METRIC_DUPLICATE")
    return tuple(sorted(pairs))


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = REPORT_SCHEMA
    report_id: str
    model_artifact_sha256: str
    dataset: SnapshotRef  # the held-out evaluation snapshot
    training_dataset_hash: str
    metrics: tuple[tuple[str, float], ...] = Field(min_length=1, max_length=MAX_REPORT_METRICS)
    evaluator_id: str
    evaluator_version: str
    created_at: str
    semantic_authority: str = "NONE"

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != REPORT_SCHEMA:
            raise ValueError("REPORT_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("report_id", "evaluator_id", "evaluator_version")
    @classmethod
    def identifiers(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("REPORT_IDENTIFIER_INVALID")
        return value

    @field_validator("model_artifact_sha256", "training_dataset_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("REPORT_HASH_INVALID")
        return value

    @field_validator("metrics", mode="before")
    @classmethod
    def bounded_metrics(cls, value: Any) -> tuple[tuple[str, float], ...]:
        return metric_pairs(value)

    @field_validator("created_at")
    @classmethod
    def created(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("TIMESTAMP_INVALID") from exc
        if parsed.tzinfo is None:
            raise ValueError("TIMESTAMP_NOT_AWARE")
        return value

    @field_validator("semantic_authority")
    @classmethod
    def no_authority(cls, value: str) -> str:
        if value != "NONE":
            raise ValueError("EVALUATION_REPORT_CANNOT_CARRY_AUTHORITY")
        return value

    @model_validator(mode="after")
    def held_out(self) -> EvaluationReport:
        if self.dataset.content_hash == self.training_dataset_hash:
            raise ValueError("EVALUATION_DATASET_NOT_HELD_OUT")
        return self

    def metric(self, name: str) -> float | None:
        return dict(self.metrics).get(name)

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def evidence_hash(self) -> str:
        """Everything the evaluator computed (excludes the free report id and creation time), so a trusted
        recomputation can be compared with a presented report."""
        return canonical_hash(self.model_dump(mode="json", exclude={"report_id", "created_at"}))


def _record_hashes(rows: list[tuple[list[float], float]]) -> set[str]:
    return {canonical_hash({"features": features, "label": label}) for features, label in rows}


def evaluate_algorithm_fit(model: ArtifactRef, *, store: ArtifactStore, eval_dataset: DatasetSnapshot,
                           training_dataset: DatasetSnapshot, report_id: str, created_at: str) -> EvaluationReport:
    """Deterministically evaluate an ALGORITHM_FIT model on a verified held-out snapshot."""
    eval_dataset, training_dataset = check_snapshot(eval_dataset), check_snapshot(training_dataset)
    if (eval_dataset.content_hash == training_dataset.content_hash
            or eval_dataset.member_hashes & training_dataset.member_hashes):
        raise TrainingContractError("EVALUATION_DATASET_NOT_HELD_OUT")
    parsed = parse_model(store.get(model))
    if parsed["dataset_hash"] != training_dataset.content_hash:
        raise TrainingContractError("EVALUATION_TRAINING_DATASET_MISMATCH")
    feature_count = int(parsed["feature_count"])
    eval_rows = load_tabular(eval_dataset, store, feature_count=feature_count)
    train_rows = load_tabular(training_dataset, store, feature_count=feature_count)
    if _record_hashes(eval_rows) & _record_hashes(train_rows):
        raise TrainingContractError("EVALUATION_RECORDS_OVERLAP_TRAINING")
    return EvaluationReport(report_id=report_id, model_artifact_sha256=model.sha256, dataset=eval_dataset.ref(),
                            training_dataset_hash=training_dataset.content_hash,
                            metrics=metric_pairs(regression_metrics(parsed, eval_rows)), evaluator_id=EVALUATOR_ID,
                            evaluator_version=EVALUATOR_VERSION, created_at=created_at)


class BenchmarkComparison(BaseModel):
    """Candidate vs baseline on one metric over the same held-out dataset. Advisory only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    higher_is_better: bool
    candidate_report_hash: str
    baseline_report_hash: str
    dataset_content_hash: str
    candidate_value: float
    baseline_value: float
    delta: float  # signed improvement (positive = candidate better)
    improved: bool
    authorizes: bool = False

    @field_validator("authorizes")
    @classmethod
    def never_authorizes(cls, value: bool) -> bool:
        if value:
            raise ValueError("BENCHMARK_CANNOT_AUTHORIZE")
        return value


def compare(candidate: EvaluationReport, baseline: EvaluationReport, *, metric: str) -> BenchmarkComparison:
    if metric not in METRIC_DIRECTION:
        raise TrainingContractError("BENCHMARK_METRIC_UNKNOWN")
    if candidate.dataset.content_hash != baseline.dataset.content_hash:
        raise TrainingContractError("BENCHMARK_DATASET_MISMATCH")
    candidate_value, baseline_value = candidate.metric(metric), baseline.metric(metric)
    if candidate_value is None or baseline_value is None:
        raise TrainingContractError("BENCHMARK_METRIC_MISSING")
    higher = METRIC_DIRECTION[metric]
    delta = candidate_value - baseline_value if higher else baseline_value - candidate_value
    return BenchmarkComparison(metric=metric, higher_is_better=higher, candidate_report_hash=candidate.report_hash,
                               baseline_report_hash=baseline.report_hash,
                               dataset_content_hash=candidate.dataset.content_hash, candidate_value=candidate_value,
                               baseline_value=baseline_value, delta=delta, improved=delta > 0)
