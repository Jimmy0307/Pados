"""Capability performance profiles: deterministic, version-separated, explainable aggregates."""
from __future__ import annotations

import random

import pytest

from pados.capability.receipts import VerificationStatus
from pados.learning.profiles import (
    ALL_RESOURCES,
    ProfileKey,
    build_profiles,
    nearest_rank,
    wilson_lower_bound,
)
from pados.learning.records import HumanDecision, resource_ref_for, task_ref_for
from tests.learning.learning_support import BORN_DIGITAL_EN, history, outcome

BUCKET = "document_language=zh-Hant;mime_class=PDF;scan_type=SCANNED"


def key(adapter_id: str = "ocr.engine-a", version: str = "1", bucket: str = BUCKET) -> ProfileKey:
    return ProfileKey(task_ref_for("DOCUMENT_EXTRACTION"), "DOCUMENT_OCR", adapter_id, version, bucket, ALL_RESOURCES)


def test_wilson_lower_bound_matches_the_public_formula() -> None:
    assert wilson_lower_bound(0, 0) == 0.0
    assert wilson_lower_bound(10, 10) == pytest.approx(0.72247, abs=1e-4)
    assert wilson_lower_bound(5, 10) == pytest.approx(0.23659, abs=1e-4)
    assert wilson_lower_bound(0, 10) == 0.0
    assert wilson_lower_bound(95, 100) < 0.95 < wilson_lower_bound(950, 1000) + 0.05
    with pytest.raises(ValueError, match="WILSON_COUNTS_INVALID"):
        wilson_lower_bound(11, 10)


def test_nearest_rank_percentiles() -> None:
    values = sorted([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
    assert nearest_rank(values, 0.5) == 500 and nearest_rank(values, 0.9) == 900
    assert nearest_rank([], 0.5) == 0 and nearest_rank([7], 0.9) == 7


def test_profile_counts_and_rates() -> None:
    records = [
        outcome(0, latency_ms=100, cost_units=2.0, quality=0.9),
        outcome(1, latency_ms=200, cost_units=4.0, human=HumanDecision.ACCEPTED),
        outcome(2, latency_ms=300, human=HumanDecision.REJECTED, retry_count=2),
        outcome(3, latency_ms=400, verification=VerificationStatus.NOT_VERIFIED),
        outcome(4, latency_ms=900, success=False),
    ]
    profile = build_profiles(records)[key()]
    assert (profile.sample_size, profile.successes, profile.good_outcomes) == (5, 4, 3)
    assert profile.success_rate == 0.8 and profile.good_rate == 0.6  # NOT_VERIFIED is not a failure
    assert (profile.verified, profile.verification_passed, profile.verification_pass_rate) == (4, 3, 0.75)
    assert (profile.human_decided, profile.human_accepted, profile.human_accept_rate) == (2, 1, 0.5)
    assert (profile.latency_p50_ms, profile.latency_p90_ms) == (300, 900)
    assert profile.cost_mean == pytest.approx(1.8) and profile.retry_mean == pytest.approx(0.4)
    assert profile.quality_samples == 1 and profile.quality_mean == 0.9
    assert profile.success_wilson_lower == pytest.approx(wilson_lower_bound(4, 5), abs=1e-6)
    assert profile.first_executed_at < profile.last_executed_at


def test_rates_without_observations_are_none_not_zero() -> None:
    profile = build_profiles([outcome(0, verification=VerificationStatus.NOT_VERIFIED)])[key()]
    assert profile.verification_pass_rate is None and profile.human_accept_rate is None
    assert profile.quality_mean is None


def test_aggregation_is_deterministic_and_order_independent() -> None:
    records = history("ocr.engine-a", 30, 7)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    first, second = build_profiles(records)[key()], build_profiles(shuffled)[key()]
    assert first == second and first.profile_hash == second.profile_hash
    altered = build_profiles([*records[:-1], outcome(36, success=False, latency_ms=999)])[key()]
    assert altered.profile_hash != first.profile_hash and altered.source_hash != first.source_hash


def test_profiles_separate_versions_buckets_and_optionally_resources() -> None:
    records = [*history("ocr.engine-a", 3, 0), outcome(10, adapter_version="2"),
               outcome(11, features=BORN_DIGITAL_EN), outcome(12, resource_id="node-2")]
    profiles = build_profiles(records)
    assert profiles[key()].sample_size == 4
    assert profiles[key(version="2")].sample_size == 1
    assert profiles[key(bucket="document_language=en;mime_class=PDF;scan_type=BORN_DIGITAL")].sample_size == 1
    by_scan = build_profiles(records, bucket_keys=("scan_type",))
    assert by_scan[key(bucket="scan_type=SCANNED")].sample_size == 4
    assert build_profiles(records, bucket_keys=())[key(bucket="*")].sample_size == 5
    per_resource = build_profiles(records, by_resource=True)
    assert {k.resource_ref for k in per_resource} == {resource_ref_for("node-1"), resource_ref_for("node-2")}
    assert list(profiles) == sorted(profiles)
    with pytest.raises(ValueError, match="BUCKET_KEY_UNKNOWN"):
        build_profiles(records, bucket_keys=("file_name",))


def test_duplicate_outcomes_are_refused() -> None:
    with pytest.raises(ValueError, match="PROFILE_DUPLICATE_OUTCOME"):
        build_profiles([outcome(1), outcome(1)])
