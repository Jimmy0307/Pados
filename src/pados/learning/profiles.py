# SPDX-License-Identifier: AGPL-3.0-only
"""Capability performance profiles: deterministic aggregates of outcome records (descriptive only).

A profile summarizes the outcomes of one adapter version for one task type (opaque ``task_ref``) and
capability in one context-feature bucket (optionally per opaque ``resource_ref`` digest): counts, success
rate with its Wilson score lower bound (public method: Wilson 1927 score interval, 95%), verification pass
rate, human accept rate (from governed decisions only), nearest-rank latency p50/p90, mean cost, mean
quality and mean retries.

Profiles are keyed per adapter VERSION so an upgraded engine never inherits its predecessor's record.
Only AUTHENTICATED context features bucket a record (``OutcomeRecord.bucket_features``): a record whose
features the trusted plan context did not record falls into the feature-less bucket, so stage-supplied
features can never steer a specific context bucket. Aggregation is order-independent (records are sorted by
the trusted ``(executed_at, outcome_id)``), floats are rounded to fixed precision, and ``profile_hash`` binds every number plus the hashes of the records it was built
from (``source_hash``), so a logged advice can be re-derived and checked.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from pados.capability.receipts import VerificationStatus
from pados.execution.context import canonical_hash
from pados.learning.records import CONTEXT_FEATURES, HumanDecision, OutcomeRecord, context_bucket

PROFILE_SCHEMA = "pados.learning.profile.v2"
WILSON_Z = 1.959963984540054  # two-sided 95%
ALL_RESOURCES = "*"
_PRECISION = 6


def wilson_lower_bound(successes: int, total: int, *, z: float = WILSON_Z) -> float:
    """Lower bound of the Wilson score interval; 0.0 for no observations (no evidence, no credit)."""
    if total <= 0:
        return 0.0
    if not 0 <= successes <= total:
        raise ValueError("WILSON_COUNTS_INVALID")
    p = successes / total
    z2 = z * z
    centre = p + z2 / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return max(0.0, (centre - margin) / (1 + z2 / total))


def nearest_rank(sorted_values: Sequence[int], quantile: float) -> int:
    """Nearest-rank percentile of already sorted values."""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(quantile * len(sorted_values)))
    return sorted_values[rank - 1]


def _round(value: float) -> float:
    return round(value, _PRECISION)


def _ratio(part: int, whole: int) -> float | None:
    return None if whole == 0 else _round(part / whole)


def validate_bucket_keys(keys: Iterable[str] | None) -> tuple[str, ...] | None:
    if keys is None:
        return None
    ordered = tuple(sorted(set(keys)))
    if any(key not in CONTEXT_FEATURES for key in ordered):
        raise ValueError("BUCKET_KEY_UNKNOWN")
    return ordered


@dataclass(frozen=True, order=True)
class ProfileKey:
    task_ref: str
    capability_id: str
    adapter_id: str
    adapter_version: str
    bucket: str
    resource_ref: str = ALL_RESOURCES


class CapabilityPerformanceProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = PROFILE_SCHEMA
    task_ref: str
    capability_id: str
    adapter_id: str
    adapter_version: str
    bucket: str
    resource_ref: str
    sample_size: int
    successes: int
    success_rate: float
    success_wilson_lower: float
    good_outcomes: int  # success AND verification not FAILED AND human decision not REJECTED
    good_rate: float
    good_wilson_lower: float
    verified: int  # outcomes with a verification result (PASSED or FAILED)
    verification_passed: int
    verification_pass_rate: float | None
    human_decided: int
    human_accepted: int
    human_accept_rate: float | None
    latency_p50_ms: int
    latency_p90_ms: int
    cost_mean: float
    quality_samples: int
    quality_mean: float | None
    retry_mean: float
    first_executed_at: str
    last_executed_at: str
    source_hash: str

    @property
    def key(self) -> ProfileKey:
        return ProfileKey(self.task_ref, self.capability_id, self.adapter_id, self.adapter_version, self.bucket,
                          self.resource_ref)

    @property
    def profile_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def _good(record: OutcomeRecord) -> bool:
    return (record.success and record.verification is not VerificationStatus.FAILED
            and record.human_decision is not HumanDecision.REJECTED)


def aggregate(key: ProfileKey, records: Iterable[OutcomeRecord]) -> CapabilityPerformanceProfile:
    """One profile from the records of one key (caller groups; order of input does not matter)."""
    ordered = sorted(records, key=lambda item: (item.executed_at, item.outcome_id))
    total = len(ordered)
    if total == 0:
        raise ValueError("PROFILE_REQUIRES_RECORDS")
    successes = sum(item.success for item in ordered)
    good = sum(_good(item) for item in ordered)
    verified = [item for item in ordered if item.verification is not VerificationStatus.NOT_VERIFIED]
    passed = sum(item.verification is VerificationStatus.PASSED for item in verified)
    decided = [item for item in ordered if item.human_decision is not HumanDecision.NONE]
    accepted = sum(item.human_decision is HumanDecision.ACCEPTED for item in decided)
    latencies = sorted(item.latency_ms for item in ordered)
    qualities = [item.quality_metric for item in ordered if item.quality_metric is not None]
    return CapabilityPerformanceProfile(
        task_ref=key.task_ref, capability_id=key.capability_id, adapter_id=key.adapter_id,
        adapter_version=key.adapter_version, bucket=key.bucket, resource_ref=key.resource_ref,
        sample_size=total, successes=successes, success_rate=_round(successes / total),
        success_wilson_lower=_round(wilson_lower_bound(successes, total)), good_outcomes=good,
        good_rate=_round(good / total), good_wilson_lower=_round(wilson_lower_bound(good, total)),
        verified=len(verified), verification_passed=passed, verification_pass_rate=_ratio(passed, len(verified)),
        human_decided=len(decided), human_accepted=accepted, human_accept_rate=_ratio(accepted, len(decided)),
        latency_p50_ms=nearest_rank(latencies, 0.5), latency_p90_ms=nearest_rank(latencies, 0.9),
        cost_mean=_round(math.fsum(item.cost_units for item in ordered) / total),
        quality_samples=len(qualities),
        quality_mean=_round(math.fsum(qualities) / len(qualities)) if qualities else None,
        retry_mean=_round(sum(item.retry_count for item in ordered) / total),
        first_executed_at=ordered[0].executed_at, last_executed_at=ordered[-1].executed_at,
        source_hash=canonical_hash(sorted(item.record_hash for item in ordered)))


def profile_key(record: OutcomeRecord, *, bucket_keys: tuple[str, ...] | None = None,
                by_resource: bool = False) -> ProfileKey:
    return ProfileKey(record.task_ref, record.capability_id, record.adapter_id, record.adapter_version,
                      context_bucket(record.bucket_features, bucket_keys),
                      record.resource_ref if by_resource else ALL_RESOURCES)


def build_profiles(records: Iterable[OutcomeRecord], *, bucket_keys: Iterable[str] | None = None,
                   by_resource: bool = False) -> dict[ProfileKey, CapabilityPerformanceProfile]:
    """Group records by profile key and aggregate each group; keys are returned in sorted order.

    ``bucket_keys`` projects context features onto those names (None keeps the full feature set);
    ``by_resource`` additionally splits profiles per resource id.
    """
    keys = validate_bucket_keys(bucket_keys)
    groups: dict[ProfileKey, list[OutcomeRecord]] = {}
    seen: set[str] = set()
    for record in records:
        if record.outcome_id in seen:
            raise ValueError("PROFILE_DUPLICATE_OUTCOME")
        seen.add(record.outcome_id)
        groups.setdefault(profile_key(record, bucket_keys=keys, by_resource=by_resource), []).append(record)
    return {key: aggregate(key, groups[key]) for key in sorted(groups)}
