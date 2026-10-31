# SPDX-License-Identifier: AGPL-3.0-only
"""Build and re-verify a ``ResearchRunManifest`` from an untrusted research run (deterministic, closed codes).

Admission rules, each fail-closed:

* the run exited 0, names only known phases and states, in legal order, and contacted no host outside the profile;
* every file path is a plain relative path under the run root (no traversal, absolute, drive, backslash, empty,
  dot or over-deep segment): ONE unauthorized path refuses the whole run;
* files under an allowlisted top-level directory become digest-addressed artifacts; anything else is quarantined
  (counted, never promoted);
* every completed phase has its output envelope; paper and review artifacts exist only when those phases completed;
  a task snapshot is always present;
* a resume continues exactly the named run, and every phase it reports as already completed stays completed;
* final-evaluation metrics are read from the artifact itself; a claim is EVIDENCED only when that artifact states
  exactly its value -- otherwise it is recorded as NOT_EVIDENCED and the run requires review.

``verify_run_manifest`` re-derives everything from the manifest plus the promoted bytes, so it is checkable by anyone
holding them. Nothing here launches a runtime, reads the filesystem or grants acceptance.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pydantic import ValidationError

from pados.packs.research.contracts import (
    MAX_FILES,
    PHASE_ORDER,
    ArtifactEntry,
    ArtifactKind,
    CampaignRequest,
    ClaimRecord,
    ClaimStatus,
    PhaseRecord,
    PhaseState,
    ResearchError,
    ResearchPhase,
    ResearchRunManifest,
    ResearchRunOutput,
    ResearchRuntimeProfile,
    RunStatus,
)

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # every file, quarantined ones included
MAX_EXACT_INT = 2**53  # larger integers cannot be compared as floats (and overflow on conversion)
MAX_PROMOTED = 30  # the fabric bounds a stage at 32 outputs: one manifest, one pinned-request copy
MAX_DEPTH = 8
# Dot-files (``.env``, ``.hydra/``) are legal names -- quarantined, not refused; ``.``/``..`` are refused separately.
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_METRIC = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})

KIND_BY_ROOT: dict[str, ArtifactKind] = {
    "task": ArtifactKind.TASK_SNAPSHOT, "investigation": ArtifactKind.INVESTIGATION,
    "ablation": ArtifactKind.ABLATION, "final_evaluation": ArtifactKind.FINAL_EVALUATION,
    "posthoc": ArtifactKind.POSTHOC, "paper": ArtifactKind.PAPER, "review": ArtifactKind.REVIEW,
    "telemetry": ArtifactKind.TELEMETRY,
}
PHASE_OUTPUT: dict[ResearchPhase, ArtifactKind] = {
    ResearchPhase.TASK_DRAFT: ArtifactKind.TASK_SNAPSHOT, ResearchPhase.INVESTIGATION: ArtifactKind.INVESTIGATION,
    ResearchPhase.ABLATION: ArtifactKind.ABLATION, ResearchPhase.FINAL_EVALUATION: ArtifactKind.FINAL_EVALUATION,
    ResearchPhase.POSTHOC: ArtifactKind.POSTHOC, ResearchPhase.PAPER: ArtifactKind.PAPER,
    ResearchPhase.REVIEW: ArtifactKind.REVIEW,
}
# Outputs that may exist only once their phase completed (a final evaluation that never completed proves nothing).
_ONLY_WHEN_COMPLETED = {ResearchPhase.FINAL_EVALUATION, ResearchPhase.PAPER, ResearchPhase.REVIEW}
FINAL_EVALUATION_PATH = "final_evaluation/result.json"


def canonical_request(campaign: CampaignRequest) -> bytes:
    """The canonical bytes of a campaign request; the task pins their SHA-256."""
    return json.dumps(campaign.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def request_digest_of(campaign: CampaignRequest) -> str:
    return hashlib.sha256(canonical_request(campaign)).hexdigest()


def _coerce(raw: object) -> ResearchRunOutput:
    if isinstance(raw, ResearchRunOutput):
        return raw
    if not isinstance(raw, Mapping):
        raise ResearchError("RESEARCH_MANIFEST_MALFORMED")
    try:
        return ResearchRunOutput.model_validate(dict(raw))
    except (ValidationError, TypeError, ValueError):
        pass
    raise ResearchError("RESEARCH_MANIFEST_MALFORMED")  # outside the handler: no runtime text chained


def _segment_allowed(segment: str) -> bool:
    # No ``.``/``..``, no trailing dot (Windows strips it), no reserved device name (``CON``, ``nul.pdf``, ``COM1``).
    return (bool(_SEGMENT.fullmatch(segment)) and not segment.endswith(".")
            and segment.split(".", 1)[0].upper() not in _RESERVED)


def path_allowed(path: str) -> bool:
    segments = path.split("/")
    return 1 <= len(segments) <= MAX_DEPTH and all(_segment_allowed(segment) for segment in segments)


def _number(value: object) -> float | None:
    """A finite float the value exactly denotes, or ``None`` (bools, non-numbers, NaN/inf, huge integers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return float(value) if abs(value) <= MAX_EXACT_INT else None
    return value if math.isfinite(value) else None


def _phases(output: ResearchRunOutput, campaign: CampaignRequest) -> tuple[PhaseRecord, ...]:
    known = {phase.value for phase in PHASE_ORDER}
    states = {state.value for state in PhaseState}
    if any(name not in known or state not in states for name, state in output.phases.items()):
        raise ResearchError("RESEARCH_PHASE_INVALID")
    reported = {ResearchPhase(name): PhaseState(state) for name, state in output.phases.items()}
    if not set(reported) <= set(campaign.phases):
        raise ResearchError("RESEARCH_PHASE_INVALID")  # a phase nobody requested
    records = tuple(PhaseRecord(phase=p, state=reported.get(p, PhaseState.PENDING)) for p in campaign.phases)
    unfinished = False
    for record in records:
        if record.state is PhaseState.COMPLETED and unfinished:
            raise ResearchError("RESEARCH_PHASE_ORDER_INVALID")
        unfinished = unfinished or record.state in (PhaseState.FAILED, PhaseState.PENDING)
    return records


def _artifacts(files: Mapping[str, bytes]) -> tuple[tuple[ArtifactEntry, ...], dict[str, bytes], int]:
    if len(files) > MAX_FILES:
        raise ResearchError("RESEARCH_ARTIFACT_LIMIT")
    if any(not isinstance(path, str) or not path_allowed(path) for path in files):
        raise ResearchError("RESEARCH_ARTIFACT_PATH_UNAUTHORIZED")
    if len({path.lower() for path in files}) != len(files):
        raise ResearchError("RESEARCH_ARTIFACT_PATH_UNAUTHORIZED")  # case-colliding names alias on some filesystems
    if (any(not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES for data in files.values())
            or sum(len(data) for data in files.values()) > MAX_TOTAL_BYTES):
        raise ResearchError("RESEARCH_ARTIFACT_LIMIT")  # bounded before anything is classified or quarantined
    promoted: dict[str, bytes] = {}
    quarantined = 0
    for path in sorted(files):
        if path.split("/", 1)[0] not in KIND_BY_ROOT or "/" not in path:
            quarantined += 1
            continue
        promoted[path] = files[path]
    if len(promoted) > MAX_PROMOTED:
        raise ResearchError("RESEARCH_ARTIFACT_LIMIT")
    entries = tuple(ArtifactEntry(kind=KIND_BY_ROOT[path.split("/", 1)[0]], relative_path=path, size_bytes=len(data),
                                  sha256=hashlib.sha256(data).hexdigest()) for path, data in promoted.items())
    return entries, promoted, quarantined


def _check_envelopes(records: tuple[PhaseRecord, ...], entries: tuple[ArtifactEntry, ...]) -> None:
    kinds = {entry.kind for entry in entries}
    if ArtifactKind.TASK_SNAPSHOT not in kinds:
        raise ResearchError("RESEARCH_TASK_SNAPSHOT_MISSING")
    state = {record.phase: record.state for record in records}
    for phase, kind in PHASE_OUTPUT.items():
        if state.get(phase) is PhaseState.COMPLETED and kind not in kinds:
            raise ResearchError("RESEARCH_PHASE_OUTPUT_MISSING")
        if phase is not ResearchPhase.TASK_DRAFT and phase not in state and kind in kinds:
            raise ResearchError("RESEARCH_PHASE_OUTPUT_UNEXPECTED")  # output of a phase nobody requested
        if phase in _ONLY_WHEN_COMPLETED and kind in kinds and state.get(phase) is not PhaseState.COMPLETED:
            raise ResearchError("RESEARCH_PHASE_OUTPUT_UNEXPECTED")
    if state.get(ResearchPhase.FINAL_EVALUATION) is PhaseState.COMPLETED and not any(
            entry.relative_path == FINAL_EVALUATION_PATH for entry in entries):
        raise ResearchError("RESEARCH_PHASE_OUTPUT_MISSING")


def _exact(value: object) -> float:
    number = _number(value)
    if number is None:  # unreachable after validation; refuse rather than guess
        raise ResearchError("RESEARCH_FINAL_EVALUATION_MALFORMED")
    return number


def read_final_evaluation(data: bytes | None) -> dict[str, float] | None:
    """Metrics as the final-evaluation artifact states them (``{"metrics": {name: number}}``), else refused."""
    if data is None:
        return None
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        document = None
    metrics = document.get("metrics") if isinstance(document, dict) else None
    if (not isinstance(metrics, dict) or len(metrics) > 64
            or any(not isinstance(k, str) or not _METRIC.fullmatch(k) for k in metrics)
            or any(_number(v) is None for v in metrics.values())):
        raise ResearchError("RESEARCH_FINAL_EVALUATION_MALFORMED")
    return {key: _exact(value) for key, value in sorted(metrics.items())}


def _claims(raw: tuple[Mapping[str, object], ...], metrics: dict[str, float] | None) -> tuple[ClaimRecord, ...]:
    records: list[ClaimRecord] = []
    for claim in raw:
        metric, value = claim.get("metric"), _number(claim.get("value"))
        if set(claim) != {"metric", "value"} or not isinstance(metric, str) or not _METRIC.fullmatch(metric) or (
                value is None):
            raise ResearchError("RESEARCH_MANIFEST_MALFORMED")
        evidenced = metrics is not None and metrics.get(metric) == value
        records.append(ClaimRecord(metric=metric, value=value,
                                   status=ClaimStatus.EVIDENCED if evidenced else ClaimStatus.NOT_EVIDENCED))
    return tuple(records)


def _evaluated(records: tuple[PhaseRecord, ...]) -> bool:
    return any(r.phase is ResearchPhase.FINAL_EVALUATION and r.state is PhaseState.COMPLETED for r in records)


def _status(records: tuple[PhaseRecord, ...]) -> RunStatus:
    states = [record.state for record in records]
    if PhaseState.FAILED in states:
        return RunStatus.FAILED
    return RunStatus.COMPLETED if all(s is PhaseState.COMPLETED for s in states) else RunStatus.PARTIAL


def _check_resume(campaign: CampaignRequest, upstream_run_id: str, records: tuple[PhaseRecord, ...]) -> None:
    """A resume continues exactly the named run, and every phase it already completed stays completed."""
    resume = campaign.resume
    if resume is None:
        return
    completed = {record.phase for record in records if record.state is PhaseState.COMPLETED}
    if resume.upstream_run_id != upstream_run_id or not set(resume.completed_phases) <= completed:
        raise ResearchError("RESEARCH_RESUME_MISMATCH")


def build_run_manifest(raw: object, *, campaign: CampaignRequest,
                       profile: ResearchRuntimeProfile) -> tuple[ResearchRunManifest, dict[str, bytes]]:
    """Admit an untrusted run: the manifest PADOS derives and the bytes it promotes (keyed by relative path)."""
    output = _coerce(raw)
    if output.exit_code != 0:
        raise ResearchError("RESEARCH_RUNTIME_FAILED")
    if not set(output.egress_hosts_contacted) <= set(profile.egress_hosts):
        raise ResearchError("RESEARCH_EGRESS_VIOLATION")
    records = _phases(output, campaign)
    entries, promoted, quarantined = _artifacts(output.files)
    _check_envelopes(records, entries)
    _check_resume(campaign, output.upstream_run_id, records)
    metrics = read_final_evaluation(promoted.get(FINAL_EVALUATION_PATH)) if _evaluated(records) else None
    claims = _claims(output.claims, metrics)
    snapshot = hashlib.sha256(json.dumps(sorted((e.relative_path, e.sha256) for e in entries
                                                if e.kind is ArtifactKind.TASK_SNAPSHOT)).encode("ascii")).hexdigest()
    manifest = ResearchRunManifest(
        profile_id=profile.profile_id, upstream_repository=profile.upstream_repository,
        upstream_revision=profile.upstream_revision, image_digest=profile.image_digest,
        upstream_run_id=output.upstream_run_id, request_digest=request_digest_of(campaign),
        task_snapshot_digest=snapshot,
        phases=records, artifacts=entries, quarantined_count=quarantined, final_evaluation=metrics, claims=claims,
        status=_status(records), review_required=any(c.status is ClaimStatus.NOT_EVIDENCED for c in claims))
    return manifest, promoted


def verify_run_manifest(manifest: ResearchRunManifest, promoted: Mapping[str, bytes], *,
                        campaign: CampaignRequest) -> bool:
    """Deterministic re-check against the PINNED campaign: the manifest names exactly that request and exactly its
    requested phases (none dropped, none added), every entry matches its bytes, and metrics, claim statuses and the
    run status re-derive. Without the campaign, a forger could drop pending phases and claim completion."""
    if manifest.request_digest != request_digest_of(campaign):
        return False
    if tuple(record.phase for record in manifest.phases) != campaign.phases:
        return False
    if set(promoted) != {entry.relative_path for entry in manifest.artifacts}:
        return False
    for entry in manifest.artifacts:
        data = promoted[entry.relative_path]
        if (not path_allowed(entry.relative_path) or KIND_BY_ROOT.get(entry.relative_path.split("/", 1)[0]) is not
                entry.kind or len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256):
            return False
    try:
        _check_resume(campaign, manifest.upstream_run_id, manifest.phases)
        _check_envelopes(manifest.phases, manifest.artifacts)
        evaluated = _evaluated(manifest.phases)
        metrics = read_final_evaluation(promoted.get(FINAL_EVALUATION_PATH)) if evaluated else None
        claims = _claims(tuple({"metric": c.metric, "value": c.value} for c in manifest.claims), metrics)
    except ResearchError:
        return False
    return (metrics == manifest.final_evaluation and claims == manifest.claims
            and manifest.review_required == any(c.status is ClaimStatus.NOT_EVIDENCED for c in claims)
            and manifest.status is _status(manifest.phases))
