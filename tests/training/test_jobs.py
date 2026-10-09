"""TrainingJob state machine, run approval and the hash-chained event log."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from pados.execution.context import canonical_hash
from pados.capability.artifacts import ScopedArtifacts
from pados.capability.model import Distribution
from pados.enterprise.classification import DataClassification
from pados.training.backends import (
    CONTRACT_ONLY_SPECS,
    ContractOnlyBackend,
    TrainingBackendError,
    TrainingRunResult,
)
from pados.training.contracts import (
    DatasetProvenance,
    DatasetSnapshot,
    LicenseConstraints,
    ModelRef,
    TrainingContractError,
    TrainingKind,
    validate_contract,
)
from pados.training.decisions import DecisionOperation
from pados.training.jobs import (
    JOB_LOG_FIELDS,
    JobState,
    TrainingJobError,
    TrainingRunError,
    execute_job,
    transition,
)
from pados.training.registry import CandidateState
from tests.windows_links import create_link_or_skip

from .support import CREATED, make_contract, make_env, make_snapshot, reanchor


def test_happy_path_reaches_completed_and_records_only_hashes(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    job_id, contract, result = env.trained()
    job = env.jobs.get(job_id)
    assert job.state is JobState.COMPLETED
    assert job.artifact_sha256 == result.artifact.ref.sha256
    assert job.metrics_hash == canonical_hash(dict(result.metrics))
    lines = (tmp_path / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert events == ["CREATED", "APPROVED", "STARTED", "COMPLETED"]
    for line in lines:
        assert set(json.loads(line)) == set(JOB_LOG_FIELDS)
        assert "features" not in line and "label" not in line  # no dataset content in the log


def test_starting_requires_an_explicit_approval(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.jobs.create(contract, job_id="job-00000001")
    with pytest.raises(TrainingRunError, match="JOB_NOT_APPROVED_TO_RUN"):
        execute_job(env.jobs, "job-00000001", contract=contract, dataset=env.train, backend=env.backend,
                    store=env.store, eval_dataset=env.held_out)
    with pytest.raises(TrainingJobError, match="JOB_TRANSITION_INVALID"):
        env.jobs.start("job-00000001", approval_id="0" * 32)
    env.jobs.approve("job-00000001", env.decision(DecisionOperation.APPROVE_TRAINING_RUN,
                                                  env.run_subject("job-00000001")))
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_REFERENCE_MISMATCH"):
        env.jobs.start("job-00000001", approval_id="f" * 32)
    assert env.jobs.get("job-00000001").state is JobState.APPROVED_TO_RUN


def test_run_approval_is_bound_to_the_exact_contract(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    other = env.contract(seed=8)
    env.jobs.create(contract, job_id="job-00000001")
    wrong_subject = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, other.contract_hash)
    with pytest.raises(TrainingJobError, match="DECISION_SUBJECT_MISMATCH"):
        env.jobs.approve("job-00000001", wrong_subject)
    wrong_operation = env.decision(DecisionOperation.PROMOTE_MODEL, env.run_subject("job-00000001"))
    with pytest.raises(TrainingJobError, match="DECISION_OPERATION_MISMATCH"):
        env.jobs.approve("job-00000001", wrong_operation)
    assert env.jobs.get("job-00000001").state is JobState.DRAFT
    env.approved_job(contract, "job-00000002")
    with pytest.raises(TrainingRunError, match="CONTRACT_NOT_BOUND_TO_JOB"):
        execute_job(env.jobs, "job-00000002", contract=other, dataset=env.train, backend=env.backend,
                    store=env.store, eval_dataset=env.held_out)


def test_run_approval_cannot_be_replayed_for_another_job(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.jobs.create(contract, job_id="job-00000001")
    env.jobs.create(contract, job_id="job-00000002")
    record = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, env.run_subject("job-00000001"))
    env.jobs.approve("job-00000001", record)
    with pytest.raises(TrainingJobError, match="DECISION_ALREADY_USED"):
        env.jobs.approve("job-00000002", record)


def test_start_rechecks_the_consumed_decision_ledger(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.approved_job(contract)
    (tmp_path / "decisions.jsonl").unlink()  # ledger evidence for the approval disappears
    approval_id = env.jobs.get("job-00000001").approval_id or ""
    with pytest.raises(TrainingJobError, match="LEDGER_ROLLBACK_DETECTED"):  # below the anchored ledger head
        env.jobs.start("job-00000001", approval_id=approval_id)
    reanchor(tmp_path / "decisions.jsonl")  # even an attacker who also rewrites the anchor gets nowhere
    with pytest.raises(TrainingJobError, match="RUN_APPROVAL_UNVERIFIED"):
        env.jobs.start("job-00000001", approval_id=approval_id)


@pytest.mark.parametrize("target", ["PROMOTED", "PROMOTION_REQUESTED", "REGISTERED", "DEPLOYED", "AUTHORIZED",
                                    "ACTIVE", "CANDIDATE"])
def test_job_can_never_enter_a_runtime_or_registry_state(target: str) -> None:
    for state in JobState:
        with pytest.raises(TrainingJobError, match="JOB_STATE_UNKNOWN"):
            transition(state, target)
    assert not ({s.value for s in JobState} & {s.value for s in CandidateState})


def test_terminal_states_are_final(tmp_path: Path) -> None:
    for state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
        for target in JobState:
            with pytest.raises(TrainingJobError, match="JOB_TRANSITION_INVALID"):
                transition(state, target)
    env = make_env(tmp_path)
    job_id, _, result = env.trained()
    with pytest.raises(TrainingJobError, match="JOB_TRANSITION_INVALID"):
        env.jobs.cancel(job_id)
    with pytest.raises(TrainingJobError, match="JOB_TRANSITION_INVALID"):
        env.jobs.complete(job_id, result)


def test_forged_log_entry_into_registry_state_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    job_id, _, _ = env.trained()
    path = tmp_path / "jobs.jsonl"
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    forged = dict(entries[-1], seq=len(entries) + 1, event="COMPLETED", from_state="COMPLETED", to_state="PROMOTED",
                  prev_hash=entries[-1]["entry_hash"])
    forged.pop("entry_hash")
    forged["entry_hash"] = canonical_hash(forged)  # a correctly re-chained forgery
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(TrainingJobError, match="LEDGER_HEAD_NOT_ANCHORED"):  # a suffix nobody anchored
        env.jobs.get(job_id)
    reanchor(path)  # an attacker who also rewrites the anchor still cannot leave the job vocabulary
    with pytest.raises(TrainingJobError, match="JOB_LOG_CORRUPT"):
        env.jobs.get(job_id)


def test_tampered_log_breaks_the_chain(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    job_id, _, _ = env.trained()
    path = tmp_path / "jobs.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["approval_id"] = "e" * 32
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(TrainingJobError, match="JOB_LOG_CORRUPT"):
        env.jobs.get(job_id)


def test_symlinked_log_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    create_link_or_skip(target, tmp_path / "jobs.jsonl", directory=False)
    with pytest.raises(TrainingJobError, match="JOB_LOG_CORRUPT"):
        env.jobs.create(env.contract(), job_id="job-00000001")


def test_backend_failure_records_failed_without_artifact(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    collinear = env.contract(config={"algorithm": "OLS", "feature_count": 2})
    rows = [{"features": [float(i), 2.0 * i], "label": float(i)} for i in range(20)]
    singular = make_snapshot(env.store, [rows], snapshot_id="singular")
    contract = make_contract(singular, env.held_out, config=collinear.config_dict())
    env.approved_job(contract)
    with pytest.raises(TrainingRunError, match="DESIGN_MATRIX_SINGULAR"):
        execute_job(env.jobs, "job-00000001", contract=contract, dataset=singular, backend=env.backend,
                    store=env.store, eval_dataset=env.held_out)
    job = env.jobs.get("job-00000001")
    assert job.state is JobState.FAILED and job.failure_code == "DESIGN_MATRIX_SINGULAR"
    assert job.artifact_sha256 is None


@pytest.mark.parametrize("kind, schema_ref, config", [
    (TrainingKind.EMBEDDING, "training.text-pairs/v1", {"epochs": 1, "batch_size": 8, "learning_rate": 0.0001}),
    (TrainingKind.LORA_FINETUNE, "training.instruction-pairs/v1",
     {"rank": 8, "alpha": 16, "learning_rate": 0.0002, "epochs": 1}),
    (TrainingKind.RERANKER, "training.query-passage-labels/v1",
     {"epochs": 1, "batch_size": 8, "learning_rate": 0.0001}),
])
def test_contract_only_backend_is_unavailable_and_never_runs(tmp_path: Path, kind: TrainingKind, schema_ref: str,
                                                             config: dict) -> None:
    env = make_env(tmp_path)
    spec = CONTRACT_ONLY_SPECS[kind]
    backend = ContractOnlyBackend(kind, spec)
    dataset = DatasetSnapshot.build(
        snapshot_id="pairs", schema_ref=schema_ref, members=env.train.members, record_count=1,
        classification=DataClassification.INTERNAL, license_spdx="Apache-2.0",
        provenance=DatasetProvenance(source_ref="project.fixture", collection_method="OPERATOR_CURATED"),
        created_at=CREATED)
    contract = make_contract(dataset, None, kind=kind, backend_adapter_id=spec.adapter_id,
                             base_model=ModelRef(model_id="org/base", version="1", license_spdx="Apache-2.0"),
                             config=config)
    backend.validate_contract(contract, dataset)  # the contract shape for the future backend is checked
    env.approved_job(contract)
    with pytest.raises(TrainingRunError, match="BACKEND_UNAVAILABLE"):
        execute_job(env.jobs, "job-00000001", contract=contract, dataset=dataset, backend=backend,
                    store=env.store)
    assert env.jobs.get("job-00000001").state is JobState.APPROVED_TO_RUN
    with pytest.raises(TrainingBackendError, match="BACKEND_UNAVAILABLE"):
        backend.run(contract, dataset, artifacts=ScopedArtifacts(env.store, readable=()))
    missing = make_contract(dataset, None, kind=kind, backend_adapter_id=spec.adapter_id,
                            base_model=contract.base_model, config={"epochs": 1})
    with pytest.raises(TrainingContractError, match="CONFIG_REQUIRED_KEY_MISSING"):
        backend.validate_contract(missing, dataset)


def test_vision_detection_contract_records_its_copyleft_engine_license(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    spec = CONTRACT_ONLY_SPECS[TrainingKind.VISION_DETECTION]
    backend = ContractOnlyBackend(TrainingKind.VISION_DETECTION, spec)
    assert backend.descriptor.license.spdx == "AGPL-3.0-only"
    assert backend.descriptor.license.distribution is Distribution.RUNTIME_OPTIONAL
    # PADOS-owned backends are AGPL-3.0-only too, so the operator's allow-list here names only a permissive engine
    # license: the copyleft third-party engine is refused unless the operator lists its license explicitly.
    permissive = LicenseConstraints(allowed_dataset_spdx=("CC-BY-4.0",), allowed_backend_spdx=("Apache-2.0",),
                                    output_spdx="Apache-2.0")
    contract = env.contract(license_constraints=permissive)
    with pytest.raises(TrainingContractError, match="BACKEND_LICENSE_NOT_ALLOWED"):
        validate_contract(contract, env.train, backend_kind=TrainingKind.ALGORITHM_FIT,
                          backend_adapter_id=contract.backend_adapter_id,
                          backend_license_spdx=backend.descriptor.license.spdx, eval_dataset=env.held_out)


def test_output_not_written_through_scope_fails_closed(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.approved_job(contract)
    real = env.backend

    class Smuggler:
        descriptor = real.descriptor
        kind = real.kind

        def available(self) -> bool:
            return True

        def validate_contract(self, contract, dataset):  # type: ignore[no-untyped-def]
            real.validate_contract(contract, dataset)

        def run(self, contract, dataset, *, artifacts):  # type: ignore[no-untyped-def]
            result = real.run(contract, dataset, artifacts=artifacts)
            ref = env.store.put(b"{}", media_type="application/json", kind="training.model")  # outside scope
            return TrainingRunResult(type(result.artifact)(ref, result.artifact.backend_adapter_id,
                                                           result.artifact.contract_hash,
                                                           result.artifact.dataset_hash), result.metrics)

    with pytest.raises(TrainingRunError, match="BACKEND_OUTPUT_NOT_WRITTEN_THROUGH_SCOPE"):
        execute_job(env.jobs, "job-00000001", contract=contract, dataset=env.train, backend=Smuggler(),
                    store=env.store, eval_dataset=env.held_out)
    assert env.jobs.get("job-00000001").state is JobState.FAILED


def test_expired_run_approval_is_refused(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    contract = env.contract()
    env.jobs.create(contract, job_id="job-00000001")
    record = env.decision(DecisionOperation.APPROVE_TRAINING_RUN, env.run_subject("job-00000001"),
                          lifetime=timedelta(seconds=1))
    env.clock.current += timedelta(hours=2)
    with pytest.raises(TrainingJobError, match="DECISION_EXPIRED"):
        env.jobs.approve("job-00000001", record)
