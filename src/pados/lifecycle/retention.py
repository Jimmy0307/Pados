# SPDX-License-Identifier: AGPL-3.0-only
"""Retention classes, storage categories and the data-kind catalog.

Every kind of data PADOS writes is registered here once, with the storage
category it is accounted under, its retention class and whether its raw payload
may ever be purged. Writers outside this module reference a ``DataKind`` instead
of inventing their own retention vocabulary.

* ``R0_EPHEMERAL``   hours/days: temp files, sandbox scratch, raw intermediates.
* ``R1_OPERATIONAL`` bounded operational period (default 14 days, 7-30 unless
  explicitly configured): logs, telemetry, retry metadata, connectivity history.
* ``R2_PROJECT``     project lifecycle: accepted sources, active artifacts.
* ``R3_AUTHORITY``   minimum durable authority metadata. R3 is metadata, not
  "every byte forever": a payload may be purged while hash, status, timestamp,
  receipt reference and decision evidence remain.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class RetentionClass(StrEnum):
    R0_EPHEMERAL = "R0_EPHEMERAL"
    R1_OPERATIONAL = "R1_OPERATIONAL"
    R2_PROJECT = "R2_PROJECT"
    R3_AUTHORITY = "R3_AUTHORITY"


class StorageCategory(StrEnum):
    AUTHORITY_METADATA = "AUTHORITY_METADATA"
    PROJECT_SOURCES = "PROJECT_SOURCES"
    ARTIFACTS = "ARTIFACTS"
    OPERATIONAL_LOGS = "OPERATIONAL_LOGS"
    REMOTE_UPLOADS = "REMOTE_UPLOADS"
    SANDBOX_DATA = "SANDBOX_DATA"
    HERDR_TELEMETRY = "HERDR_TELEMETRY"
    RELEASE_TEST_EVIDENCE = "RELEASE_TEST_EVIDENCE"
    CACHE = "CACHE"


class PayloadState(StrEnum):
    PRESENT = "PRESENT"
    METADATA_RETAINED_PAYLOAD_PURGED = "METADATA_RETAINED_PAYLOAD_PURGED"
    NOT_PERSISTED = "NOT_PERSISTED"


class PurgeRule(StrEnum):
    """What a purge may do to a data kind once it is past its retention window."""

    DELETE_RECORD = "DELETE_RECORD"  # non-authoritative: the whole record may go
    PURGE_PAYLOAD_KEEP_METADATA = "PURGE_PAYLOAD_KEEP_METADATA"  # bytes go, metadata stays
    HUMAN_DECISION_ONLY = "HUMAN_DECISION_ONLY"  # project lifecycle: never automatic
    NEVER = "NEVER"  # authority metadata


class DataKind(StrEnum):
    # R3 authority metadata
    SCOPE_VERSION_TRANSITION = "SCOPE_VERSION_TRANSITION"
    CAS_TRANSITION = "CAS_TRANSITION"
    DECISION_IDENTITY = "DECISION_IDENTITY"
    AUDIT_CHAIN = "AUDIT_CHAIN"
    SOURCE_IDENTITY = "SOURCE_IDENTITY"
    ARTIFACT_HASH = "ARTIFACT_HASH"
    ACCEPTANCE_STATUS = "ACCEPTANCE_STATUS"
    EXECUTION_RECEIPT_SUMMARY = "EXECUTION_RECEIPT_SUMMARY"
    RESULT_RECEIPT_SUMMARY = "RESULT_RECEIPT_SUMMARY"
    REMOTE_INTENT_LEDGER = "REMOTE_INTENT_LEDGER"
    DEPLOYMENT_APPROVAL_LEDGER = "DEPLOYMENT_APPROVAL_LEDGER"
    PRIVILEGED_GRANT_LEDGER = "PRIVILEGED_GRANT_LEDGER"
    MACHINE_INVENTORY = "MACHINE_INVENTORY"
    OUTCOME_RECORD = "OUTCOME_RECORD"
    EVALUATION_RECORD = "EVALUATION_RECORD"
    ADVICE_LOG = "ADVICE_LOG"
    TRAINING_JOB_EVENT_LOG = "TRAINING_JOB_EVENT_LOG"
    MODEL_REGISTRY_LEDGER = "MODEL_REGISTRY_LEDGER"
    TRAINING_DECISION_LEDGER = "TRAINING_DECISION_LEDGER"
    CAPABILITY_DECISION_LEDGER = "CAPABILITY_DECISION_LEDGER"
    PAYLOAD_TOMBSTONE = "PAYLOAD_TOMBSTONE"
    # R3 metadata whose payload may be purged
    REJECTED_ARTIFACT_PAYLOAD = "REJECTED_ARTIFACT_PAYLOAD"
    # R2 project
    PROJECT_SOURCE = "PROJECT_SOURCE"
    ACTIVE_ARTIFACT = "ACTIVE_ARTIFACT"
    DERIVED_DOCUMENT = "DERIVED_DOCUMENT"
    PROJECT_EVIDENCE = "PROJECT_EVIDENCE"
    GOVERNED_PROJECT_CONTENT = "GOVERNED_PROJECT_CONTENT"
    RELEASE_TEST_EVIDENCE = "RELEASE_TEST_EVIDENCE"
    STORAGE_PLACEMENT_INDEX = "STORAGE_PLACEMENT_INDEX"
    # R1 operational
    OPERATIONAL_LOG = "OPERATIONAL_LOG"
    TELEMETRY = "TELEMETRY"
    FAILED_INTERMEDIATE_ARTIFACT = "FAILED_INTERMEDIATE_ARTIFACT"
    NOTIFICATION_DELIVERY = "NOTIFICATION_DELIVERY"
    CONNECTIVITY_HISTORY = "CONNECTIVITY_HISTORY"
    HERDR_TELEMETRY = "HERDR_TELEMETRY"
    QUARANTINED_UPLOAD = "QUARANTINED_UPLOAD"
    MACHINE_CONNECTION_HISTORY = "MACHINE_CONNECTION_HISTORY"
    CAPABILITY_STAGE_EVIDENCE = "CAPABILITY_STAGE_EVIDENCE"
    # R0 ephemeral
    TEMPORARY_FILE = "TEMPORARY_FILE"
    SANDBOX_WORKING_DATA = "SANDBOX_WORKING_DATA"
    RAW_INTERMEDIATE_OUTPUT = "RAW_INTERMEDIATE_OUTPUT"
    TEMPORARY_UPLOAD = "TEMPORARY_UPLOAD"
    DEBUG_TRACE = "DEBUG_TRACE"
    PROBE_DEBUG = "PROBE_DEBUG"
    CACHE_ENTRY = "CACHE_ENTRY"
    CAPABILITY_INTERMEDIATE_ARTIFACT = "CAPABILITY_INTERMEDIATE_ARTIFACT"


@dataclass(frozen=True)
class DataKindPolicy:
    kind: DataKind
    category: StorageCategory
    retention: RetentionClass
    purge_rule: PurgeRule
    authoritative: bool
    description: str
    # When set, the raw payload (never the metadata) becomes purge-eligible once this
    # class's window has elapsed since the triggering decision (e.g. a rejection).
    payload_retention: RetentionClass | None = None


def _p(kind: DataKind, category: StorageCategory, retention: RetentionClass, rule: PurgeRule,
       description: str, payload_retention: RetentionClass | None = None) -> DataKindPolicy:
    return DataKindPolicy(kind, category, retention, rule, retention is RetentionClass.R3_AUTHORITY, description,
                          payload_retention)


_R0, _R1, _R2, _R3 = (RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL,
                      RetentionClass.R2_PROJECT, RetentionClass.R3_AUTHORITY)
_C = StorageCategory
_U = PurgeRule

CATALOG: dict[DataKind, DataKindPolicy] = {
    policy.kind: policy
    for policy in (
        _p(DataKind.SCOPE_VERSION_TRANSITION, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "ScopeVersion transitions"),
        _p(DataKind.CAS_TRANSITION, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "semantic CAS transitions"),
        _p(DataKind.DECISION_IDENTITY, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "decision identity and binding"),
        _p(DataKind.AUDIT_CHAIN, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "immutable audit chain"),
        _p(DataKind.SOURCE_IDENTITY, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "source content hash and lineage"),
        _p(DataKind.ARTIFACT_HASH, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "artifact SHA-256 references"),
        _p(DataKind.ACCEPTANCE_STATUS, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "acceptance / validation status"),
        _p(DataKind.EXECUTION_RECEIPT_SUMMARY, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "ExecutionReceipt summary"),
        _p(DataKind.RESULT_RECEIPT_SUMMARY, _C.AUTHORITY_METADATA, _R3, _U.NEVER, "ResultReceipt summary"),
        _p(DataKind.DEPLOYMENT_APPROVAL_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "consumed deployment approvals (replay protection; hash-chained, no key material)"),
        _p(DataKind.PRIVILEGED_GRANT_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "hash-chained privileged grant ledger (no command text or output)"),
        _p(DataKind.MACHINE_INVENTORY, _C.AUTHORITY_METADATA, _R2, _U.HUMAN_DECISION_ONLY,
           "operator machine inventory and pinned host keys (references only, never passwords)"),
        _p(DataKind.EVALUATION_RECORD, _C.ARTIFACTS, _R2, _U.HUMAN_DECISION_ONLY,
           "offline capability benchmark results (metrics and hashes only)"),
        _p(DataKind.MODEL_REGISTRY_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "model registry candidate states (hashes, provenance refs; promotion only via governed decision)"),
        _p(DataKind.TRAINING_DECISION_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "consumed training/promotion decisions (replay protection; no key material)"),
        _p(DataKind.CAPABILITY_DECISION_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "consumed capability human-execution decisions (decision ids and times only; replay protection)"),
        _p(DataKind.TRAINING_JOB_EVENT_LOG, _C.ARTIFACTS, _R2, _U.HUMAN_DECISION_ONLY,
           "hash-chained training job events (hashes and states only)"),
        _p(DataKind.REMOTE_INTENT_LEDGER, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "hash-chained remote intent ledger (operational evidence, kept for replay protection)"),
        _p(DataKind.PAYLOAD_TOMBSTONE, _C.AUTHORITY_METADATA, _R3, _U.NEVER,
           "hash-chained tombstone left when a rejected payload is purged (metadata retained)"),
        _p(DataKind.REJECTED_ARTIFACT_PAYLOAD, _C.ARTIFACTS, _R3, _U.PURGE_PAYLOAD_KEEP_METADATA,
           "payload of a rejected artifact: bytes purgeable after the R1 window from the rejection decision; "
           "hash/status/receipt/decision retained", payload_retention=_R1),
        _p(DataKind.PROJECT_SOURCE, _C.PROJECT_SOURCES, _R2, _U.HUMAN_DECISION_ONLY, "accepted project sources"),
        _p(DataKind.ACTIVE_ARTIFACT, _C.ARTIFACTS, _R2, _U.HUMAN_DECISION_ONLY, "artifacts of accepted state"),
        _p(DataKind.PROJECT_EVIDENCE, _C.ARTIFACTS, _R2, _U.HUMAN_DECISION_ONLY, "project evidence"),
        _p(DataKind.DERIVED_DOCUMENT, _C.ARTIFACTS, _R2, _U.HUMAN_DECISION_ONLY,
           "final capability-pipeline outputs derived from project sources (e.g. normalized OCR documents, "
           "algorithm result envelopes); project content, non-authoritative, revoked with their source lineage"),
        _p(DataKind.GOVERNED_PROJECT_CONTENT, _C.PROJECT_SOURCES, _R2, _U.HUMAN_DECISION_ONLY,
           "current governed project content"),
        _p(DataKind.RELEASE_TEST_EVIDENCE, _C.RELEASE_TEST_EVIDENCE, _R2, _U.HUMAN_DECISION_ONLY,
           "release and acceptance evidence"),
        _p(DataKind.STORAGE_PLACEMENT_INDEX, _C.AUTHORITY_METADATA, _R2, _U.HUMAN_DECISION_ONLY,
           "storage placement index (digests, sizes, classes, tiers, backend ids, safe labels; no content, "
           "no authority)"),
        _p(DataKind.OUTCOME_RECORD, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD,
           "outcome learning records (metadata, features, hashes; no content)"),
        _p(DataKind.ADVICE_LOG, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD, "advisor advice-set log"),
        _p(DataKind.OPERATIONAL_LOG, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD, "operational logs"),
        _p(DataKind.TELEMETRY, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD, "operational telemetry"),
        _p(DataKind.FAILED_INTERMEDIATE_ARTIFACT, _C.ARTIFACTS, _R1, _U.DELETE_RECORD,
           "failed intermediate artifacts never referenced by accepted state"),
        _p(DataKind.NOTIFICATION_DELIVERY, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD,
           "notification retry / delivery metadata"),
        _p(DataKind.CONNECTIVITY_HISTORY, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD,
           "connectivity state transitions (no network identifiers)"),
        _p(DataKind.HERDR_TELEMETRY, _C.HERDR_TELEMETRY, _R1, _U.DELETE_RECORD, "Herdr operational telemetry"),
        _p(DataKind.QUARANTINED_UPLOAD, _C.REMOTE_UPLOADS, _R1, _U.DELETE_RECORD, "quarantined remote uploads"),
        _p(DataKind.MACHINE_CONNECTION_HISTORY, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD,
           "machine connection state observations"),
        _p(DataKind.CAPABILITY_STAGE_EVIDENCE, _C.OPERATIONAL_LOGS, _R1, _U.DELETE_RECORD,
           "capability stage/plan receipts, engine evidence and reproducibility records (hashes and metadata only; "
           "copied to PROJECT_EVIDENCE when an accepted decision references them)"),
        _p(DataKind.TEMPORARY_FILE, _C.CACHE, _R0, _U.DELETE_RECORD, "temporary files"),
        _p(DataKind.SANDBOX_WORKING_DATA, _C.SANDBOX_DATA, _R0, _U.DELETE_RECORD, "sandbox working data"),
        _p(DataKind.RAW_INTERMEDIATE_OUTPUT, _C.SANDBOX_DATA, _R0, _U.DELETE_RECORD, "raw intermediate outputs"),
        _p(DataKind.TEMPORARY_UPLOAD, _C.REMOTE_UPLOADS, _R0, _U.DELETE_RECORD, "temporary uploads"),
        _p(DataKind.DEBUG_TRACE, _C.OPERATIONAL_LOGS, _R0, _U.DELETE_RECORD, "debug traces"),
        _p(DataKind.PROBE_DEBUG, _C.OPERATIONAL_LOGS, _R0, _U.DELETE_RECORD, "connectivity probe debugging"),
        _p(DataKind.CACHE_ENTRY, _C.CACHE, _R0, _U.DELETE_RECORD, "rebuildable cache"),
        _p(DataKind.CAPABILITY_INTERMEDIATE_ARTIFACT, _C.ARTIFACTS, _R0, _U.DELETE_RECORD,
           "intermediate capability stage outputs (e.g. OCR preprocess manifests, partial documents)"),
    )
}


def policy_for(kind: DataKind) -> DataKindPolicy:
    return CATALOG[kind]


class ProhibitedPersistence(StrEnum):
    """Data PADOS never persists by default (DATA_MINIMIZATION)."""

    PLAINTEXT_PASSWORD = "PLAINTEXT_PASSWORD"
    API_CREDENTIAL = "API_CREDENTIAL"
    FULL_RECONSTRUCTED_CONTEXT = "FULL_RECONSTRUCTED_CONTEXT"
    DUPLICATED_PROVIDER_CONTEXT = "DUPLICATED_PROVIDER_CONTEXT"
    FULL_TERMINAL_HISTORY = "FULL_TERMINAL_HISTORY"
    RAW_SANDBOX_SCRATCH = "RAW_SANDBOX_SCRATCH"
    TEMPORARY_REASONING = "TEMPORARY_REASONING"
    RAW_HTTP_BODY = "RAW_HTTP_BODY"
    DUPLICATE_UPLOAD = "DUPLICATE_UPLOAD"


R0_DEFAULT = timedelta(hours=24)
R0_MAX = timedelta(days=7)
R1_DEFAULT = timedelta(days=14)
R1_MIN = timedelta(days=7)
R1_MAX = timedelta(days=30)


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention windows. R2/R3 have no time window: they never expire on age."""

    r0: timedelta = R0_DEFAULT
    r1: timedelta = R1_DEFAULT
    # R1 outside 7-30 days is an explicit operator decision, recorded in the policy.
    explicit_override: bool = False

    def __post_init__(self) -> None:
        if not timedelta(minutes=1) <= self.r0 <= R0_MAX:
            raise ValueError("R0_WINDOW_OUT_OF_RANGE")
        if not timedelta(hours=1) <= self.r1 <= timedelta(days=365):
            raise ValueError("R1_WINDOW_OUT_OF_RANGE")
        if not self.explicit_override and not R1_MIN <= self.r1 <= R1_MAX:
            raise ValueError("R1_WINDOW_REQUIRES_EXPLICIT_OVERRIDE")

    @classmethod
    def configured(cls, *, r0_hours: float | None = None, r1_days: float | None = None,
                   explicit_override: bool = False) -> RetentionPolicy:
        r1 = timedelta(days=r1_days) if r1_days is not None else R1_DEFAULT
        r0 = timedelta(hours=r0_hours) if r0_hours is not None else R0_DEFAULT
        return cls(r0=r0, r1=r1, explicit_override=explicit_override)

    def window(self, retention: RetentionClass) -> timedelta | None:
        if retention is RetentionClass.R0_EPHEMERAL:
            return self.r0
        if retention is RetentionClass.R1_OPERATIONAL:
            return self.r1
        return None
