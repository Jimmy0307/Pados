# SPDX-License-Identifier: AGPL-3.0-only
"""Build and verify a runtime lane evidence index (OP-002) without hand-typed statuses, commits or hashes.

The builder never takes a PASS on the spec author's word:

* A PASS comes only from a machine-written runner record named by ``source``: a Windows gate summary
  (``run_gates.ps1``), a VMware chain summary (``vm_online_chain.sh``) or an E2E result (``local_dogfood_e2e.py``).
  The builder reads the commit, the exit codes of the named gates, the platform where the record has one, and the
  finish time from that file. A record for another commit, a missing gate or a platform mismatch refuses the build.
* A spec may still state a non-zero exit code without a source (a FAIL is never an upgrade), but a spec-typed
  ``exit_code: 0`` is refused.
* A spec may *downgrade* a lane (SKIPPED or NOT_VALIDATED, with a reason).
* recorded_at is the record's finish time (or, for a record without one, its modification time), so staleness counts
  from when the evidence was produced. The hash is computed here.

Missing, outside-root or reused evidence refuses the whole build (fail closed).

What this proves is binding: a runner record for this commit, with these exit codes, in these bytes. A record can
still be forged or come from a runner that checks the wrong thing; content truth stays with review and the existing
acceptance paths.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pados.release.runtime_lanes import (
    EvidenceIndex,
    EvidenceRecord,
    LaneStatus,
    RuntimeLaneRegistry,
    _resolved_evidence,
    _sha256,
)

SPEC_SCHEMA = "pados.runtime-lane-spec.v1"


class LaneSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_id: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=40)
    command: str = Field(min_length=1, max_length=1000)
    exit_code: int | None = None
    evidence_path: str | None = Field(default=None, max_length=500)
    declare: Literal["SKIPPED", "NOT_VALIDATED"] | None = None
    source: Literal["gate-summary", "vm-summary", "e2e-result"] | None = None
    gates: tuple[str, ...] = Field(default=(), max_length=64)
    notes: str = Field(default="", max_length=2000)


class IndexSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pados.runtime-lane-spec.v1"]
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    lanes: tuple[LaneSpec, ...] = Field(min_length=1)


class IndexBuildError(ValueError):
    """Raised with a stable code; the whole build is refused."""


def load_spec(data: Mapping[str, Any]) -> IndexSpec:
    try:
        return IndexSpec.model_validate(data)
    except ValidationError as exc:
        raise IndexBuildError(f"RUNTIME_LANE_SPEC_INVALID: {exc}") from exc


def _evidence_file(root: Path, lane: LaneSpec) -> Path:
    if lane.evidence_path is None:
        raise IndexBuildError(f"RUNTIME_LANE_EVIDENCE_REQUIRED {lane.lane_id}")
    resolved = _resolved_evidence(root, lane.evidence_path)
    if resolved is None:
        raise IndexBuildError(f"RUNTIME_LANE_EVIDENCE_OUTSIDE_ROOT {lane.lane_id}")
    if not resolved.is_file():
        raise IndexBuildError(f"RUNTIME_LANE_EVIDENCE_MISSING {lane.lane_id}")
    return resolved


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _gate_exit(gates: Mapping[str, Any], name: str, lane_id: str) -> int:
    entry = gates.get(name)
    code = entry.get("exit_code") if isinstance(entry, Mapping) else entry
    if isinstance(code, bool) or not isinstance(code, int):
        raise IndexBuildError(f"RUNTIME_LANE_SOURCE_GATE_MISSING {lane_id}:{name}")
    return code


def _from_source(lane: LaneSpec, path: Path, *, commit: str) -> tuple[int, datetime]:
    """The exit code and finish time read from a machine-written runner record, cross-checked against the spec."""
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise IndexBuildError(f"RUNTIME_LANE_SOURCE_UNREADABLE {lane.lane_id}") from exc
    if not isinstance(record, Mapping):
        raise IndexBuildError(f"RUNTIME_LANE_SOURCE_UNREADABLE {lane.lane_id}")
    if lane.source == "e2e-result":
        recorded_commit, platform = record.get("commit"), str(record.get("platform", "")).lower()
        if not platform.startswith(lane.platform.lower()):
            raise IndexBuildError(f"RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH {lane.lane_id}")
        exit_code = 0 if record.get("verdict") == "PASS" else 1
    else:
        recorded_commit = record.get("repo_head" if lane.source == "gate-summary" else "sha")
        if lane.source == "vm-summary" and (lane.platform != "linux" or record.get("virt") != "vmware"):
            raise IndexBuildError(f"RUNTIME_LANE_SOURCE_PLATFORM_MISMATCH {lane.lane_id}")
        gates = record.get("gates")
        if not isinstance(gates, Mapping) or not lane.gates:
            raise IndexBuildError(f"RUNTIME_LANE_SOURCE_GATES_REQUIRED {lane.lane_id}")
        # The VMware summary keeps the E2E exit code beside its gates.
        available = {**gates, "e2e": record["e2e"]} if "e2e" in record else dict(gates)
        exits = [_gate_exit(available, name, lane.lane_id) for name in lane.gates]
        exit_code = next((code for code in exits if code != 0), 0)
    if recorded_commit != commit:
        raise IndexBuildError(f"RUNTIME_LANE_SOURCE_COMMIT_MISMATCH {lane.lane_id}")
    finished = _aware(record.get("finished")) or datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return exit_code, finished


def _record(lane: LaneSpec, *, commit: str, root: Path, now: datetime) -> EvidenceRecord:
    common: dict[str, Any] = {"lane_id": lane.lane_id, "commit": commit, "platform": lane.platform,
                              "command": lane.command, "notes": lane.notes}
    if lane.declare is not None:
        if len(lane.notes.strip()) < 10:
            raise IndexBuildError(f"RUNTIME_LANE_DECLARATION_NEEDS_A_REASON {lane.lane_id}")
        # A declared lane keeps its exit code and evidence for the record, but its status is never derived from them.
        evidence: dict[str, Any] = {}
        if lane.evidence_path is not None:
            path = _evidence_file(root, lane)
            evidence = {"evidence_path": lane.evidence_path, "evidence_sha256": _sha256(path)}
        return EvidenceRecord(status=LaneStatus(lane.declare), exit_code=lane.exit_code, recorded_at=now,
                              **common, **evidence)
    if lane.source is not None:
        if lane.exit_code is not None:
            raise IndexBuildError(f"RUNTIME_LANE_EXIT_CODE_FROM_SPEC_AND_SOURCE {lane.lane_id}")
        path = _evidence_file(root, lane)
        exit_code, produced = _from_source(lane, path, commit=commit)
        return EvidenceRecord(status=LaneStatus.PASS if exit_code == 0 else LaneStatus.FAIL, exit_code=exit_code,
                              evidence_path=lane.evidence_path, evidence_sha256=_sha256(path), recorded_at=produced,
                              **common)
    if lane.exit_code is None:
        # Nothing ran (or the exit code was not captured): never a PASS.
        return EvidenceRecord(status=LaneStatus.NOT_VALIDATED, recorded_at=now, **common)
    if lane.exit_code == 0:
        # A PASS needs a machine-written runner record; a typed zero is the spec author's word.
        raise IndexBuildError(f"RUNTIME_LANE_PASS_NEEDS_MACHINE_SOURCE {lane.lane_id}")
    path = _evidence_file(root, lane)
    produced = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return EvidenceRecord(status=LaneStatus.FAIL, exit_code=lane.exit_code, evidence_path=lane.evidence_path,
                          evidence_sha256=_sha256(path), recorded_at=produced, **common)


def build_index(spec: IndexSpec, *, root: Path, registry: RuntimeLaneRegistry | None = None,
                now: datetime | None = None) -> EvidenceIndex:
    moment = now or datetime.now(UTC)
    unknown = sorted({lane.lane_id for lane in spec.lanes} - set(registry.lanes)) if registry else []
    if unknown:
        raise IndexBuildError(f"RUNTIME_LANE_UNKNOWN {unknown}")
    records = tuple(_record(lane, commit=spec.commit, root=root, now=moment) for lane in spec.lanes)
    shared: dict[str, list[str]] = {}
    for record in records:
        if record.evidence_path is not None:
            shared.setdefault(str(_resolved_evidence(root, record.evidence_path)), []).append(record.lane_id)
    reused = sorted(lane for lanes in shared.values() if len(lanes) > 1 for lane in lanes)
    if reused:
        raise IndexBuildError(f"RUNTIME_LANE_EVIDENCE_REUSED {reused}")
    try:
        return EvidenceIndex(schema_version="pados.runtime-lane-evidence.v1", records=records)
    except ValidationError as exc:
        raise IndexBuildError(f"RUNTIME_LANE_INDEX_INVALID: {exc}") from exc


def verify_index(index: EvidenceIndex, *, root: Path) -> tuple[str, ...]:
    """Re-hash every referenced evidence file; each finding names the lane and the problem."""
    findings: list[str] = []
    for record in index.records:
        if record.evidence_path is None or record.evidence_sha256 is None:
            continue
        resolved = _resolved_evidence(root, record.evidence_path)
        if resolved is None:
            findings.append(f"{record.lane_id}:EVIDENCE_PATH_OUTSIDE_ROOT")
        elif not resolved.is_file():
            findings.append(f"{record.lane_id}:EVIDENCE_FILE_MISSING")
        elif _sha256(resolved) != record.evidence_sha256:
            findings.append(f"{record.lane_id}:EVIDENCE_HASH_MISMATCH")
    return tuple(findings)


def summary(index: EvidenceIndex) -> Sequence[str]:
    return [f"{r.lane_id:34} {r.status.value:13} exit={r.exit_code} commit={r.commit[:12]}" for r in index.records]
