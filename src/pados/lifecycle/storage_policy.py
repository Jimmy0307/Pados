# SPDX-License-Identifier: AGPL-3.0-only
"""Storage Flexibility V1+: the operator storage policy document and its tier/class -> retention mapping.

The policy is configuration, never a guess: budgets (``StorageBudget``), R0/R1 retention windows (the existing
``RetentionPolicy``) and which existing ``DataKind`` each ``ArtifactClass`` is retained as. There is no second
retention vocabulary: a class maps to a ``DataKind`` of ``pados.lifecycle.retention.CATALOG``, and that kind's
``RetentionClass`` (R0..R3) and purge rule apply.

* Authoritative state and receipts/evidence are R3 or R2: they never expire by age.
* Rebuildable caches and intermediates are R0, and the EPHEMERAL tier is R0 only.
* Project artifacts and large sources are R2 or R3 (unique work is never age-expired by placement policy).
* Model weights are never PADOS bytes. PADOS keeps their identity only (``ARTIFACT_HASH``, R3).

Loading fails closed: an unknown schema, an unknown or duplicate key, a wrong type (strings or booleans where a
number belongs are refused, never coerced), an inconsistent mapping or an unreadable file raises
``StoragePolicyError``. An unset path is the default policy: no size limits, only the conservative pressure
fractions. This module decides nothing about truth, authorization or deletion.
"""
from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pados.lifecycle.retention import (
    CATALOG,
    DataKind,
    PurgeRule,
    RetentionClass,
    RetentionPolicy,
)
from pados.lifecycle.storage_flexibility import ArtifactClass, StorageBudget, StorageTier

STORAGE_POLICY_SCHEMA: Literal["pados.lifecycle.storage-policy.v1"] = "pados.lifecycle.storage-policy.v1"
MAX_POLICY_BYTES = 256 * 1024

_R0, _R1, _R2, _R3 = (RetentionClass.R0_EPHEMERAL, RetentionClass.R1_OPERATIONAL, RetentionClass.R2_PROJECT,
                      RetentionClass.R3_AUTHORITY)

DEFAULT_CLASS_KINDS: Mapping[ArtifactClass, DataKind] = {
    ArtifactClass.AUTHORITATIVE_STATE: DataKind.ARTIFACT_HASH,
    ArtifactClass.RECEIPT_OR_EVIDENCE: DataKind.EXECUTION_RECEIPT_SUMMARY,
    ArtifactClass.PROJECT_ARTIFACT: DataKind.ACTIVE_ARTIFACT,
    ArtifactClass.LARGE_SOURCE: DataKind.PROJECT_SOURCE,
    ArtifactClass.MODEL_WEIGHTS: DataKind.ARTIFACT_HASH,  # identity only: the runtime keeps the bytes
    ArtifactClass.REBUILDABLE_CACHE: DataKind.CACHE_ENTRY,
    ArtifactClass.INTERMEDIATE: DataKind.RAW_INTERMEDIATE_OUTPUT,
}

# Which retention classes an operator may map each artifact class onto.
ALLOWED_CLASS_RETENTION: Mapping[ArtifactClass, frozenset[RetentionClass]] = {
    ArtifactClass.AUTHORITATIVE_STATE: frozenset({_R3, _R2}),
    ArtifactClass.RECEIPT_OR_EVIDENCE: frozenset({_R3, _R2}),
    ArtifactClass.PROJECT_ARTIFACT: frozenset({_R2, _R3}),
    ArtifactClass.LARGE_SOURCE: frozenset({_R2, _R3}),
    ArtifactClass.MODEL_WEIGHTS: frozenset({_R3}),
    ArtifactClass.REBUILDABLE_CACHE: frozenset({_R0}),
    ArtifactClass.INTERMEDIATE: frozenset({_R0}),
}


class StoragePolicyError(ValueError):
    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(f"{code}:{reason}" if reason else code)
        self.code = code
        self.reason = reason


class RetentionWindows(BaseModel):
    """R0/R1 windows for the existing ``RetentionPolicy`` (R2/R3 have no window by definition)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    r0_hours: float | None = Field(default=None, gt=0, strict=True)
    r1_days: float | None = Field(default=None, gt=0, strict=True)
    explicit_override: bool = Field(default=False, strict=True)

    def policy(self) -> RetentionPolicy:
        return RetentionPolicy.configured(r0_hours=self.r0_hours, r1_days=self.r1_days,
                                          explicit_override=self.explicit_override)


def _kind_problem(artifact_class: ArtifactClass, kind: DataKind) -> str | None:
    policy = CATALOG[kind]
    if policy.retention not in ALLOWED_CLASS_RETENTION[artifact_class]:
        return "STORAGE_CLASS_RETENTION_NOT_ALLOWED"
    if policy.purge_rule is PurgeRule.PURGE_PAYLOAD_KEEP_METADATA:
        return "STORAGE_CLASS_KIND_REQUIRES_REJECTION_PROOF"  # only the rejection path may map there
    return None


class StoragePolicy(BaseModel):
    """The validated operator storage policy. Immutable; every field has a no-limit or conservative default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.lifecycle.storage-policy.v1"] = STORAGE_POLICY_SCHEMA
    budget: StorageBudget = Field(default_factory=StorageBudget)
    retention: RetentionWindows = Field(default_factory=RetentionWindows)
    class_data_kinds: Mapping[ArtifactClass, DataKind] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> StoragePolicy:
        try:
            self.retention.policy()
        except ValueError as exc:
            raise ValueError(f"STORAGE_RETENTION_WINDOWS_INVALID:{exc}") from None
        for artifact_class, kind in self.class_data_kinds.items():
            problem = _kind_problem(artifact_class, kind)
            if problem:
                raise ValueError(f"{problem}:{artifact_class.value}")
        return self

    @property
    def retention_policy(self) -> RetentionPolicy:
        return self.retention.policy()

    def data_kind(self, artifact_class: ArtifactClass) -> DataKind:
        return self.class_data_kinds.get(artifact_class, DEFAULT_CLASS_KINDS[artifact_class])

    def retention_class(self, artifact_class: ArtifactClass) -> RetentionClass:
        return CATALOG[self.data_kind(artifact_class)].retention

    def tier_retention(self, tier: StorageTier, artifact_class: ArtifactClass) -> RetentionClass:
        """The retention class bytes of ``artifact_class`` carry in ``tier``. EPHEMERAL is R0 only."""
        retention = self.retention_class(artifact_class)
        if tier is StorageTier.EPHEMERAL and retention is not _R0:
            raise StoragePolicyError("STORAGE_EPHEMERAL_TIER_IS_R0_ONLY", artifact_class.value)
        return retention

    def expires_by_age(self, artifact_class: ArtifactClass) -> bool:
        return self.retention_policy.window(self.retention_class(artifact_class)) is not None


def _refuse_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise StoragePolicyError("STORAGE_POLICY_DUPLICATE_KEY")
    return dict(pairs)


def _refuse_constant(name: str) -> float:
    raise StoragePolicyError("STORAGE_POLICY_NON_FINITE_NUMBER", name)


def _parse(raw: bytes, suffix: str) -> Any:
    text = raw.decode("utf-8")
    if suffix == ".json":
        return json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant)
    if suffix == ".toml":
        return tomllib.loads(text)  # TOML refuses duplicate keys itself
    raise StoragePolicyError("STORAGE_POLICY_FORMAT_UNSUPPORTED")


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise StoragePolicyError("STORAGE_POLICY_NON_FINITE_NUMBER")
    if isinstance(value, Mapping):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def storage_policy_from_document(document: Any) -> StoragePolicy:
    """Validate a parsed policy document; anything unexpected is refused (never repaired or guessed)."""
    if not isinstance(document, Mapping):
        raise StoragePolicyError("STORAGE_POLICY_NOT_AN_OBJECT")
    if document.get("schema_version") != STORAGE_POLICY_SCHEMA:
        raise StoragePolicyError("STORAGE_POLICY_SCHEMA_UNSUPPORTED")
    _finite(document)
    try:
        return StoragePolicy.model_validate(dict(document))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise StoragePolicyError("STORAGE_POLICY_INVALID", where) from None


def load_storage_policy(path: str | Path | None) -> StoragePolicy:
    """The operator policy at ``path`` (``.json`` or ``.toml``). Unset -> defaults; anything else fails closed."""
    if path is None or str(path) == "":
        return StoragePolicy()
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise StoragePolicyError("STORAGE_POLICY_NOT_A_REGULAR_FILE")
        raw = source.read_bytes()
    except OSError as exc:
        raise StoragePolicyError("STORAGE_POLICY_UNREADABLE") from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise StoragePolicyError("STORAGE_POLICY_TOO_LARGE")
    try:
        document = _parse(raw, source.suffix.lower())
    except StoragePolicyError:
        raise
    except (UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise StoragePolicyError("STORAGE_POLICY_UNPARSEABLE") from exc
    return storage_policy_from_document(document)
