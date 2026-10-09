# SPDX-License-Identifier: AGPL-3.0-only
"""OutcomeAdvisor: bounded, explainable, reversible, audited advice for already-feasible candidates.

Implements ``pados.capability.seams.ScoreAdvisor``. The advisor is disabled by default and only ever
returns ``Advice`` for the candidates it is handed (the planner passes the adapters policy already found
feasible and authorized); advice reaches a plan exclusively through ``seams.apply_advice``, which clamps it
to ``ADVICE_BOUND`` and drops anything outside the feasible set or bound to another context. The advisor
holds no reference to policy, registry, grants, ACL or approval state and has no write path to any of them.

Binding: ``advice_context_hash`` binds task, capability, context features, the candidate set (adapter ids
AND versions), the task classification and the planner-supplied plan binding hash. All of them are
mandatory when advice is enabled: without classification or plan binding the call is logged as
``BINDING_INCOMPLETE`` and no advice is returned, and ``context_hash`` returns an unbound hash that matches
no advice (fail closed). The hash is computed over opaque digests (``task-``/``cap-``/``adp-``), so a
verifier can reconstruct it from the log alone. Advice is version-matched: after an adapter upgrade, advice
computed for the old version can never apply.

Score (V1, reliability only): ``ADVICE_BOUND * (2 * L - 1)`` where ``L`` is the Wilson 95% lower bound of
the "good outcome" rate (succeeded, verification not FAILED, human decision not REJECTED). Below
``min_samples`` there is no advice. Latency and cost are explained but do not move the score.

Audit: an enabled advisor requires a durable ``AdviceLog`` WITH a configured ``ExternalAnchor``
(``ADVICE_LOG_DURABLE_REQUIRED`` / ``ADVICE_ANCHOR_REQUIRED`` at construction; the acknowledged test-only
in-memory log always carries its own in-memory anchor). Every enabled call is logged (hash-chained, sequenced,
anchored, with the configuration inputs, a per-candidate derivation snapshot and the plan binding hash)
BEFORE advice is returned; if logging or anchoring fails no advice is returned. ``advise_logged`` returns the
advice together with the audit entry's ``(seq, entry_hash)`` (``AdviceResult.audit_ref``) for the plan to
record.

Issuance (``issued``): advice is reported as issued only from a log that verifies against the external anchor
(``ANCHORED``, head equal to the anchored checkpoint, so the entry lies within the anchored history), only
within ``AdvisorConfig.advice_ttl`` of its ``logged_at`` (default 24 h, at most 7 days) and, when the plan
recorded it, only for exactly that ``(seq, entry_hash)``. Rewritten, truncated, unanchored, unavailable,
unverifiable or expired history issues nothing (``None``).

Verification: ``verify(entry)`` checks the configuration hash and inputs, the status/shape, candidate
uniqueness, the context binding (recomputed from the logged digests), the counter relations and the score
arithmetic of every snapshot (``SCORE_ARITHMETIC_VERIFIED``); when the current records still reproduce the
exact advice set, including the candidates that received none, it is ``DERIVATION_VERIFIED``. Historical
verification after the records are pruned therefore rests on the logged snapshots.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pados.capability.model import AdapterDescriptor
from pados.capability.seams import ADVICE_BOUND, Advice
from pados.connectivity.storage import StoreLockTimeout
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.learning.advice_log import (
    COMPUTED_STATUSES,
    MAX_CANDIDATES,
    AdviceLog,
    AdviceLogEntry,
    AdviceRecordBody,
    AdviceStatus,
    DerivationSnapshot,
    EntryKind,
    LoggedAdvice,
    LoggedCandidate,
    config_hash_of,
)
from pados.learning.profiles import (
    ALL_RESOURCES,
    CapabilityPerformanceProfile,
    ProfileKey,
    build_profiles,
    validate_bucket_keys,
    wilson_lower_bound,
)
from pados.learning.records import (
    ContextFeatures,
    IdentityDigests,
    IdentityKey,
    LearningRecordError,
    OutcomeRecord,
    adapter_ref_for,
    capability_ref_for,
    context_bucket,
    digest_mode,
    identity_digests,
    task_ref_for,
    validate_context_features,
)
from pados.learning.store import FUTURE_SKEW, LearningStoreError, utc_now

ADVISOR_VERSION = "pados.learning.advisor.v3"
ADVICE_CONTEXT_KIND = "pados.learning.advice-context.v3"
DEFAULT_MIN_SAMPLES = 20
MIN_SAMPLES_FLOOR = 5
DEFAULT_ADVICE_TTL = timedelta(hours=24)
MIN_ADVICE_TTL = timedelta(minutes=1)
MAX_ADVICE_TTL = timedelta(days=7)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
UNBOUND_CONTEXT_HASH = canonical_hash({"kind": ADVICE_CONTEXT_KIND, "unbound": True})  # never carried by advice
_SOURCE_ERRORS = (LearningStoreError, StoreLockTimeout, OSError, ValueError)

CandidateLike = AdapterDescriptor | tuple[str, str]


class OutcomeSource(Protocol):
    def read(self) -> Sequence[OutcomeRecord]: ...


class VerificationLevel(StrEnum):
    REJECTED = "REJECTED"
    SCORE_ARITHMETIC_VERIFIED = "SCORE_ARITHMETIC_VERIFIED"  # everything checkable from the log alone holds
    DERIVATION_VERIFIED = "DERIVATION_VERIFIED"  # ...and the current records reproduce the exact advice set


class ConfigSource(StrEnum):
    ADVISOR = "ADVISOR"  # checked against the verifying advisor's own configuration
    LOGGED = "LOGGED"  # explicitly checked against the configuration persisted in the entry


@dataclass(frozen=True)
class AdviceVerification:
    level: VerificationLevel
    code: str
    config_source: ConfigSource
    identity_digests: IdentityDigests = IdentityDigests.UNKEYED  # IDENTITY_DIGESTS_UNKEYED: dictionary-testable

    @property
    def ok(self) -> bool:
        return self.level is not VerificationLevel.REJECTED


@dataclass(frozen=True)
class AdviceResult:
    """Advice plus the stable identity of its audit entry (record ``(entry_seq, entry_hash)`` in the plan)."""

    advice: tuple[Advice, ...]
    status: AdviceStatus | None
    entry_seq: int | None
    entry_hash: str | None

    @property
    def audit_ref(self) -> tuple[int, str] | None:
        """``(seq, entry_hash)`` of the audit entry: what the plan records and later passes to ``issued``."""
        return None if self.entry_seq is None or self.entry_hash is None else (self.entry_seq, self.entry_hash)


def candidate_refs(candidates: Iterable[CandidateLike], key: IdentityKey | None = None) -> tuple[str, ...]:
    """Sorted ``adp-`` digests of (adapter id, version); duplicate adapter ids are refused."""
    pairs: list[tuple[str, str]] = []
    for item in candidates:
        if isinstance(item, AdapterDescriptor):
            pairs.append((item.adapter_id, item.version))
        else:
            pairs.append((str(item[0]), str(item[1])))
    if len({adapter_id for adapter_id, _ in pairs}) != len(pairs):
        raise LearningRecordError("ADVICE_CANDIDATES_DUPLICATE")
    return tuple(sorted(adapter_ref_for(adapter_id, version, key) for adapter_id, version in pairs))


def as_classification(value: object) -> DataClassification | None:
    """The seam passes the classification value as a string; anything that is not a known value is unbound."""
    if isinstance(value, DataClassification):
        return value
    if isinstance(value, str):
        try:
            return DataClassification(value)
        except ValueError:
            return None
    return None


def context_hash_from_refs(*, task_ref: str, capability_ref: str, context_features: ContextFeatures,
                           adapter_refs: Sequence[str], classification: DataClassification,
                           plan_binding_hash: str) -> str:
    """The binding hash over the logged digests (what a verifier reconstructs from an entry)."""
    return canonical_hash({"kind": ADVICE_CONTEXT_KIND, "task_ref": task_ref, "capability_ref": capability_ref,
                           "context_features": [list(item) for item in context_features],
                           "candidates": sorted(adapter_refs),
                           "classification": DataClassification(classification).value,
                           "plan_binding_hash": plan_binding_hash})


def advice_context_hash(*, task_type: str, capability_id: str, context_features: Mapping[str, str],
                        candidates: Iterable[CandidateLike], classification: DataClassification,
                        plan_binding_hash: str, identity_key: IdentityKey | None = None) -> str:
    """The hash that binds advice to one request: advice computed for anything else never matches.

    ``plan_binding_hash`` is the planner's ``pados.capability.planner.plan_binding_hash``: it covers the fabric
    task hash (task id and type, project, work contract, classification, network policy, required outputs,
    quality, risk, human gate, inputs and parameters), the template hash, the capability and the feasible
    candidate set, so advice for one project, work contract or input set never applies to another."""
    if not isinstance(plan_binding_hash, str) or not _SHA256.fullmatch(plan_binding_hash):
        raise LearningRecordError("PLAN_BINDING_HASH_INVALID")
    return context_hash_from_refs(task_ref=task_ref_for(task_type, identity_key),
                                  capability_ref=capability_ref_for(capability_id, identity_key),
                                  context_features=validate_context_features(context_features),
                                  adapter_refs=candidate_refs(candidates, identity_key), classification=classification,
                                  plan_binding_hash=plan_binding_hash)


@dataclass(frozen=True)
class AdvisorConfig:
    enabled: bool = False  # explicit opt-in; default is no advice
    min_samples: int = DEFAULT_MIN_SAMPLES
    bucket_keys: tuple[str, ...] | None = None  # None: full feature set; () : one bucket for all contexts
    advice_ttl: timedelta = DEFAULT_ADVICE_TTL  # issued() reports logged advice only this long after logged_at

    def __post_init__(self) -> None:
        if not MIN_SAMPLES_FLOOR <= self.min_samples <= 1_000_000:
            raise ValueError("ADVISOR_MIN_SAMPLES_OUT_OF_RANGE")
        if not isinstance(self.advice_ttl, timedelta) or not MIN_ADVICE_TTL <= self.advice_ttl <= MAX_ADVICE_TTL:
            raise ValueError("ADVICE_TTL_OUT_OF_RANGE")
        object.__setattr__(self, "bucket_keys", validate_bucket_keys(self.bucket_keys))

    @property
    def config_hash(self) -> str:
        return config_hash_of(advisor_version=ADVISOR_VERSION, min_samples=self.min_samples,
                              bucket_keys=self.bucket_keys)


def score_from_lower_bound(lower_bound: float) -> float:
    return round(ADVICE_BOUND * (2.0 * lower_bound - 1.0), 6)


def score_for(profile: CapabilityPerformanceProfile) -> float:
    return score_from_lower_bound(profile.good_wilson_lower)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def explain(profile: CapabilityPerformanceProfile, score: float, min_samples: int,
            digests: IdentityDigests = IdentityDigests.UNKEYED) -> str:
    """Deterministic explanation built only from profile numbers (no record content exists to leak); it states
    whether identity digests are keyed."""
    return (f"n={profile.sample_size}>=min {min_samples}; good={profile.good_outcomes}/{profile.sample_size} "
            f"({_pct(profile.good_rate)}), wilson95_lb={profile.good_wilson_lower:.3f}; "
            f"verify_pass={_pct(profile.verification_pass_rate)} of {profile.verified}; "
            f"human_accept={_pct(profile.human_accept_rate)} of {profile.human_decided}; "
            f"latency p50={profile.latency_p50_ms}ms p90={profile.latency_p90_ms}ms; "
            f"cost_mean={profile.cost_mean:.3f}; score={score:+.3f} = {ADVICE_BOUND}*(2*lb-1); {digests.value}")


def snapshot_of(profile: CapabilityPerformanceProfile, adapter_ref: str, min_samples: int) -> DerivationSnapshot:
    return DerivationSnapshot(adapter_ref=adapter_ref, sample_size=profile.sample_size,
                              successes=profile.successes, good_outcomes=profile.good_outcomes,
                              verified=profile.verified, verification_passed=profile.verification_passed,
                              human_decided=profile.human_decided, human_accepted=profile.human_accepted,
                              good_wilson_lower=profile.good_wilson_lower, min_samples=min_samples,
                              profile_hash=profile.profile_hash)


def logged_advice(profile: CapabilityPerformanceProfile, adapter_ref: str, min_samples: int) -> LoggedAdvice:
    snapshot = snapshot_of(profile, adapter_ref, min_samples)
    return LoggedAdvice(adapter_ref=adapter_ref, score=score_for(profile), snapshot=snapshot,
                        snapshot_hash=snapshot.snapshot_hash)


def logged_explanation(advice: LoggedAdvice) -> str:
    """Deterministic explanation rebuilt from the logged derivation snapshot alone (reported by ``issued``)."""
    snap = advice.snapshot
    return (f"logged: n={snap.sample_size}>=min {snap.min_samples}; good={snap.good_outcomes}/{snap.sample_size}, "
            f"wilson95_lb={snap.good_wilson_lower:.3f}; verify_pass={snap.verification_passed}/{snap.verified}; "
            f"human_accept={snap.human_accepted}/{snap.human_decided}; score={advice.score:+.3f} = "
            f"{ADVICE_BOUND}*(2*lb-1); snapshot={advice.snapshot_hash[:16]}; {digest_mode(advice.adapter_ref).value}")


def _shape_problem(entry: AdviceLogEntry) -> str | None:
    """Status, candidate uniqueness and the reconstructed context binding of one advice entry."""
    if (entry.status is AdviceStatus.ADVISED) != bool(entry.advice):
        return "ADVICE_STATUS_INCONSISTENT"
    refs = [item.adapter_ref for item in entry.candidates]
    if len(set(refs)) != len(refs):
        return "ADVICE_CANDIDATES_NOT_UNIQUE"
    if refs != sorted(refs):
        return "ADVICE_CANDIDATES_NOT_CANONICAL"
    advised = [item.adapter_ref for item in entry.advice]
    if len(set(advised)) != len(advised) or advised != sorted(advised) or not set(advised) <= set(refs):
        return "ADVICE_NOT_FOR_LOGGED_CANDIDATES"
    if len({digest_mode(ref) for ref in (*refs, entry.task_ref or "", entry.capability_ref or "") if ref}) > 1:
        return "ADVICE_IDENTITY_DIGESTS_MIXED"  # keyed and unkeyed identities never share one entry
    if entry.status not in COMPUTED_STATUSES and entry.context_hash is None:
        return None
    if (entry.task_ref is None or entry.capability_ref is None or entry.classification is None
            or entry.plan_binding_hash is None or entry.context_hash is None or not refs):
        return "ADVICE_BINDING_MISSING"
    expected = context_hash_from_refs(task_ref=entry.task_ref, capability_ref=entry.capability_ref,
                                      context_features=entry.context_features, adapter_refs=refs,
                                      classification=entry.classification,
                                      plan_binding_hash=entry.plan_binding_hash)
    return None if expected == entry.context_hash else "ADVICE_CONTEXT_BINDING_MISMATCH"


def _snapshot_problem(item: LoggedAdvice, min_samples: int) -> str | None:
    snap = item.snapshot
    if snap.snapshot_hash != item.snapshot_hash or snap.adapter_ref != item.adapter_ref:
        return "ADVICE_SNAPSHOT_HASH_MISMATCH"
    if snap.min_samples != min_samples or snap.sample_size < min_samples:
        return "ADVICE_BELOW_MIN_SAMPLES"
    if not snap.counters_consistent():
        return "ADVICE_COUNTERS_INCONSISTENT"
    if (round(wilson_lower_bound(snap.good_outcomes, snap.sample_size), 6) != snap.good_wilson_lower
            or score_from_lower_bound(snap.good_wilson_lower) != item.score):
        return "ADVICE_SCORE_NOT_DERIVED"
    return None


class OutcomeAdvisor:
    """``ScoreAdvisor`` over authenticated outcomes. Pure function of (records, config, request) plus a log."""

    def __init__(self, source: OutcomeSource, *, config: AdvisorConfig | None = None,
                 log: AdviceLog | None = None, clock: Callable[[], datetime] = utc_now,
                 identity_key: IdentityKey | None = None) -> None:
        self._source = source
        self.config = config or AdvisorConfig()
        if self.config.enabled and not _log_acceptable(log):
            raise ValueError("ADVICE_LOG_DURABLE_REQUIRED")
        if self.config.enabled and log is not None and log.anchor is None:
            raise ValueError("ADVICE_ANCHOR_REQUIRED")  # issuance provenance needs an external anchor
        self.log = log
        self._clock = clock
        self._identity_key = identity_key  # in memory only; must equal the OutcomeStore's key

    @property
    def identity_digests(self) -> IdentityDigests:
        return identity_digests(self._identity_key)

    def context_hash(self, *, task_type: str, capability_id: str, context_features: Mapping[str, str],
                     candidates: Sequence[CandidateLike] | None = None,
                     classification: str | None = None,
                     plan_binding_hash: str | None = None) -> str:
        """``ScoreAdvisor.context_hash``. Without candidates, classification and plan binding the hash is
        unbound and matches no advice (fail closed, never an exception in the planner)."""
        bound_classification = as_classification(classification)
        if candidates is None or bound_classification is None or plan_binding_hash is None:
            return UNBOUND_CONTEXT_HASH
        try:
            return advice_context_hash(task_type=task_type, capability_id=capability_id,
                                       context_features=context_features, candidates=candidates,
                                       classification=bound_classification, plan_binding_hash=plan_binding_hash,
                                       identity_key=self._identity_key)
        except (LearningRecordError, ValueError, TypeError, IndexError):
            return UNBOUND_CONTEXT_HASH

    def advise(self, *, task_type: str, capability_id: str, candidates: Sequence[AdapterDescriptor],
               context_features: Mapping[str, str], classification: str | None = None,
               plan_binding_hash: str | None = None) -> Sequence[Advice]:
        """``ScoreAdvisor.advise``; see ``advise_logged`` for the audit entry identity."""
        return self.advise_logged(task_type=task_type, capability_id=capability_id, candidates=candidates,
                                  context_features=context_features, classification=classification,
                                  plan_binding_hash=plan_binding_hash).advice

    def advise_logged(self, *, task_type: str, capability_id: str, candidates: Sequence[AdapterDescriptor],
                      context_features: Mapping[str, str], classification: str | None = None,
                      plan_binding_hash: str | None = None) -> AdviceResult:
        """Advice plus the ``(seq, entry_hash)`` of the audit entry it was logged under."""
        if not self.config.enabled or self.log is None or not _log_acceptable(self.log):
            return AdviceResult((), None, None, None)
        bound_classification = as_classification(classification)
        features: ContextFeatures = ()
        task_ref: str | None = None
        capability_ref: str | None = None
        context_hash: str | None = None
        pairs: list[tuple[str, AdapterDescriptor]] = []
        advised: list[tuple[Advice, LoggedAdvice]] = []
        binding = plan_binding_hash if isinstance(plan_binding_hash, str) and _SHA256.fullmatch(
            plan_binding_hash) else None
        try:
            features = validate_context_features(context_features)
            task_ref = task_ref_for(task_type, self._identity_key)
            capability_ref = capability_ref_for(capability_id, self._identity_key)
            valid = plan_binding_hash is None or binding is not None
        except LearningRecordError:
            valid = False
        if not valid:
            status, features, task_ref, capability_ref = AdviceStatus.CONTEXT_INVALID, (), None, None
        elif bound_classification is None or binding is None or task_ref is None or capability_ref is None:
            status = AdviceStatus.BINDING_INCOMPLETE
        else:
            checked = _candidate_pairs(candidates, capability_id, self._identity_key)
            if checked is None:
                status = AdviceStatus.CANDIDATES_INVALID
            else:
                pairs = checked
                refs = [ref for ref, _ in pairs]
                context_hash = context_hash_from_refs(task_ref=task_ref, capability_ref=capability_ref,
                                                      context_features=features, adapter_refs=refs,
                                                      classification=bound_classification, plan_binding_hash=binding)
                try:
                    qualifying = self._qualifying(tuple(self._source.read()), task_ref, capability_ref, refs,
                                                  features)
                except _SOURCE_ERRORS:
                    status = AdviceStatus.SOURCE_UNAVAILABLE
                else:
                    by_ref = dict(pairs)
                    for ref, profile in qualifying:
                        item = logged_advice(profile, ref, self.config.min_samples)
                        advised.append((Advice(adapter_id=by_ref[ref].adapter_id, score=item.score,
                                               sample_size=profile.sample_size,
                                               explanation=explain(profile, item.score, self.config.min_samples,
                                                                   self.identity_digests),
                                               profile_hash=profile.profile_hash, context_hash=context_hash), item))
                    status = AdviceStatus.ADVISED if advised else AdviceStatus.NO_QUALIFYING_PROFILE
        try:
            entry = self.log.record(AdviceRecordBody(
                logged_at=self._clock().isoformat(), advisor_version=ADVISOR_VERSION,
                config_hash=self.config.config_hash, min_samples=self.config.min_samples,
                bucket_keys=self.config.bucket_keys, status=status, task_ref=task_ref, capability_ref=capability_ref,
                classification=bound_classification if context_hash else None, plan_binding_hash=binding,
                context_features=features, context_hash=context_hash,
                candidates=tuple(LoggedCandidate(adapter_ref=ref) for ref, _ in pairs),
                advice=tuple(item for _, item in advised)))
        except (LearningStoreError, StoreLockTimeout, OSError, ValueError):
            return AdviceResult((), status, None, None)  # unlogged advice is never returned
        return AdviceResult(tuple(item for item, _ in advised), status, entry.seq, entry.entry_hash)

    def issued(self, plan_binding_hash: str, *, candidates: Sequence[tuple[str, str]],
               audit_ref: tuple[int, str] | None = None) -> tuple[Advice, ...] | None:
        """``ScoreAdvisor.issued``: the advice of the anchored, verified log entry for this plan binding.

        Provenance first: the log must verify against the configured ``ExternalAnchor`` (``ANCHORED``, head
        equal to the anchored checkpoint: ``AdviceLog.anchored_view``) and the entry must lie within that
        anchored history; rewritten, truncated, unanchored, unavailable or unverifiable history -> ``None``.
        With ``audit_ref`` (the ``(seq, entry_hash)`` the plan recorded from ``advise_logged``) only exactly
        that entry counts; otherwise the LATEST entry for the binding. The entry must be within
        ``advice_ttl`` of its ``logged_at``, verify (configuration, binding, counters, arithmetic) and have been
        logged for exactly the passed ``(adapter_id, version)`` candidate set. The explanation is rebuilt from
        the logged snapshot (``logged_explanation``)."""
        if not (_log_acceptable(self.log) and isinstance(plan_binding_hash, str)
                and _SHA256.fullmatch(plan_binding_hash)) or self.log is None or self.log.anchor is None:
            return None
        try:
            wanted = None if audit_ref is None else (int(audit_ref[0]), str(audit_ref[1]))
            refs = {adapter_ref_for(str(adapter_id), str(version), self._identity_key): str(adapter_id)
                    for adapter_id, version in candidates}
            view = self.log.anchored_view()
        except (LearningStoreError, LearningRecordError, StoreLockTimeout, OSError, ValueError, TypeError,
                IndexError):
            return None
        matching = [entry for entry in view.entries if entry.kind is EntryKind.ADVICE
                    and entry.plan_binding_hash == plan_binding_hash and entry.seq <= view.checkpoint.seq
                    and (wanted is None or (entry.seq, entry.entry_hash) == wanted)]
        if not matching or len(refs) != len(tuple(candidates)):
            return None
        entry = matching[-1]
        now = self._clock()
        if entry.when > now + FUTURE_SKEW or now - entry.when > self.config.advice_ttl:
            return None  # outside the advice validity period
        if self.verify(entry).level is VerificationLevel.REJECTED:
            return None
        if sorted(item.adapter_ref for item in entry.candidates) != sorted(refs):
            return None
        if entry.context_hash is None:
            return None if entry.advice else ()
        context_hash = entry.context_hash
        return tuple(Advice(adapter_id=refs[item.adapter_ref], score=item.score,
                            sample_size=item.snapshot.sample_size, explanation=logged_explanation(item),
                            profile_hash=item.snapshot.profile_hash, context_hash=context_hash)
                     for item in entry.advice)

    def profiles(self, records: Iterable[OutcomeRecord]) -> dict[ProfileKey, CapabilityPerformanceProfile]:
        return build_profiles(records, bucket_keys=self.config.bucket_keys)

    def _qualifying(self, records: Sequence[OutcomeRecord], task_ref: str, capability_ref: str,
                    refs: Sequence[str], features: ContextFeatures) -> list[tuple[str, CapabilityPerformanceProfile]]:
        """(adapter ref, profile) for every candidate with at least ``min_samples`` in the request's bucket."""
        wanted, digest_key = set(refs), self._identity_key
        relevant = [record for record in records if record.task_ref == task_ref
                    and capability_ref_for(record.capability_id, digest_key) == capability_ref
                    and adapter_ref_for(record.adapter_id, record.adapter_version, digest_key) in wanted]
        bucket = context_bucket(features, self.config.bucket_keys)
        by_ref = {adapter_ref_for(key.adapter_id, key.adapter_version, digest_key): profile
                  for key, profile in self.profiles(relevant).items()
                  if key.bucket == bucket and key.resource_ref == ALL_RESOURCES}
        return [(ref, by_ref[ref]) for ref in sorted(refs)
                if ref in by_ref and by_ref[ref].sample_size >= self.config.min_samples]

    def verify(self, entry: AdviceLogEntry, *, against_logged_config: bool = False,
               use_records: bool = True) -> AdviceVerification:
        """Verify one logged advice set. An entry whose configuration differs from this advisor's is
        REJECTED unless ``against_logged_config`` explicitly asks to verify it against the persisted
        configuration (reported as ``ConfigSource.LOGGED``)."""
        source = ConfigSource.LOGGED if against_logged_config else ConfigSource.ADVISOR
        digests = digest_mode(entry.task_ref) if entry.task_ref else self.identity_digests

        def result(level: VerificationLevel, code: str) -> AdviceVerification:
            return AdviceVerification(level, code, source, digests)

        if entry.kind is not EntryKind.ADVICE or entry.status is None:
            return result(VerificationLevel.REJECTED, "ADVICE_ENTRY_NOT_ADVICE")
        if (entry.advisor_version is None or entry.min_samples is None
                or entry.config_hash != config_hash_of(advisor_version=entry.advisor_version,
                                                       min_samples=entry.min_samples,
                                                       bucket_keys=entry.bucket_keys)):
            return result(VerificationLevel.REJECTED, "ADVICE_CONFIG_HASH_MISMATCH")
        if entry.advisor_version != ADVISOR_VERSION:
            return result(VerificationLevel.REJECTED, "ADVISOR_VERSION_UNSUPPORTED")
        if not MIN_SAMPLES_FLOOR <= entry.min_samples <= 1_000_000:
            return result(VerificationLevel.REJECTED, "ADVICE_CONFIG_INVALID")  # no real advisor allows it
        same_config = entry.config_hash == self.config.config_hash
        if not same_config and not against_logged_config:
            return result(VerificationLevel.REJECTED, "ADVICE_CONFIG_DIFFERS")
        problem = _shape_problem(entry) or next(
            (code for code in (_snapshot_problem(item, entry.min_samples) for item in entry.advice) if code), None)
        if problem is not None:
            return result(VerificationLevel.REJECTED, problem)
        if entry.status not in COMPUTED_STATUSES:
            return result(VerificationLevel.DERIVATION_VERIFIED, "NO_ADVICE_GIVEN")
        if not use_records or not same_config:
            return result(VerificationLevel.SCORE_ARITHMETIC_VERIFIED, "SCORE_FOLLOWS_FROM_SNAPSHOT")
        try:
            derived = self.rederive(entry)
        except _SOURCE_ERRORS:
            return result(VerificationLevel.SCORE_ARITHMETIC_VERIFIED, "RECORDS_UNAVAILABLE")
        if derived != entry.advice:
            return result(VerificationLevel.SCORE_ARITHMETIC_VERIFIED, "RECORDS_DO_NOT_REPRODUCE")
        return result(VerificationLevel.DERIVATION_VERIFIED, "RECORDS_REPRODUCE_ADVICE")

    def rederive(self, entry: AdviceLogEntry) -> tuple[LoggedAdvice, ...]:
        """Recompute a logged advice set from the CURRENT records (no logging); refuses a changed config.
        Historical entries whose records were pruned are verified from their snapshots instead."""
        if entry.config_hash != self.config.config_hash or entry.advisor_version != ADVISOR_VERSION:
            raise LearningStoreError("ADVISOR_CONFIG_CHANGED")
        if entry.status not in COMPUTED_STATUSES or entry.task_ref is None or entry.capability_ref is None:
            return ()
        refs = [item.adapter_ref for item in entry.candidates]
        qualifying = self._qualifying(tuple(self._source.read()), entry.task_ref, entry.capability_ref, refs,
                                      entry.context_features)
        return tuple(logged_advice(profile, ref, self.config.min_samples) for ref, profile in qualifying)


def _log_acceptable(log: AdviceLog | None) -> bool:
    return log is not None and (log.durable or log.for_tests)


def _candidate_pairs(candidates: Sequence[AdapterDescriptor], capability_id: str,
                     key: IdentityKey | None) -> list[tuple[str, AdapterDescriptor]] | None:
    """(adapter ref, descriptor) sorted by ref; None when the set is malformed (no advice, never a guess)."""
    if len(candidates) > MAX_CANDIDATES:
        return None
    pairs: dict[str, AdapterDescriptor] = {}
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, AdapterDescriptor) or candidate.capability_id != capability_id:
            return None
        if candidate.adapter_id in seen:
            return None
        seen.add(candidate.adapter_id)
        try:
            pairs[adapter_ref_for(candidate.adapter_id, candidate.version, key)] = candidate
        except LearningRecordError:
            return None
    return sorted(pairs.items())
