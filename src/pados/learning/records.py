# SPDX-License-Identifier: AGPL-3.0-only
"""Outcome and evaluation records: authenticated, content-free metadata the Learning Plane may observe.

An ``OutcomeRecord`` describes how one capability stage went. Every field is a closed enum, a registered
identifier, a bucket, a count/metric, a hash or an opaque digest:

* ``failure_code`` is the closed ``FailureCode`` enum (anything else maps to ``ADAPTER_FAILURE``) and
  ``hardware_class`` the closed ``HardwareClass`` enum;
* resource ids, task types, decision refs and evaluator refs are persisted only as opaque digests
  (``res-``/``task-``/``dec-``/``evr-`` + 16 hex), never as labels;
* ``capability_id``/``adapter_id``/``adapter_version`` of an outcome are registered descriptor identities,
  authenticated against the trusted stage receipt (which binds the descriptor digests); every OTHER
  persistence path (evaluation records, advice-log candidates) stores them only as ``cap-``/``adp-`` digests;
* context features come from the closed ``CONTEXT_FEATURES`` vocabulary.

Identity digests are unkeyed SHA-256 by default (reported as ``IDENTITY_DIGESTS_UNKEYED``: a guessable
low-entropy label can be confirmed by dictionary hashing). When the operator configures an ``IdentityKey``
(resolved once through ``pados.enterprise.secrets.SecretBoundary`` from a ``secretref://`` reference), every
digest is HMAC-SHA256 under that key with the same prefix and a scheme tag (``task-k1-<hex>``), so it cannot be
confirmed without the key. The key lives only in memory: it is never persisted, rendered or pickled. The same
key must be given to the record builder, the ``OutcomeStore`` and the ``OutcomeAdvisor``.

Ingestion is authenticated (``OutcomeAuthenticator``): a record is accepted only when its stage receipt hash
resolves, through the executor's trusted receipt store, to a structurally valid ``PlanReceipt`` that contains
that stage, the execution facts equal the trusted stage receipt, the human decision equals the verdict
resolved from the existing governed approval path, and the task type and classification equal the trusted
plan context (``resolve_plan_context``). A decision is never inferred. The execution time (``executed_at``)
is the trusted stage receipt's ``finished_at``; retention ages a record from it, never from the ingestion
time (``recorded_at``), so re-ingesting an old receipt cannot extend its life. Context features are
authenticated when the trusted plan context records them (``features_authenticated``); stage-supplied
features are kept but marked unauthenticated and never used for bucketing. The authenticator is wired by the
integrator to the fabric executor's receipt store, plan context and approval path; this package only
consumes it.

An ``EvaluationRecord`` is an offline benchmark result bound to a dataset snapshot hash.
Records are observations, never authority (``semantic_authority`` is not a field they can carry).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.capability.model import Modality
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus
from pados.connectivity.state import ConnectivityState
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import SecretBoundary
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

OUTCOME_SCHEMA = "pados.learning.outcome.v3"
EVALUATION_SCHEMA = "pados.learning.evaluation.v3"
CLOCK_SKEW = timedelta(minutes=5)  # tolerated clock difference for "future" and "before execution" checks

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UPPER_ID = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")  # capability ids (before digesting)
# Task types (before digesting): the Capability Fabric's TaskContract form (lowercase, dotted) or UPPER_SNAKE.
_TASK_TYPE = re.compile(r"^(?:[A-Z][A-Z0-9_]{2,63}|[a-z][a-z0-9_.-]{2,63})$")
_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")  # raw resource ids / decision / evaluator refs
KEYED_SCHEME = "k1"  # HMAC-SHA256 identity digests, scheme version 1
_DIGEST = {prefix: re.compile(rf"^{prefix}-(?:{KEYED_SCHEME}-)?[0-9a-f]{{16}}$")
           for prefix in ("res", "task", "dec", "evr")}
_IDENTITY_DIGEST = {prefix: re.compile(rf"^{prefix}-(?:{KEYED_SCHEME}-)?[0-9a-f]{{24}}$") for prefix in ("adp", "cap")}
MIN_IDENTITY_KEY_BYTES = 32
ADAPTER_REF_PATTERN = _IDENTITY_DIGEST["adp"].pattern
CAPABILITY_REF_PATTERN = _IDENTITY_DIGEST["cap"].pattern

MAX_CONTEXT_FEATURES = 8
MAX_LATENCY_MS = 7 * 24 * 3600 * 1000
MAX_COST_UNITS = 1e9
MAX_RETRIES = 100
MAX_RECORD_BYTES = 4096

_LANGUAGES = frozenset({"en", "zh-Hant", "zh-Hans", "ja", "ko", "de", "fr", "es", "it", "pt", "nl", "ru", "ar",
                        "hi", "th", "vi", "id", "tr", "pl", "mixed", "unknown"})

# The closed context vocabulary a stage may report. Values are enumerations, never free text, so a feature
# cannot carry a file name, a sentence of a document or a prompt.
CONTEXT_FEATURES: Mapping[str, frozenset[str]] = MappingProxyType({
    "document_language": _LANGUAGES,
    "scan_type": frozenset({"BORN_DIGITAL", "SCANNED", "PHOTO", "MIXED", "UNKNOWN"}),
    "mime_class": frozenset({m.value for m in Modality} | {"UNKNOWN"}),
    "text_layer": frozenset({"PRESENT", "PARTIAL", "ABSENT", "UNKNOWN"}),
    "page_bucket": frozenset({"P1", "P2_10", "P11_50", "P51_200", "P201_PLUS", "UNKNOWN"}),
    "size_bucket": frozenset({"XS", "S", "M", "L", "XL", "UNKNOWN"}),
    "layout": frozenset({"SIMPLE", "MULTI_COLUMN", "TABLE_HEAVY", "FORM", "HANDWRITTEN", "UNKNOWN"}),
    "code_language": frozenset({"python", "typescript", "javascript", "go", "rust", "java", "csharp", "cpp", "c",
                                "sql", "shell", "other", "unknown"}),
})

ContextFeatures = tuple[tuple[str, str], ...]


class LearningRecordError(ValueError):
    """Closed-code refusal; never echoes the offending value."""


def validate_context_features(features: Mapping[str, str] | Iterable[tuple[str, str]]) -> ContextFeatures:
    """Canonical (sorted) context features from the closed vocabulary; fail closed on anything else."""
    if isinstance(features, Mapping):
        pairs: list[Any] = list(features.items())
    elif isinstance(features, (list, tuple)):
        pairs = list(features)
    else:
        raise LearningRecordError("CONTEXT_FEATURES_INVALID")
    if len(pairs) > MAX_CONTEXT_FEATURES:
        raise LearningRecordError("CONTEXT_FEATURES_TOO_MANY")
    seen: dict[str, str] = {}
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise LearningRecordError("CONTEXT_FEATURE_INVALID")
        name, value = pair
        if not isinstance(name, str) or name not in CONTEXT_FEATURES:
            raise LearningRecordError("CONTEXT_FEATURE_UNKNOWN")
        if not isinstance(value, str) or value not in CONTEXT_FEATURES[name]:
            raise LearningRecordError("CONTEXT_FEATURE_VALUE_INVALID")
        if name in seen:
            raise LearningRecordError("CONTEXT_FEATURE_DUPLICATE")
        seen[name] = value
    return tuple(sorted(seen.items()))


def context_bucket(features: ContextFeatures, keys: Iterable[str] | None = None) -> str:
    """Canonical bucket label: the features projected onto ``keys`` (all when None); ``*`` when empty."""
    wanted = None if keys is None else frozenset(keys)
    kept = [f"{name}={value}" for name, value in features if wanted is None or name in wanted]
    return ";".join(kept) if kept else "*"


def page_bucket(pages: int) -> str:
    """Map a page count to its closed bucket (the count itself is never recorded)."""
    if pages < 1:
        return "UNKNOWN"
    for limit, label in ((1, "P1"), (10, "P2_10"), (50, "P11_50"), (200, "P51_200")):
        if pages <= limit:
            return label
    return "P201_PLUS"


def size_bucket(size_bytes: int) -> str:
    """Map a byte size to its closed bucket (XS < 64 KiB <= S < 1 MiB <= M < 16 MiB <= L < 256 MiB <= XL)."""
    if size_bytes < 0:
        return "UNKNOWN"
    for limit, label in ((64 * 1024, "XS"), (1024**2, "S"), (16 * 1024**2, "M"), (256 * 1024**2, "L")):
        if size_bytes < limit:
            return label
    return "XL"


class FailureCode(StrEnum):
    """Closed failure vocabulary; any other adapter/receipt code is recorded as ``ADAPTER_FAILURE``."""

    ADAPTER_FAILURE = "ADAPTER_FAILURE"
    PARTIAL_OUTPUT = "PARTIAL_OUTPUT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INPUT_INVALID = "INPUT_INVALID"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"


class HardwareClass(StrEnum):
    """Closed hardware classes (the ``pados.resources.hardware.AcceleratorRuntime`` tokens plus UNKNOWN)."""

    CPU = "CPU"
    CUDA = "CUDA"
    ROCM = "ROCM"
    METAL = "METAL"
    DIRECTML = "DIRECTML"
    VULKAN = "VULKAN"
    ONEAPI = "ONEAPI"
    NPU = "NPU"
    UNKNOWN = "UNKNOWN"


class MetricName(StrEnum):
    """Closed offline-evaluation metric names."""

    CHAR_ACCURACY = "char_accuracy"
    WORD_ACCURACY = "word_accuracy"
    CER = "cer"
    WER = "wer"
    EXACT_MATCH = "exact_match"
    PRECISION = "precision"
    RECALL = "recall"
    F1 = "f1"
    LAYOUT_F1 = "layout_f1"
    TABLE_F1 = "table_f1"
    LATENCY_MS = "latency_ms"
    COST_UNITS = "cost_units"


class HumanDecision(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NONE = "NONE"


def failure_code_for(code: str | None, *, partial: bool = False) -> FailureCode | None:
    """Closed failure code for a receipt: exact enum members only; anything else is ADAPTER_FAILURE."""
    if code is None:
        return FailureCode.PARTIAL_OUTPUT if partial else None
    try:
        return FailureCode(code)
    except ValueError:
        return FailureCode.ADAPTER_FAILURE


class IdentityDigests(StrEnum):
    """How identity digests were computed (reported by ``verify`` and in explanations)."""

    KEYED = "IDENTITY_DIGESTS_KEYED"
    UNKEYED = "IDENTITY_DIGESTS_UNKEYED"


class IdentityKey:
    """Operator-configured HMAC key for identity digests. Held in memory only: it has no serialized form, its
    repr is redacted and it refuses pickling; nothing in this package persists it."""

    __slots__ = ("_material",)

    def __init__(self, material: bytes) -> None:
        if not isinstance(material, bytes) or len(material) < MIN_IDENTITY_KEY_BYTES:
            raise LearningRecordError("IDENTITY_KEY_TOO_SHORT")
        self._material = hmac.new(b"pados.learning.identity-key.v1", material, hashlib.sha256).digest()

    @classmethod
    def from_secret_ref(cls, boundary: SecretBoundary, ref: str) -> IdentityKey:
        """Resolve the operator's ``secretref://`` key through the SecretBoundary (that one reference only)."""
        with boundary.resolved({ref}, authorized_refs={ref}) as values:
            return cls(values[ref].reveal().encode("utf-8"))

    def digest(self, prefix: str, value: str) -> str:
        return hmac.new(self._material, f"{prefix}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return "IdentityKey([REDACTED])"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("IDENTITY_KEY_NOT_SERIALIZABLE")


def identity_digests(key: IdentityKey | None) -> IdentityDigests:
    return IdentityDigests.UNKEYED if key is None else IdentityDigests.KEYED


def digest_mode(ref: str) -> IdentityDigests:
    """Whether a persisted ``<prefix>-[k1-]<hex>`` digest is keyed."""
    parts = ref.split("-")
    return IdentityDigests.KEYED if len(parts) == 3 and parts[1] == KEYED_SCHEME else IdentityDigests.UNKEYED


def _keyed(prefix: str, value: str, length: int, key: IdentityKey | None) -> str:
    if key is None:
        return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]}"
    return f"{prefix}-{KEYED_SCHEME}-{key.digest(prefix, value)[:length]}"


def _digest(prefix: str, value: str, pattern: re.Pattern[str], code: str, length: int = 16,
            key: IdentityKey | None = None) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LearningRecordError(code)
    return _keyed(prefix, value, length, key)


def resource_ref_for(resource_id: str, key: IdentityKey | None = None) -> str:
    """Opaque resource reference: the raw resource id is never persisted."""
    return _digest("res", resource_id, _REF, "RESOURCE_ID_INVALID", key=key)


def task_ref_for(task_type: str, key: IdentityKey | None = None) -> str:
    """Opaque task-type reference: the task label is never persisted."""
    return _digest("task", task_type, _TASK_TYPE, "TASK_TYPE_INVALID", key=key)


def decision_ref_for(decision_ref: str, key: IdentityKey | None = None) -> str:
    """Opaque reference to a governed decision (the approval path's own id is never persisted)."""
    return _digest("dec", decision_ref, _REF, "HUMAN_DECISION_REF_INVALID", key=key)


def evaluator_ref_for(evaluator: str, key: IdentityKey | None = None) -> str:
    return _digest("evr", evaluator, _REF, "EVALUATOR_REF_INVALID", key=key)


def capability_ref_for(capability_id: str, key: IdentityKey | None = None) -> str:
    """Opaque capability identity for persistence paths other than authenticated outcomes."""
    return _digest("cap", capability_id, _UPPER_ID, "CAPABILITY_ID_INVALID", 24, key)


def adapter_ref_for(adapter_id: str, adapter_version: str, key: IdentityKey | None = None) -> str:
    """Opaque adapter identity (id AND version) for persistence paths other than authenticated outcomes."""
    if not isinstance(adapter_version, str) or not _VERSION.fullmatch(adapter_version):
        raise LearningRecordError("ADAPTER_VERSION_INVALID")
    if not isinstance(adapter_id, str) or not _ADAPTER_ID.fullmatch(adapter_id):
        raise LearningRecordError("ADAPTER_ID_INVALID")
    return _keyed("adp", f"{adapter_id}@{adapter_version}", 24, key)


def _sha(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("LEARNING_HASH_INVALID")
    return value


def _utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("LEARNING_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("LEARNING_TIMESTAMP_NOT_AWARE")
    return parsed.astimezone(UTC).isoformat()


def _pattern(value: str, pattern: re.Pattern[str], code: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(code)
    return value


def _schema_keys(record: Mapping[str, Any], allowed: frozenset[str]) -> None:
    if not isinstance(record, Mapping):
        raise DataMinimizationViolation("RECORD_MUST_BE_MAPPING", "")
    for key in record:
        if key not in allowed:
            raise DataMinimizationViolation("FIELD_NOT_IN_SCHEMA", key if isinstance(key, str) else "")


@dataclass(frozen=True)
class TrustedPlanContext:
    """What the trusted plan side recorded for a plan: its task type, classification and (when the planner
    recorded an advice context) the context features. ``context_features=None`` means none were recorded."""

    task_type: str
    classification: DataClassification
    context_features: Mapping[str, str] | None = None


class OutcomeAuthenticator(Protocol):
    """Trusted resolution of receipts, plan context and decisions (wired by the integrator; never
    caller-asserted).

    ``resolve_receipt`` returns the executor's own ``PlanReceipt`` containing the stage receipt with this
    hash, or None; the stage receipt's ``finished_at`` is the trusted execution time.
    ``resolve_plan_context`` returns the trusted task type, classification and recorded context features of
    the plan with this ``plan_hash``, or None. ``resolve_decision`` returns the verdict recorded for a
    decision id in the existing governed approval path (ACCEPTED / REJECTED), or None when no verdict is
    recorded.
    """

    def resolve_receipt(self, stage_receipt_hash: str) -> PlanReceipt | None: ...

    def resolve_plan_context(self, plan_hash: str) -> TrustedPlanContext | None: ...

    def resolve_decision(self, decision_ref: str) -> HumanDecision | None: ...


class ResourceUsage(BaseModel):
    """Bounded resource summary (counters only)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_ms: int = Field(default=0, ge=0, le=2**40)
    peak_ram_mb: int = Field(default=0, ge=0, le=4_194_304)
    gpu_ms: int = Field(default=0, ge=0, le=2**40)
    peak_vram_mb: int = Field(default=0, ge=0, le=1_048_576)


def outcome_id_for(receipt_hash: str) -> str:
    """One outcome per stage receipt: the id is derived from the receipt hash (idempotent appends)."""
    return f"oc-{_sha(receipt_hash)[:40]}"


class OutcomeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = OUTCOME_SCHEMA
    outcome_id: str
    task_ref: str
    capability_id: str
    adapter_id: str
    adapter_version: str
    resource_ref: str
    hardware_class: HardwareClass
    network_state: ConnectivityState
    classification: DataClassification
    context_features: ContextFeatures = ()
    features_authenticated: bool = False  # True only when the trusted plan context recorded these features
    latency_ms: int = Field(ge=0, le=MAX_LATENCY_MS)
    cost_units: float = Field(default=0.0, ge=0.0, le=MAX_COST_UNITS)
    success: bool
    failure_code: FailureCode | None = None
    verification: VerificationStatus
    human_decision: HumanDecision = HumanDecision.NONE
    human_decision_ref: str | None = None
    quality_metric: float | None = Field(default=None, ge=0.0, le=1.0)
    retry_count: int = Field(default=0, ge=0, le=MAX_RETRIES)
    resource_usage: ResourceUsage = ResourceUsage()
    plan_hash: str
    receipt_hash: str
    executed_at: str  # trusted: the stage receipt's finished_at; retention ages the record from it
    recorded_at: str  # ingestion time (informational; never extends retention)

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != OUTCOME_SCHEMA:
            raise ValueError("OUTCOME_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("capability_id")
    @classmethod
    def capability_pattern(cls, value: str) -> str:
        return _pattern(value, _UPPER_ID, "LEARNING_IDENTIFIER_INVALID")

    @field_validator("task_ref")
    @classmethod
    def task_digest(cls, value: str) -> str:
        return _pattern(value, _DIGEST["task"], "TASK_REF_NOT_A_DIGEST")

    @field_validator("resource_ref")
    @classmethod
    def resource_digest(cls, value: str) -> str:
        return _pattern(value, _DIGEST["res"], "RESOURCE_REF_NOT_A_DIGEST")

    @field_validator("human_decision_ref")
    @classmethod
    def decision_digest(cls, value: str | None) -> str | None:
        return None if value is None else _pattern(value, _DIGEST["dec"], "HUMAN_DECISION_REF_NOT_A_DIGEST")

    @field_validator("adapter_id")
    @classmethod
    def adapter_id_pattern(cls, value: str) -> str:
        return _pattern(value, _ADAPTER_ID, "ADAPTER_ID_INVALID")

    @field_validator("adapter_version")
    @classmethod
    def version_pattern(cls, value: str) -> str:
        return _pattern(value, _VERSION, "ADAPTER_VERSION_INVALID")

    @field_validator("outcome_id")
    @classmethod
    def refs(cls, value: str) -> str:
        return _pattern(value, _REF, "LEARNING_REFERENCE_INVALID")

    @field_validator("context_features", mode="before")
    @classmethod
    def closed_features(cls, value: Any) -> ContextFeatures:
        return validate_context_features(value)

    @field_validator("cost_units", "quality_metric")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("LEARNING_NUMBER_NOT_FINITE")
        return value

    @field_validator("plan_hash", "receipt_hash")
    @classmethod
    def hashes(cls, value: str) -> str:
        return _sha(value)

    @field_validator("recorded_at", "executed_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def consistent(self) -> OutcomeRecord:
        if self.recorded_at_time < self.executed_at_time - CLOCK_SKEW:
            raise ValueError("OUTCOME_RECORDED_BEFORE_EXECUTION")
        if self.outcome_id != outcome_id_for(self.receipt_hash):
            raise ValueError("OUTCOME_ID_NOT_DERIVED_FROM_RECEIPT")
        if self.success == (self.failure_code is not None):
            raise ValueError("OUTCOME_SUCCESS_FAILURE_CODE_INCONSISTENT")
        if not self.success and self.verification is VerificationStatus.PASSED:
            raise ValueError("FAILED_OUTCOME_CANNOT_PASS_VERIFICATION")
        if (self.human_decision is HumanDecision.NONE) != (self.human_decision_ref is None):
            raise ValueError("HUMAN_DECISION_REQUIRES_RECORDED_REF")
        self.to_record()  # credential-shaped values are refused at construction, not only at write time
        return self

    @property
    def features(self) -> dict[str, str]:
        return dict(self.context_features)

    @property
    def bucket_features(self) -> ContextFeatures:
        """Features used for bucketing: only authenticated ones (unauthenticated features never bucket)."""
        return self.context_features if self.features_authenticated else ()

    @property
    def executed_at_time(self) -> datetime:
        return datetime.fromisoformat(self.executed_at)

    @property
    def recorded_at_time(self) -> datetime:
        return datetime.fromisoformat(self.recorded_at)

    @property
    def record_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def to_record(self) -> dict[str, Any]:
        """The persisted form, checked by the shared minimization guard against the explicit schema."""
        record = self.model_dump(mode="json")
        assert_minimized(record, allowed_keys=OUTCOME_KEYS, max_string=128, max_items=32,
                         max_total_bytes=MAX_RECORD_BYTES)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> OutcomeRecord:
        _schema_keys(record, OUTCOME_KEYS)  # the full minimization guard runs in validation
        return cls.model_validate(dict(record))

    @classmethod
    def from_receipts(cls, plan: PlanReceipt, stage: StageReceipt, *, authenticator: OutcomeAuthenticator,
                      hardware_class: HardwareClass, network_state: ConnectivityState, recorded_at: datetime,
                      context_features: Mapping[str, str] | None = None, cost_units: float = 0.0,
                      quality_metric: float | None = None, retry_count: int = 0,
                      resource_usage: ResourceUsage | None = None,
                      identity_key: IdentityKey | None = None) -> OutcomeRecord:
        """Build a record from the TRUSTED receipts and plan context; the verdict comes from the approval path.

        Task type, classification and execution time come only from the trusted side. ``context_features``
        is used only when the trusted plan context recorded none; such features are stored as
        unauthenticated and never bucket. Features that conflict with recorded trusted ones are refused."""
        trusted = _resolve_plan(authenticator, stage.receipt_hash)
        if trusted is None or trusted.envelope_hash != plan.envelope_hash:
            raise LearningRecordError("LEARNING_OUTCOME_UNAUTHENTICATED")
        facts = trusted_facts(trusted, stage.receipt_hash, authenticator, identity_key)
        if facts is None:
            raise LearningRecordError("LEARNING_OUTCOME_UNAUTHENTICATED")
        if not facts["features_authenticated"]:
            facts["context_features"] = validate_context_features(context_features or {})
        elif context_features is not None and validate_context_features(context_features) != facts[
                "context_features"]:
            raise LearningRecordError("CONTEXT_FEATURES_CONFLICT_WITH_TRUSTED_PLAN")
        return cls(hardware_class=hardware_class, network_state=network_state, cost_units=cost_units,
                   quality_metric=quality_metric, retry_count=retry_count,
                   resource_usage=resource_usage or ResourceUsage(), recorded_at=recorded_at.isoformat(), **facts)


OUTCOME_KEYS = frozenset(OutcomeRecord.model_fields)
# Facts a record must share with its trusted stage receipt, trusted plan context and governed decision.
AUTHENTICATED_FIELDS = ("outcome_id", "task_ref", "capability_id", "adapter_id", "adapter_version",
                        "resource_ref", "classification", "latency_ms", "success", "failure_code", "verification",
                        "human_decision", "human_decision_ref", "plan_hash", "receipt_hash", "executed_at",
                        "features_authenticated")


def _resolve_plan(authenticator: OutcomeAuthenticator, receipt_hash: str) -> PlanReceipt | None:
    try:
        plan = authenticator.resolve_receipt(receipt_hash)
    except Exception:  # noqa: BLE001 - any resolver failure is "not authenticated" (fail closed)
        return None
    return plan if isinstance(plan, PlanReceipt) else None


def _plan_context(authenticator: OutcomeAuthenticator, plan_hash: str,
                  key: IdentityKey | None) -> tuple[str, DataClassification, ContextFeatures | None] | None:
    """(task_ref, classification, authenticated features or None) from the trusted plan context, or None."""
    try:
        context = authenticator.resolve_plan_context(plan_hash)
        if not isinstance(context, TrustedPlanContext):
            return None
        features = None if context.context_features is None else validate_context_features(
            context.context_features)
        return task_ref_for(context.task_type, key), DataClassification(context.classification), features
    except Exception:  # noqa: BLE001 - an unresolvable or malformed plan context fails closed
        return None


def trusted_facts(plan: PlanReceipt, receipt_hash: str, authenticator: OutcomeAuthenticator,
                  key: IdentityKey | None = None) -> dict[str, Any] | None:
    """Facts derived from a trusted plan receipt, its trusted plan context and the governed verdict, or None."""
    if not plan.verify_structure():
        return None
    stage = next((item for item in plan.stages if item.receipt_hash == receipt_hash), None)
    if stage is None:
        return None
    context = _plan_context(authenticator, plan.plan_hash, key)
    if context is None:
        return None
    task_ref, classification, features = context
    verdict: HumanDecision | None = None
    if plan.human_decision_ref is not None:
        try:
            verdict = authenticator.resolve_decision(plan.human_decision_ref)
        except Exception:  # noqa: BLE001 - an unresolvable decision fails closed
            return None
        if verdict is not None and verdict not in {HumanDecision.ACCEPTED, HumanDecision.REJECTED}:
            return None
    failure = failure_code_for(stage.failure_code, partial=stage.partial)
    try:
        resource_ref = resource_ref_for(stage.resource_id, key)
        executed_at = _utc_timestamp(stage.finished_at)
    except ValueError:
        return None
    return {"outcome_id": outcome_id_for(receipt_hash), "task_ref": task_ref, "classification": classification,
            "capability_id": stage.capability_id, "adapter_id": stage.adapter_id,
            "adapter_version": stage.adapter_version, "resource_ref": resource_ref,
            "latency_ms": stage.duration_ms, "success": failure is None, "failure_code": failure,
            "verification": stage.verification, "human_decision": verdict or HumanDecision.NONE,
            "human_decision_ref": None if verdict is None or plan.human_decision_ref is None
            else decision_ref_for(plan.human_decision_ref, key),
            "plan_hash": plan.plan_hash, "receipt_hash": receipt_hash, "executed_at": executed_at,
            "features_authenticated": features is not None, "context_features": features or ()}


def authenticate_outcome(record: OutcomeRecord, authenticator: OutcomeAuthenticator,
                         key: IdentityKey | None = None) -> bool:
    """True only when the record's facts equal its trusted receipt, trusted plan context and the governed
    verdict; authenticated features must equal the trusted ones."""
    plan = _resolve_plan(authenticator, record.receipt_hash)
    if plan is None:
        return False
    facts = trusted_facts(plan, record.receipt_hash, authenticator, key)
    if facts is None or any(getattr(record, name) != facts[name] for name in AUTHENTICATED_FIELDS):
        return False
    return not record.features_authenticated or record.context_features == facts["context_features"]


def evaluation_id_for(*, task_ref: str, capability_ref: str, adapter_ref: str, dataset_snapshot_hash: str,
                      metric_name: str, context_features: ContextFeatures) -> str:
    """One result per (adapter version, dataset snapshot, metric, context): re-runs are idempotent."""
    digest = canonical_hash({"kind": EVALUATION_SCHEMA, "task_ref": task_ref, "capability_ref": capability_ref,
                             "adapter_ref": adapter_ref, "dataset_snapshot_hash": dataset_snapshot_hash,
                             "metric_name": metric_name,
                             "context_features": [list(item) for item in context_features]})
    return f"ev-{digest[:40]}"


class EvaluationRecord(BaseModel):
    """An offline benchmark result: one metric of one adapter version on one dataset snapshot.

    The capability and adapter (id AND version) are persisted only as ``cap-``/``adp-`` digests; an
    operator maps them back through the registry (``capability_ref_for`` / ``adapter_ref_for``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = EVALUATION_SCHEMA
    evaluation_id: str
    task_ref: str
    capability_ref: str
    adapter_ref: str
    dataset_snapshot_hash: str
    dataset_size: int = Field(ge=1, le=10_000_000)
    metric_name: MetricName
    metric_value: float = Field(ge=-1e9, le=1e9)
    higher_is_better: bool = True
    context_features: ContextFeatures = ()
    evaluator_ref: str
    evidence_hash: str | None = None  # content hash of the benchmark report artifact
    evaluated_at: str

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != EVALUATION_SCHEMA:
            raise ValueError("EVALUATION_SCHEMA_UNSUPPORTED")
        return value

    @field_validator("capability_ref")
    @classmethod
    def capability_digest(cls, value: str) -> str:
        return _pattern(value, _IDENTITY_DIGEST["cap"], "CAPABILITY_REF_NOT_A_DIGEST")

    @field_validator("adapter_ref")
    @classmethod
    def adapter_digest(cls, value: str) -> str:
        return _pattern(value, _IDENTITY_DIGEST["adp"], "ADAPTER_REF_NOT_A_DIGEST")

    @field_validator("task_ref")
    @classmethod
    def task_digest(cls, value: str) -> str:
        return _pattern(value, _DIGEST["task"], "TASK_REF_NOT_A_DIGEST")

    @field_validator("evaluator_ref")
    @classmethod
    def evaluator_digest(cls, value: str) -> str:
        return _pattern(value, _DIGEST["evr"], "EVALUATOR_REF_NOT_A_DIGEST")

    @field_validator("evaluation_id")
    @classmethod
    def refs(cls, value: str) -> str:
        return _pattern(value, _REF, "LEARNING_REFERENCE_INVALID")

    @field_validator("metric_value")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("LEARNING_NUMBER_NOT_FINITE")
        return value

    @field_validator("context_features", mode="before")
    @classmethod
    def closed_features(cls, value: Any) -> ContextFeatures:
        return validate_context_features(value)

    @field_validator("dataset_snapshot_hash")
    @classmethod
    def snapshot(cls, value: str) -> str:
        return _sha(value)

    @field_validator("evidence_hash")
    @classmethod
    def evidence(cls, value: str | None) -> str | None:
        return None if value is None else _sha(value)

    @field_validator("evaluated_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def derived_id(self) -> EvaluationRecord:
        expected = evaluation_id_for(task_ref=self.task_ref, capability_ref=self.capability_ref,
                                     adapter_ref=self.adapter_ref, dataset_snapshot_hash=self.dataset_snapshot_hash,
                                     metric_name=self.metric_name.value, context_features=self.context_features)
        if self.evaluation_id != expected:
            raise ValueError("EVALUATION_ID_NOT_DERIVED")
        self.to_record()
        return self

    @property
    def record_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def to_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json")
        assert_minimized(record, allowed_keys=EVALUATION_KEYS, max_string=128, max_items=32,
                         max_total_bytes=MAX_RECORD_BYTES)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> EvaluationRecord:
        _schema_keys(record, EVALUATION_KEYS)
        return cls.model_validate(dict(record))

    @classmethod
    def create(cls, *, task_type: str, capability_id: str, adapter_id: str, adapter_version: str,
               dataset_snapshot_hash: str, dataset_size: int, metric_name: MetricName | str, metric_value: float,
               evaluator: str, evaluated_at: datetime, higher_is_better: bool = True,
               context_features: Mapping[str, str] | None = None,
               evidence_hash: str | None = None, identity_key: IdentityKey | None = None) -> EvaluationRecord:
        features = validate_context_features(context_features or {})
        try:
            metric = MetricName(metric_name)
        except ValueError as exc:
            raise LearningRecordError("METRIC_NAME_INVALID") from exc
        task_ref = task_ref_for(task_type, identity_key)
        capability_ref = capability_ref_for(capability_id, identity_key)
        adapter_ref = adapter_ref_for(adapter_id, adapter_version, identity_key)
        evaluation_id = evaluation_id_for(task_ref=task_ref, capability_ref=capability_ref, adapter_ref=adapter_ref,
                                          dataset_snapshot_hash=dataset_snapshot_hash, metric_name=metric.value,
                                          context_features=features)
        return cls(evaluation_id=evaluation_id, task_ref=task_ref, capability_ref=capability_ref,
                   adapter_ref=adapter_ref, dataset_snapshot_hash=dataset_snapshot_hash, dataset_size=dataset_size,
                   metric_name=metric, metric_value=metric_value, higher_is_better=higher_is_better,
                   context_features=features, evaluator_ref=evaluator_ref_for(evaluator, identity_key),
                   evidence_hash=evidence_hash, evaluated_at=evaluated_at.isoformat())


EVALUATION_KEYS = frozenset(EvaluationRecord.model_fields)
