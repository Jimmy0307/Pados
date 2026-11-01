# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime lane validation status and evidence-bound readiness (OP-002).

A lane is one runtime path that can be validated (a platform E2E, a gate set, a live provider). Each release target
names the lanes it requires. Evidence records say what happened in a lane (PASS, FAIL, SKIPPED or NOT_VALIDATED) and
bind it to a commit, platform, command, exit code and an evidence file with its SHA-256.

``evaluate`` only interprets evidence. A required lane counts only with a PASS record for exactly the target commit,
on the lane's platform, inside its freshness window, with exit code 0, and with an evidence file inside the evidence
root that hashes to the recorded digest and is not reused by another lane. Everything else blocks the target and is
reported with a stable reason. The report carries ``semantic_authority = NONE``: it grants no approval, acceptance or
release decision, and the existing approval, acceptance and promotion checks stay authoritative.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REGISTRY_SCHEMA = "pados.runtime-lanes.v1"
EVIDENCE_SCHEMA = "pados.runtime-lane-evidence.v1"
REPORT_SCHEMA = "pados.runtime-readiness.v1"
_CLOCK_SKEW = timedelta(minutes=5)


class LaneStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    NOT_VALIDATED = "NOT_VALIDATED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LaneDefinition(_Frozen):
    description: str = Field(min_length=1)
    platform: Literal["windows", "linux", "macos", "github", "any"]
    max_age_days: int = Field(ge=1, le=365)
    claims: tuple[str, ...] = Field(min_length=1)
    does_not_cover: tuple[str, ...] = ()
    # The documented state when no evidence is supplied; informational only, never counted as evidence.
    known_status: LaneStatus | None = None
    notes: str = ""


class TargetDefinition(_Frozen):
    description: str = Field(min_length=1)
    required_lanes: tuple[str, ...] = Field(min_length=1)


class RuntimeLaneRegistry(_Frozen):
    schema_version: Literal["pados.runtime-lanes.v1"]
    targets: dict[str, TargetDefinition]
    lanes: dict[str, LaneDefinition]

    @model_validator(mode="after")
    def _targets_name_known_lanes(self) -> RuntimeLaneRegistry:
        for name, target in self.targets.items():
            unknown = sorted(set(target.required_lanes) - set(self.lanes))
            if unknown:
                raise ValueError(f"TARGET_REQUIRES_UNKNOWN_LANE {name}: {unknown}")
        return self


class EvidenceRecord(_Frozen):
    lane_id: str = Field(min_length=1, max_length=80)
    status: LaneStatus
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    platform: str = Field(min_length=1, max_length=40)
    command: str = Field(min_length=1, max_length=1000)
    exit_code: int | None = None
    evidence_path: str | None = Field(default=None, max_length=500)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    notes: str = Field(default="", max_length=2000)

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("RECORDED_AT_NEEDS_A_TIMEZONE")
        return value

    @model_validator(mode="after")
    def _ran_lanes_carry_evidence(self) -> EvidenceRecord:
        if self.status in (LaneStatus.PASS, LaneStatus.FAIL) and (
                self.exit_code is None or self.evidence_path is None or self.evidence_sha256 is None):
            raise ValueError("PASS_OR_FAIL_NEEDS_EXIT_CODE_AND_EVIDENCE")
        return self


class EvidenceIndex(_Frozen):
    schema_version: Literal["pados.runtime-lane-evidence.v1"]
    records: tuple[EvidenceRecord, ...] = ()

    @model_validator(mode="after")
    def _one_record_per_lane(self) -> EvidenceIndex:
        lanes = [record.lane_id for record in self.records]
        duplicates = sorted({lane for lane in lanes if lanes.count(lane) > 1})
        if duplicates:
            raise ValueError(f"DUPLICATE_LANE_RECORD {duplicates}")
        return self


class LaneVerdict(_Frozen):
    lane_id: str
    required: bool
    status: LaneStatus
    reason: str
    claims: tuple[str, ...]
    does_not_cover: tuple[str, ...]
    known_status: LaneStatus | None
    record: dict[str, Any] | None


class ReadinessReport(_Frozen):
    schema_version: Literal["pados.runtime-readiness.v1"] = "pados.runtime-readiness.v1"
    target: str
    commit: str
    evaluated_at: datetime
    ready: bool
    blockers: tuple[str, ...]
    lanes: tuple[LaneVerdict, ...]
    semantic_authority: Literal["NONE"] = "NONE"
    note: str = ("Evidence interpretation only. It grants no approval, acceptance or release decision; a lane's "
                 "claims never extend past its does_not_cover list.")


def load_registry(path: Path) -> RuntimeLaneRegistry:
    try:
        return RuntimeLaneRegistry.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ValueError(f"RUNTIME_LANE_REGISTRY_INVALID: {exc}") from exc


def load_evidence(data: Mapping[str, Any]) -> EvidenceIndex:
    try:
        return EvidenceIndex.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"RUNTIME_LANE_EVIDENCE_INVALID: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_evidence(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _reused(records: tuple[EvidenceRecord, ...], root: Path) -> set[str]:
    owners: dict[tuple[str, str], set[str]] = {}
    for record in records:
        if record.evidence_path is None or record.evidence_sha256 is None:
            continue
        resolved = _resolved_evidence(root, record.evidence_path)
        key = (str(resolved or record.evidence_path), record.evidence_sha256)
        owners.setdefault(key, set()).add(record.lane_id)
    return {lane for lanes in owners.values() if len(lanes) > 1 for lane in lanes}


def _check_pass(record: EvidenceRecord, lane: LaneDefinition, *, commit: str, now: datetime, root: Path,
                reused: set[str]) -> tuple[LaneStatus, str]:
    if lane.platform != "any" and record.platform != lane.platform:
        return LaneStatus.FAIL, "EVIDENCE_PLATFORM_MISMATCH"
    if record.commit != commit:
        return LaneStatus.NOT_VALIDATED, "EVIDENCE_COMMIT_MISMATCH"
    if record.recorded_at > now + _CLOCK_SKEW:
        return LaneStatus.FAIL, "EVIDENCE_FROM_THE_FUTURE"
    if now - record.recorded_at > timedelta(days=lane.max_age_days):
        return LaneStatus.NOT_VALIDATED, "EVIDENCE_STALE"
    if record.exit_code != 0:
        return LaneStatus.FAIL, "PASS_WITH_NONZERO_EXIT"
    resolved = _resolved_evidence(root, str(record.evidence_path))
    if resolved is None:
        return LaneStatus.FAIL, "EVIDENCE_PATH_OUTSIDE_ROOT"
    if not resolved.is_file():
        return LaneStatus.FAIL, "EVIDENCE_FILE_MISSING"
    if _sha256(resolved) != record.evidence_sha256:
        return LaneStatus.FAIL, "EVIDENCE_HASH_MISMATCH"
    if record.lane_id in reused:
        return LaneStatus.FAIL, "EVIDENCE_REUSED_ACROSS_LANES"
    return LaneStatus.PASS, "VERIFIED"


def _verdict_for(record: EvidenceRecord | None, lane: LaneDefinition, *, commit: str, now: datetime, root: Path,
                 reused: set[str]) -> tuple[LaneStatus, str]:
    if record is None:
        return LaneStatus.NOT_VALIDATED, "NO_EVIDENCE_RECORD"
    if record.status is LaneStatus.SKIPPED:
        return LaneStatus.SKIPPED, "LANE_SKIPPED"
    if record.status is LaneStatus.NOT_VALIDATED:
        return LaneStatus.NOT_VALIDATED, "LANE_NOT_VALIDATED"
    if record.status is LaneStatus.FAIL:
        return LaneStatus.FAIL, "LANE_FAILED"
    return _check_pass(record, lane, commit=commit, now=now, root=root, reused=reused)


def evaluate(registry: RuntimeLaneRegistry, evidence: EvidenceIndex, *, target: str, commit: str, now: datetime,
             evidence_root: Path) -> ReadinessReport:
    if target not in registry.targets:
        raise ValueError(f"UNKNOWN_TARGET {target}")
    required = set(registry.targets[target].required_lanes)
    by_lane = {record.lane_id: record for record in evidence.records}
    reused = _reused(evidence.records, evidence_root)
    blockers = [f"{lane}:UNKNOWN_LANE" for lane in sorted(set(by_lane) - set(registry.lanes))]
    verdicts: list[LaneVerdict] = []
    for lane_id in sorted(registry.lanes):
        lane = registry.lanes[lane_id]
        record = by_lane.get(lane_id)
        status, reason = _verdict_for(record, lane, commit=commit, now=now, root=evidence_root, reused=reused)
        verdicts.append(LaneVerdict(lane_id=lane_id, required=lane_id in required, status=status, reason=reason,
                                    claims=lane.claims, does_not_cover=lane.does_not_cover,
                                    known_status=lane.known_status,
                                    record=None if record is None else record.model_dump(mode="json")))
        if lane_id in required and status is not LaneStatus.PASS:
            blockers.append(f"{lane_id}:{reason}")
    return ReadinessReport(target=target, commit=commit, evaluated_at=now, ready=not blockers,
                           blockers=tuple(blockers), lanes=tuple(verdicts))
