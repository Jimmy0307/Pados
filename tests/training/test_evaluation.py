"""Held-out evaluation reports and advisory benchmark comparisons."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pados.training.contracts import TrainingContractError
from pados.training.evaluation import BenchmarkComparison, EvaluationReport, compare

from .support import linear_rows, make_env, make_snapshot


def test_report_binds_artifact_and_held_out_dataset(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _, _, result = env.trained()
    report = env.report(result)
    assert report.model_artifact_sha256 == result.artifact.ref.sha256
    assert report.dataset.matches(env.held_out)
    assert report.training_dataset_hash == env.train.content_hash
    assert report.metric("r2") is not None and report.metric("r2") > 0.999  # type: ignore[operator]
    assert report.metric("records") == 30.0
    assert report.semantic_authority == "NONE"
    assert env.report(result).report_hash == report.report_hash  # deterministic


def test_evaluation_refuses_non_held_out_data(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _, _, result = env.trained()
    with pytest.raises(TrainingContractError, match="EVALUATION_DATASET_NOT_HELD_OUT"):
        env.report(result, held_out=env.train)
    shared_member = make_snapshot(env.store, [linear_rows(40, seed=1)], snapshot_id="shared")
    with pytest.raises(TrainingContractError, match="EVALUATION_DATASET_NOT_HELD_OUT"):
        env.report(result, held_out=shared_member)
    repacked = make_snapshot(env.store, [linear_rows(40, seed=1)[:7] + linear_rows(5, seed=77)], snapshot_id="rp")
    with pytest.raises(TrainingContractError, match="EVALUATION_RECORDS_OVERLAP_TRAINING"):
        env.report(result, held_out=repacked)


def test_report_model_rejects_training_hash_as_eval_hash(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _, _, result = env.trained()
    data = env.report(result).model_dump(mode="json")
    data["training_dataset_hash"] = data["dataset"]["content_hash"]
    with pytest.raises(ValidationError, match="EVALUATION_DATASET_NOT_HELD_OUT"):
        EvaluationReport.model_validate(data)
    data = env.report(result).model_dump(mode="json")
    data["semantic_authority"] = "ACCEPTED"
    with pytest.raises(ValidationError, match="EVALUATION_REPORT_CANNOT_CARRY_AUTHORITY"):
        EvaluationReport.model_validate(data)


def test_benchmark_comparison_is_advisory_and_same_dataset_only(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    _, _, good = env.trained()
    _, _, ridge = env.trained(env.contract(config={"algorithm": "OLS", "feature_count": 2, "l2": 5000.0}),
                              job_id="job-00000002")
    candidate, baseline = env.report(good), env.report(ridge, report_id="report-2")
    comparison = compare(candidate, baseline, metric="mse")
    assert comparison.improved and comparison.delta > 0 and comparison.higher_is_better is False
    assert comparison.authorizes is False
    with pytest.raises(ValidationError, match="BENCHMARK_CANNOT_AUTHORIZE"):
        BenchmarkComparison.model_validate(dict(comparison.model_dump(), authorizes=True))
    with pytest.raises(TrainingContractError, match="BENCHMARK_METRIC_UNKNOWN"):
        compare(candidate, baseline, metric="vibes")
    other = make_snapshot(env.store, [linear_rows(20, seed=31)], snapshot_id="heldout-2")
    with pytest.raises(TrainingContractError, match="BENCHMARK_DATASET_MISMATCH"):
        compare(candidate, env.report(ridge, report_id="report-3", held_out=other), metric="mse")
