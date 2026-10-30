# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [16][48]: a session binding becomes resolvable only after its creation evidence is durable.

Protocol: provider session created -> authoritative binding STAGED (never resolvable) -> creation receipt
durably committed -> binding COMMITTED against that receipt's hash (resolvable). Any failure or crash between
the steps leaves no resolvable binding, so no follow-up can be authorized against a session whose creation is
missing from the receipt chain; the operational store being full/corrupt/unwritable can never split authority
from evidence. Abort can retire a pending binding but can never revoke a committed one.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from loop_learning_support import (
    ENDPOINT,
    HEALTH_OK,
    LEARNING_ADAPTER,
    LEARNING_CAPABILITY,
    FakeHttp,
    contract,
    generate_ok,
    grant,
    learning_request,
)

from pados.capability_loop.adapters.openmaic_http import OpenMaicLearningProvider
from pados.capability_loop.binding_authority import BindingAuthorityError, GovernedSessionBinding
from pados.capability_loop.learning import LearningService
from pados.capability_loop.receipts import OperationResult
from pados.capability_loop.store import LoopStore, LoopStoreError
from pados.capability_loop_runtime.binding import (
    GENESIS,
    AnchoredSessionBindingAuthority,
    InMemorySessionBindingAuthority,
    _seal,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification
from pados.learning.anchor import Checkpoint, FileAnchor

PROVIDER = "openmaic.adaptive_learning"
SESSION = "job-commit"
STATE_ROUTE = {("GET", f"/api/generate-classroom/{SESSION}"): (200, {
    "jobId": SESSION, "status": "running", "step": "researching", "progress": 40})}


def _anchored(tmp_path: Path) -> AnchoredSessionBindingAuthority:
    return anchored_session_binding_authority(tmp_path / "binding-log" / "binding.jsonl",
                                              tmp_path / "binding-anchor")


AUTHORITIES: dict[str, Callable[[Path], Any]] = {
    "anchored": _anchored,
    "in_memory": lambda _tmp: InMemorySessionBindingAuthority(),
}


def _binding(project: str = "proj-a", session: str = SESSION) -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=session, project_id=project, work_contract_id="wc-1",
        classification=DataClassification.INTERNAL, capability_gap_ref="gap:cad_edit",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _http() -> FakeHttp:
    return FakeHttp({**HEALTH_OK, **generate_ok(step="researching", progress=20, job_id=SESSION), **STATE_ROUTE})


def _service(store: LoopStore, authority: Any, http: FakeHttp | None = None) -> LearningService:
    return LearningService(OpenMaicLearningProvider(ENDPOINT, http or _http()), store,
                           capability=LEARNING_CAPABILITY, adapter=LEARNING_ADAPTER, authority=authority)


def _create(service: LearningService) -> Any:
    return service.create_session(learning_request(), contract=contract(), grant=grant(),
                                  classification=DataClassification.INTERNAL)


def _assert_followup_refused(tmp_path: Path, authority: Any) -> None:
    http = _http()
    receipt = _service(LoopStore(tmp_path / "recovered.json"), authority, http).session_state(
        session_ref=SESSION, contract=contract(), grant=grant())
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == "SESSION_NOT_BOUND"
    assert all("/api/generate-classroom/" not in path for _method, path in http.calls)  # provider never asked


# -- authority-level protocol semantics (both implementations) ---------------------------------------------
@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_staged_binding_is_not_resolvable_until_committed(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    staged = authority.stage(_binding())
    assert authority.resolve(PROVIDER, SESSION) is None
    assert authority.stage(_binding()) == staged  # an identical re-stage of a pending binding is idempotent
    committed = authority.commit(_binding(), evidence_ref="c" * 64)
    resolved = authority.resolve(PROVIDER, SESSION)
    assert resolved is not None and resolved.context_hash == _binding().context_hash
    assert authority.commit(_binding(), evidence_ref="c" * 64) == committed  # idempotent finalize


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_commit_requires_a_pending_stage(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    with pytest.raises(BindingAuthorityError, match="BINDING_NOT_STAGED"):
        authority.commit(_binding(), evidence_ref="c" * 64)
    assert authority.resolve(PROVIDER, SESSION) is None


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_aborted_binding_never_becomes_resolvable(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    authority.stage(_binding())
    authority.abort(_binding())
    authority.abort(_binding())  # idempotent
    with pytest.raises(BindingAuthorityError, match="BINDING_ABORTED"):
        authority.commit(_binding(), evidence_ref="c" * 64)
    with pytest.raises(BindingAuthorityError, match="BINDING_ABORTED"):
        authority.stage(_binding())  # an aborted identity is retired, never revived
    assert authority.resolve(PROVIDER, SESSION) is None


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_abort_can_never_revoke_a_committed_binding(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    authority.stage(_binding())
    authority.commit(_binding(), evidence_ref="c" * 64)
    with pytest.raises(BindingAuthorityError, match="BINDING_ALREADY_COMMITTED"):
        authority.abort(_binding())
    with pytest.raises(BindingAuthorityError, match="BINDING_ALREADY_COMMITTED"):
        authority.stage(_binding())  # a second creation of the same session is refused, not re-staged
    with pytest.raises(BindingAuthorityError, match="BINDING_ALREADY_COMMITTED"):
        authority.commit(_binding(), evidence_ref="d" * 64)  # cannot re-point committed evidence
    assert authority.resolve(PROVIDER, SESSION) is not None


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
@pytest.mark.parametrize("step", ["stage", "commit", "abort"])
def test_differing_context_conflicts_at_every_step(tmp_path: Path, kind: str, step: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    authority.stage(_binding())
    with pytest.raises(BindingAuthorityError, match="BINDING_CONFLICT"):
        if step == "stage":
            authority.stage(_binding(project="proj-b"))
        elif step == "commit":
            authority.commit(_binding(project="proj-b"), evidence_ref="c" * 64)
        else:
            authority.abort(_binding(project="proj-b"))
    assert authority.resolve(PROVIDER, SESSION) is None


# -- LearningService: evidence before authority --------------------------------------------------------------
@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_full_operational_store_leaves_no_resolvable_binding(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    with pytest.raises(LoopStoreError, match="LOOP_STORE_FULL"):
        _create(_service(LoopStore(tmp_path / "loop.json", max_receipts=0), authority))
    assert authority.resolve(PROVIDER, SESSION) is None
    _assert_followup_refused(tmp_path, authority)


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_corrupt_operational_store_leaves_no_resolvable_binding(tmp_path: Path, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)
    path = tmp_path / "loop.json"
    path.write_text(json.dumps({"schema_version": "tampered"}), encoding="utf-8")
    with pytest.raises(LoopStoreError, match="LOOP_STORE_CORRUPT"):
        _create(_service(LoopStore(path), authority))
    assert authority.resolve(PROVIDER, SESSION) is None
    _assert_followup_refused(tmp_path, authority)


@pytest.mark.parametrize("kind", sorted(AUTHORITIES))
def test_operational_store_write_failure_leaves_no_resolvable_binding(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    authority = AUTHORITIES[kind](tmp_path)

    def disk_full(_path: Path, _data: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("pados.capability_loop.store.atomic_write_json", disk_full)
    with pytest.raises(OSError, match="No space left"):
        _create(_service(LoopStore(tmp_path / "loop.json"), authority))
    monkeypatch.undo()
    assert authority.resolve(PROVIDER, SESSION) is None
    _assert_followup_refused(tmp_path, authority)


def test_failed_abort_after_evidence_failure_is_reported_and_still_fails_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _anchored(tmp_path)

    def abort_fails(_binding: GovernedSessionBinding) -> None:
        raise BindingAuthorityError("BINDING_LOG_WRITE_FAILED")

    monkeypatch.setattr(authority, "abort", abort_fails)
    with pytest.raises(LoopStoreError, match="LOOP_STORE_FULL") as excinfo:
        _create(_service(LoopStore(tmp_path / "loop.json", max_receipts=0), authority))
    assert any("BINDING_LOG_WRITE_FAILED" in note for note in getattr(excinfo.value, "__notes__", ()))
    monkeypatch.undo()
    assert authority.resolve(PROVIDER, SESSION) is None  # still only STAGED: never resolvable
    _assert_followup_refused(tmp_path, authority)


def test_finalize_failure_records_a_compensating_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _anchored(tmp_path)
    store = LoopStore(tmp_path / "loop.json")

    def commit_fails(_binding: GovernedSessionBinding, *, evidence_ref: str | None) -> None:
        raise BindingAuthorityError("BINDING_ANCHOR_UNAVAILABLE")

    monkeypatch.setattr(authority, "commit", commit_fails)
    receipt = _create(_service(store, authority))
    monkeypatch.undo()
    assert receipt.result is OperationResult.REFUSED_BY_POLICY
    assert receipt.policy_code == "SESSION_BIND_FINALIZE_FAILED"
    chain = store.receipts()
    assert [r.result for r in chain] == [OperationResult.RECORDED, OperationResult.REFUSED_BY_POLICY]
    assert chain[1].session_ref == SESSION and chain[1].binding_digest == chain[0].binding_digest
    assert authority.resolve(PROVIDER, SESSION) is None
    with pytest.raises(BindingAuthorityError, match="BINDING_ABORTED"):
        authority.commit(_binding(), evidence_ref=chain[0].receipt_hash)  # the pending stage was retired
    _assert_followup_refused(tmp_path, authority)


def test_crash_between_receipt_and_finalize_leaves_session_unauthorized_after_restart(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _anchored(tmp_path)

    def process_dies(_binding: GovernedSessionBinding, *, evidence_ref: str | None) -> None:
        raise SystemExit("killed between the receipt write and the authority commit")

    monkeypatch.setattr(authority, "commit", process_dies)
    with pytest.raises(SystemExit):
        _create(_service(LoopStore(tmp_path / "loop.json"), authority))
    monkeypatch.undo()
    restarted = _anchored(tmp_path)  # a new process over the same durable files
    assert restarted.resolve(PROVIDER, SESSION) is None
    _assert_followup_refused(tmp_path, restarted)


def test_successful_creation_commits_against_its_receipt_hash(tmp_path: Path) -> None:
    authority = _anchored(tmp_path)
    receipt = _create(_service(LoopStore(tmp_path / "loop.json"), authority))
    assert receipt.result is OperationResult.RECORDED
    assert authority.resolve(PROVIDER, SESSION) is not None
    lines = [json.loads(line) for line in
             (tmp_path / "binding-log" / "binding.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["state"] for line in lines] == ["STAGED", "COMMITTED"]
    assert lines[-1]["evidence_ref"] == receipt.receipt_hash  # authority -> evidence linkage
    # and the follow-up is authorized through the committed binding
    state = _service(LoopStore(tmp_path / "loop.json"), authority).session_state(
        session_ref=SESSION, contract=contract(), grant=grant())
    assert state.result is OperationResult.RECORDED


def test_learning_service_never_uses_the_one_shot_bind() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "pados" / "capability_loop" / "learning.py"
    assert ".bind(" not in source.read_text(encoding="utf-8")


# -- the anchored log validates the transition grammar even under a matching anchor -------------------------
def _write_anchored_log(tmp_path: Path, states: list[tuple[str, str | None]]) -> AnchoredSessionBindingAuthority:
    log = tmp_path / "binding-log" / "binding.jsonl"
    log.parent.mkdir(parents=True)
    anchor = FileAnchor(tmp_path / "binding-anchor")
    previous_hash, previous_point, lines = GENESIS, None, []
    binding = _binding()
    for seq, (state, evidence) in enumerate(states, start=1):
        # Raw, correctly sealed rows (not via the model, which already refuses some of these shapes) so the
        # LOADER's own grammar check is what is exercised.
        row = {"seq": seq, "prev_hash": previous_hash, "state": state, "evidence_ref": evidence,
               **binding.model_dump(mode="json")}
        row["entry_hash"] = _seal(row)
        lines.append(json.dumps(row, sort_keys=True))
        point = Checkpoint(seq, row["entry_hash"])
        anchor.record(previous_point, point)
        previous_hash, previous_point = row["entry_hash"], point
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(log, 0o600)  # owner-only like a real log, so on POSIX the GRAMMAR (not file mode) is what is tested
    return AnchoredSessionBindingAuthority(log, anchor)


@pytest.mark.parametrize(("states", "code"), [
    ([("COMMITTED", "c" * 64)], "BINDING_LOG_INCONSISTENT"),  # finalized without a stage
    ([("ABORTED", None)], "BINDING_LOG_INCONSISTENT"),  # aborted without a stage
    ([("STAGED", None), ("STAGED", None)], "BINDING_LOG_INCONSISTENT"),  # staged twice
    ([("STAGED", None), ("COMMITTED", "c" * 64), ("ABORTED", None)], "BINDING_LOG_INCONSISTENT"),  # revoked
    ([("STAGED", None), ("ABORTED", None), ("COMMITTED", "c" * 64)], "BINDING_LOG_INCONSISTENT"),  # revived
    ([("STAGED", "c" * 64)], "BINDING_LOG_CORRUPT"),  # a stage carrying evidence: refused by the record model
], ids=["commit-first", "abort-first", "double-stage", "revoke", "revive", "staged-evidence"])
def test_illegal_transition_sequences_fail_closed_even_when_anchored(
        tmp_path: Path, states: list[tuple[str, str | None]], code: str) -> None:
    authority = _write_anchored_log(tmp_path, states)
    with pytest.raises(BindingAuthorityError) as excinfo:
        authority.resolve(PROVIDER, SESSION)
    assert excinfo.value.code == code  # the exact cause, never a file-mode refusal passing for it


def test_pending_stage_survives_restart_as_unresolvable(tmp_path: Path) -> None:
    authority = _write_anchored_log(tmp_path, [("STAGED", None)])
    assert authority.resolve(PROVIDER, SESSION) is None  # loads healthy (anchored), but nothing is authorized
    assert FileAnchor(tmp_path / "binding-anchor").latest() == Checkpoint(1, authority._load()[0].entry_hash)
