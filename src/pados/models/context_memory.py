# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass
from enum import StrEnum


class VerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"


class FreshnessStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class SourceTrustLevel(StrEnum):
    CANONICAL = "CANONICAL"
    VERIFIED_GENERATED = "VERIFIED_GENERATED"
    USER_CONFIRMED = "USER_CONFIRMED"
    OBSERVED = "OBSERVED"
    MODEL_INFERRED = "MODEL_INFERRED"
    UNTRUSTED = "UNTRUSTED"


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    DECISION = "decision"
    ARTIFACT = "artifact"
    OBSERVATION = "observation"


class ExclusionReason(StrEnum):
    OUTSIDE_PROJECT = "OUTSIDE_PROJECT"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    LOW_RELEVANCE = "LOW_RELEVANCE"
    DUPLICATE = "DUPLICATE"
    SUPERSEDED = "SUPERSEDED"
    FORBIDDEN_SOURCE = "FORBIDDEN_SOURCE"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    HASH_MISMATCH = "HASH_MISMATCH"
    AUTHORITY_NOT_PROMOTED = "AUTHORITY_NOT_PROMOTED"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


@dataclass(frozen=True)
class MemoryCandidate:
    memory_id: str
    project_id: str
    memory_type: str
    title: str
    content_summary: str
    source_type: str
    source_uri: str
    source_hash: str | None
    source_trust_level: str
    verification_status: str
    freshness_status: str
    importance: int
    retrieval_priority: int
    estimated_tokens: int
    score: float
    retrieval_reason: str = "exact_fts"
    authority_event_id: str | None = None
    authority_state: str = "LEGACY_UNKNOWN"
    authority_version: int | None = None
    context_eligible: bool = False
    origin_type: str = "UNKNOWN"
    origin_actor_type: str | None = None
    origin_actor_id: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    source_ref: str | None = None
    evidence_ref: str | None = None
    evidence_status: str = "UNKNOWN"
    result_ref: str | None = None
    run_id: str | None = None
    provider: str | None = None
    model: str | None = None
    runtime: str | None = None
    contract_id: str | None = None
    promotion_gate: str | None = None
